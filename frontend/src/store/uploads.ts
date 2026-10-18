import { create } from "zustand";
import { api, ApiError, getAccessToken } from "@/lib/api";
import {
  ConnectionError, HttpError, runUpload, StoppedError,
  type CompleteResult, type SessionInfo, type Transport, type UploadFields,
} from "@/lib/uploader";

/** The upload queue. It lives outside any page, so uploads carry on while you browse, and one file is sent at a time so
 *  memory stays small (only the 5 MiB part in flight is ever held). */

export type UploadState = "queued" | "uploading" | "finishing" | "paused" | "done" | "duplicate" | "error";

export interface UploadItem {
  id: number;
  name: string;
  size: number;
  sent: number;
  state: UploadState;
  message?: string;
  reason?: string;
}

interface UploadsState {
  items: UploadItem[];
  /** true while the user (or a lost connection) has stopped the queue */
  paused: boolean;
  /** why it is paused: "user" stays paused until Resume, "offline" continues by itself when the connection returns */
  pausedBy: "user" | "offline" | null;
  add(files: File[], fields: UploadFields): void;
  pause(): void;
  resume(): void;
  cancel(id: number): void;
  clearFinished(): void;
}

const FINISHED: UploadState[] = ["done", "duplicate", "error"];
export const isFinished = (s: UploadState) => FINISHED.includes(s);

// File objects and abort controllers are not plain data: keep them beside the store, keyed by item id.
const files = new Map<number, { file: File; fields: UploadFields }>();
let controller: AbortController | null = null;
let seq = 0;
let running = false;

// A reload loses the File, but not the session on the server: remember which session belonged to which file so choosing the
// same file again continues from where it stopped instead of starting over.
const REMEMBER_KEY = "listens.upload-sessions";
const fingerprint = (f: File) => `${f.name}|${f.size}|${f.lastModified}`;
function readRemembered(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(REMEMBER_KEY) ?? "{}");
  } catch {
    return {};
  }
}
function remember(f: File, sessionId: string | null) {
  try {
    const all = readRemembered();
    if (sessionId) all[fingerprint(f)] = sessionId;
    else delete all[fingerprint(f)];
    localStorage.setItem(REMEMBER_KEY, JSON.stringify(all));
  } catch {
    /* private mode: resuming after a reload just won't be offered */
  }
}

async function errorOf(res: Response): Promise<HttpError> {
  let msg = res.statusText;
  try {
    const body = await res.json();
    msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
  } catch {
    /* non-JSON error body */
  }
  return new HttpError(res.status, msg);
}

/** JSON calls go through the normal API helper (token refresh, 401 retry); a refusal becomes an HttpError, a dead connection
 *  stays a network error that the engine retries. */
async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  try {
    return await api<T>(path, init, true, { quiet: true });
  } catch (e) {
    if (e instanceof ApiError && e.status > 0) throw new HttpError(e.status, e.message);
    throw e;
  }
}

const BASE = "/api/catalogue/uploads/sessions";

const transport: Transport = {
  createSession: (name, size, fields) =>
    call<SessionInfo>(BASE, { method: "POST", body: JSON.stringify({ filename: name, size, rights_confirmed: true, ...fields }) }),
  async getSession(id) {
    try {
      return await call<SessionInfo>(`${BASE}/${id}`);
    } catch (e) {
      if (e instanceof HttpError && e.status === 404) return null;
      throw e;
    }
  },
  async putPart(id, n, body, onProgress, signal) {
    const token = await getAccessToken();
    await new Promise<void>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", `${BASE}/${id}/parts/${n}`);
      if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded);
      xhr.onerror = () => reject(new TypeError("Network error"));
      xhr.ontimeout = () => reject(new TypeError("Timed out"));
      xhr.onabort = () => reject(new StoppedError());
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) return resolve();
        let msg = xhr.statusText;
        try {
          const b = JSON.parse(xhr.responseText);
          if (typeof b.detail === "string") msg = b.detail;
        } catch {
          /* ignore */
        }
        reject(new HttpError(xhr.status, msg));
      };
      xhr.timeout = 120_000;
      signal.addEventListener("abort", () => xhr.abort(), { once: true });
      xhr.send(body);
    });
  },
  async complete(id) {
    const res = await call<{ duplicate: boolean; reason?: string | null; track?: { id: string } }>(`${BASE}/${id}/complete`, { method: "POST" });
    return { duplicate: res.duplicate, reason: res.reason, trackId: res.track?.id } satisfies CompleteResult;
  },
  async cancel(id) {
    await call(`${BASE}/${id}`, { method: "DELETE" }).catch(() => {});
  },
};

export const useUploads = create<UploadsState>((set, get) => {
  const patch = (id: number, p: Partial<UploadItem>) => set((s) => ({ items: s.items.map((it) => (it.id === id ? { ...it, ...p } : it)) }));

  async function pump() {
    if (running) return;
    running = true;
    try {
      for (;;) {
        const { items, paused } = get();
        const next = paused ? undefined : items.find((it) => it.state === "queued");
        if (!next) return;
        await sendOne(next);
      }
    } finally {
      running = false;
    }
  }

  async function sendOne(item: UploadItem) {
    const entry = files.get(item.id);
    if (!entry) return patch(item.id, { state: "error", message: "The file is no longer available" });
    const ctl = (controller = new AbortController());
    patch(item.id, { state: "uploading", message: undefined });
    try {
      const resume = readRemembered()[fingerprint(entry.file)];
      const result = await runUpload(
        entry.file, entry.fields, resume, transport, ctl.signal,
        (u) => {
          if (u.sent !== undefined) patch(item.id, { sent: u.sent });
          if (u.state === "finishing") patch(item.id, { state: "finishing" });
        },
        { remember: (sid) => remember(entry.file, sid) },
      );
      remember(entry.file, null);
      files.delete(item.id);
      patch(item.id, { state: result.duplicate ? "duplicate" : "done", sent: item.size, reason: result.reason ?? undefined });
    } catch (e) {
      if (e instanceof StoppedError) {
        // pause() already marked it; a cancel() removed it
        if (get().items.some((it) => it.id === item.id && it.state === "uploading")) patch(item.id, { state: "paused" });
      } else if (e instanceof ConnectionError) {
        patch(item.id, { state: "paused", message: "Connection lost. It continues when you are back online." });
        set({ paused: true, pausedBy: "offline" });
      } else {
        remember(entry.file, null);
        files.delete(item.id);
        patch(item.id, { state: "error", message: (e as Error).message });
      }
    } finally {
      if (controller === ctl) controller = null;
    }
  }

  return {
    items: [],
    paused: false,
    pausedBy: null,

    add(list, fields) {
      const added = list.map<UploadItem>((f) => {
        const id = ++seq;
        files.set(id, { file: f, fields: list.length === 1 ? fields : { license: fields.license } });
        return { id, name: f.name, size: f.size, sent: 0, state: "queued" };
      });
      set((s) => ({ items: [...s.items, ...added] }));
      void pump();
    },

    pause() {
      set((s) => ({
        paused: true,
        pausedBy: "user",
        items: s.items.map((it) => (it.state === "uploading" || it.state === "finishing" ? it : it.state === "queued" ? { ...it, state: "paused" } : it)),
      }));
      controller?.abort(); // the part in flight stops; everything already received stays on the server
    },

    resume() {
      set((s) => ({ paused: false, pausedBy: null, items: s.items.map((it) => (it.state === "paused" ? { ...it, state: "queued", message: undefined } : it)) }));
      void pump();
    },

    cancel(id) {
      const item = get().items.find((it) => it.id === id);
      const entry = files.get(id);
      if (!item) return;
      if (item.state === "uploading") controller?.abort();
      set((s) => ({ items: s.items.filter((it) => it.id !== id) }));
      files.delete(id);
      if (entry) {
        const sid = readRemembered()[fingerprint(entry.file)];
        remember(entry.file, null);
        if (sid) void transport.cancel(sid); // throw away what already arrived on the server
      }
    },

    clearFinished() {
      set((s) => ({ items: s.items.filter((it) => !isFinished(it.state)) }));
    },
  };
});

/** Overall progress for the notification and the page header: bytes sent of all files in the list. */
export function overall(items: UploadItem[]): { percent: number; active: boolean; sending: boolean; count: number; index: number } {
  const open = items.filter((it) => !isFinished(it.state));
  const total = items.reduce((n, it) => n + it.size, 0);
  const sent = items.reduce((n, it) => n + Math.min(it.sent, it.size), 0);
  const current = items.findIndex((it) => it.state === "uploading" || it.state === "finishing");
  return {
    percent: total ? Math.round((sent / total) * 100) : 0,
    active: open.length > 0,
    sending: current >= 0,
    count: items.length,
    index: current >= 0 ? current + 1 : items.length - open.length + 1,
  };
}

// Continue by itself once the connection is back (only when it was a lost connection that paused the queue).
if (typeof window !== "undefined") {
  window.addEventListener("online", () => {
    if (useUploads.getState().pausedBy === "offline") useUploads.getState().resume();
  });
}
