import assert from "node:assert/strict";
import { test } from "node:test";
import {
  ConnectionError, HttpError, partLength, runUpload, StoppedError,
  type CompleteResult, type SessionInfo, type Transport, type Update,
} from "./uploader.ts";

const PART = 5 * 1024 * 1024;
const fileOf = (size: number) => new Blob([new Uint8Array(size)]) as unknown as { name: string; size: number; slice(a: number, b: number): Blob };
const file = (size: number, name = "song.mp3") => Object.assign(fileOf(size), { name });
const fields = { license: "Personal library" };
const instant = { sleep: async () => {}, backoff: [0, 0, 0] };

/** An in-memory server with the same contract as services/catalogue/app/uploads.py. */
class FakeServer implements Transport {
  sessions = new Map<string, { size: number; parts: Map<number, number> }>();
  calls: string[] = [];
  failPut: (n: number, attempt: number) => Error | null = () => null;
  failComplete: () => Error | null = () => null;
  result: CompleteResult = { duplicate: false, trackId: "t1" };
  private attempts = new Map<number, number>();
  next = 1;

  async createSession(name: string, size: number): Promise<SessionInfo> {
    const id = `s${this.next++}`;
    this.sessions.set(id, { size, parts: new Map() });
    this.calls.push(`create`);
    return this.info(id)!;
  }
  info(id: string): SessionInfo | null {
    const s = this.sessions.get(id);
    return s ? { id, size: s.size, part_size: PART, total_parts: Math.max(1, Math.ceil(s.size / PART)), received: [...s.parts.keys()].sort((a, b) => a - b) } : null;
  }
  async getSession(id: string) {
    this.calls.push(`get`);
    return this.info(id);
  }
  async putPart(id: string, n: number, body: Blob, onProgress: (b: number) => void, signal: AbortSignal) {
    this.calls.push(`put${n}`);
    const attempt = (this.attempts.get(n) ?? 0) + 1;
    this.attempts.set(n, attempt);
    const err = this.failPut(n, attempt);
    if (err) throw err;
    if (signal.aborted) throw new Error("aborted");
    const s = this.sessions.get(id);
    if (!s) throw new HttpError(404, "gone");
    onProgress(body.size / 2);
    s.parts.set(n, body.size);
    onProgress(body.size);
  }
  async complete(id: string) {
    this.calls.push(`complete`);
    const err = this.failComplete();
    if (err) throw err;
    this.sessions.delete(id);
    return this.result;
  }
  async cancel(id: string) {
    this.sessions.delete(id);
  }
}

test("a file is sent part by part in order, then completed, with progress that reaches the full size", async () => {
  const server = new FakeServer();
  const size = 11 * 1024 * 1024; // 3 parts
  const updates: Update[] = [];
  const result = await runUpload(file(size), fields, undefined, server, new AbortController().signal, (u) => updates.push(u), instant);
  assert.deepEqual(server.calls, ["create", "put1", "put2", "put3", "complete"]);
  assert.deepEqual(result, { duplicate: false, trackId: "t1" });
  assert.equal(Math.max(...updates.map((u) => u.sent ?? 0)), size);
  assert.ok(updates.some((u) => u.state === "finishing"));
  assert.equal(partLength(size, PART, 3), size - 2 * PART);
});

test("a one-part file (smaller than 5 MiB) works", async () => {
  const server = new FakeServer();
  await runUpload(file(1234), fields, undefined, server, new AbortController().signal, () => {}, instant);
  assert.deepEqual(server.calls, ["create", "put1", "complete"]);
});

test("resuming skips the parts the server already has and counts them as sent", async () => {
  const server = new FakeServer();
  const size = 11 * 1024 * 1024;
  const s = await server.createSession("x", size);
  server.sessions.get(s.id)!.parts.set(1, PART);
  server.sessions.get(s.id)!.parts.set(2, PART);
  server.calls.length = 0;
  const first: Update[] = [];
  await runUpload(file(size), fields, s.id, server, new AbortController().signal, (u) => first.push(u), instant);
  assert.deepEqual(server.calls, ["get", "put3", "complete"]); // not re-sent
  assert.equal(first.find((u) => u.sent !== undefined)!.sent, 2 * PART);
});

test("stopping mid-upload keeps what already arrived, and the next run continues from there", async () => {
  const server = new FakeServer();
  const size = 16 * 1024 * 1024; // 4 parts
  const controller = new AbortController();
  server.failPut = (n) => {
    if (n === 3) {
      controller.abort(); // the user pauses while part 3 is in flight
      return new Error("aborted");
    }
    return null;
  };
  let sessionId: string | undefined;
  await assert.rejects(
    runUpload(file(size), fields, undefined, server, controller.signal, (u) => { if (u.sessionId) sessionId = u.sessionId; }, instant),
    StoppedError,
  );
  assert.deepEqual(server.info(sessionId!)!.received, [1, 2]);

  server.failPut = () => null;
  server.calls.length = 0;
  await runUpload(file(size), fields, sessionId, server, new AbortController().signal, () => {}, instant);
  assert.deepEqual(server.calls, ["get", "put3", "put4", "complete"]);
});

test("a flaky connection is retried and the upload carries on", async () => {
  const server = new FakeServer();
  server.failPut = (n, attempt) => (n === 1 && attempt < 3 ? new HttpError(503, "busy") : null);
  await runUpload(file(100), fields, undefined, server, new AbortController().signal, () => {}, instant);
  assert.deepEqual(server.calls, ["create", "put1", "put1", "put1", "complete"]);
});

test("the server saying no (a 4xx) fails at once without retrying", async () => {
  const server = new FakeServer();
  server.failPut = () => new HttpError(400, "Part 1 must be exactly 100 bytes");
  await assert.rejects(
    runUpload(file(100), fields, undefined, server, new AbortController().signal, () => {}, instant),
    (e: unknown) => e instanceof HttpError && e.status === 400,
  );
  assert.deepEqual(server.calls, ["create", "put1"]);
});

test("a connection that keeps failing pauses the upload (ConnectionError), it does not lose it", async () => {
  const server = new FakeServer();
  server.failPut = () => new TypeError("Failed to fetch");
  await assert.rejects(runUpload(file(100), fields, undefined, server, new AbortController().signal, () => {}, instant), ConnectionError);
  assert.equal(server.calls.filter((c) => c === "put1").length, 4); // the first try and 3 retries
  assert.equal(server.sessions.size, 1); // the session is still there to resume
});

test("a session the server forgot is restarted once from the beginning", async () => {
  const server = new FakeServer();
  const forgotten: (string | null)[] = [];
  const result = await runUpload(file(100), fields, "does-not-exist", server, new AbortController().signal, () => {}, { ...instant, remember: (id) => forgotten.push(id) });
  assert.equal(result.trackId, "t1");
  assert.deepEqual(server.calls, ["get", "create", "put1", "complete"]);
  assert.equal(forgotten.at(-1) !== null, true);
});

test("losing the session twice is reported instead of looping forever", async () => {
  const server = new FakeServer();
  server.failPut = () => new HttpError(404, "Upload session not found");
  await assert.rejects(runUpload(file(100), fields, undefined, server, new AbortController().signal, () => {}, instant), (e: unknown) => e instanceof HttpError && e.status === 410);
});

test("a repeat song is reported back, not treated as an error", async () => {
  const server = new FakeServer();
  server.result = { duplicate: true, reason: "same title and artist already in your library", trackId: "old" };
  const r = await runUpload(file(100), fields, undefined, server, new AbortController().signal, () => {}, instant);
  assert.equal(r.duplicate, true);
  assert.match(r.reason!, /already in your library/);
});

test("a completion that cannot be answered yet is retried, and a missing-parts answer restarts", async () => {
  const server = new FakeServer();
  let n = 0;
  server.failComplete = () => (n++ === 0 ? new HttpError(502, "bad gateway") : null);
  await runUpload(file(100), fields, undefined, server, new AbortController().signal, () => {}, instant);
  assert.deepEqual(server.calls, ["create", "put1", "complete", "complete"]);

  const again = new FakeServer();
  let m = 0;
  again.failComplete = () => (m++ === 0 ? new HttpError(409, "1 of 2 parts have not arrived yet") : null);
  await runUpload(file(100), fields, undefined, again, new AbortController().signal, () => {}, instant);
  assert.deepEqual(again.calls, ["create", "put1", "complete", "create", "put1", "complete"]);
});
