/** The upload engine: sends one file to the server in parts, resumes from what the server already has, and can be stopped
 *  at any moment. No browser or app imports, so it is unit-tested on its own (uploader.test.ts); the app wires it to the
 *  network in store/uploads.ts.
 *
 *  Memory: only one part (5 MiB) is ever being read, whatever the size of the song. `file.slice()` is a view of the file on
 *  disk, not a copy, and the part in flight is released as soon as the server has it. */

export interface UploadFields {
  license: string;
  title?: string;
  artist?: string;
  album?: string;
  genre?: string;
}

export interface SessionInfo {
  id: string;
  size: number;
  part_size: number;
  total_parts: number;
  received: number[];
}

export interface CompleteResult {
  duplicate: boolean;
  reason?: string | null;
  trackId?: string;
}

/** What the engine needs from the network. The real one is in store/uploads.ts; tests pass a fake. */
export interface Transport {
  createSession(name: string, size: number, fields: UploadFields): Promise<SessionInfo>;
  /** null when the server no longer knows the session (it expired, or was cancelled elsewhere). */
  getSession(id: string): Promise<SessionInfo | null>;
  putPart(id: string, n: number, body: Blob, onProgress: (loadedBytes: number) => void, signal: AbortSignal): Promise<void>;
  complete(id: string): Promise<CompleteResult>;
  cancel(id: string): Promise<void>;
}

/** The server refused with this status (a 4xx the user has to act on, or a 5xx that kept failing). */
export class HttpError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "HttpError";
    this.status = status;
  }
}

/** The connection kept failing: the upload is paused, not lost, and continues from where it stopped. */
export class ConnectionError extends Error {
  constructor(message = "Connection lost") {
    super(message);
    this.name = "ConnectionError";
  }
}

/** The upload was stopped by the user (pause or cancel). */
export class StoppedError extends Error {
  constructor() {
    super("Stopped");
    this.name = "StoppedError";
  }
}

class SessionLostError extends Error {}

export type Update = { sessionId?: string; sent?: number; state?: "finishing" };

export interface RunOptions {
  sleep?: (ms: number) => Promise<void>;
  /** wait before retry 1, 2, 3 … of a failing request */
  backoff?: number[];
  /** called with the session id once known, and with null when it is gone (to forget it) */
  remember?: (sessionId: string | null) => void;
}

export function partLength(size: number, partSize: number, n: number): number {
  return Math.min(partSize, size - (n - 1) * partSize);
}

const DEFAULT_BACKOFF = [1000, 3000, 8000];
const RETRYABLE_4XX = new Set([408, 425, 429]);

export async function runUpload(
  file: { name: string; size: number; slice(start: number, end: number): Blob },
  fields: UploadFields,
  resumeSessionId: string | undefined,
  transport: Transport,
  signal: AbortSignal,
  onUpdate: (u: Update) => void,
  options: RunOptions = {},
): Promise<CompleteResult> {
  const sleep = options.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const backoff = options.backoff ?? DEFAULT_BACKOFF;

  const stopIfAborted = () => {
    if (signal.aborted) throw new StoppedError();
  };

  /** Run one request, retrying connection trouble and server errors with a growing wait. */
  async function withRetry<T>(call: () => Promise<T>): Promise<T> {
    for (let attempt = 0; ; attempt++) {
      stopIfAborted();
      try {
        return await call();
      } catch (e) {
        stopIfAborted();
        if (e instanceof StoppedError || e instanceof SessionLostError) throw e;
        if (e instanceof HttpError) {
          if (e.status === 404 || e.status === 409) throw new SessionLostError();
          if (e.status >= 400 && e.status < 500 && !RETRYABLE_4XX.has(e.status)) throw e; // the server said no: retrying will not help
        }
        if (attempt >= backoff.length) throw new ConnectionError(e instanceof Error ? e.message : undefined);
        await sleep(backoff[attempt]);
      }
    }
  }

  async function attempt(sessionId: string | undefined): Promise<CompleteResult> {
    let session = sessionId ? await withRetry(() => transport.getSession(sessionId)) : null;
    if (!session) session = await withRetry(() => transport.createSession(file.name, file.size, fields));
    options.remember?.(session.id);
    onUpdate({ sessionId: session.id });

    const have = new Set(session.received);
    let sent = 0;
    for (let n = 1; n <= session.total_parts; n++) if (have.has(n)) sent += partLength(file.size, session.part_size, n);
    onUpdate({ sent });

    for (let n = 1; n <= session.total_parts; n++) {
      if (have.has(n)) continue;
      const start = (n - 1) * session.part_size;
      const length = partLength(file.size, session.part_size, n);
      const base = sent;
      await withRetry(() => transport.putPart(session.id, n, file.slice(start, start + length), (loaded) => onUpdate({ sent: base + loaded }), signal));
      sent = base + length;
      onUpdate({ sent });
    }
    onUpdate({ state: "finishing" });
    return withRetry(() => transport.complete(session.id));
  }

  // A session the server has forgotten is restarted once from the beginning; a second loss is an error.
  let sessionId = resumeSessionId;
  for (let restarts = 0; ; restarts++) {
    try {
      return await attempt(sessionId);
    } catch (e) {
      if (!(e instanceof SessionLostError)) throw e;
      options.remember?.(null);
      if (restarts >= 1) throw new HttpError(410, "The upload could not be continued. Please try again.");
      sessionId = undefined;
      onUpdate({ sent: 0 });
    }
  }
}
