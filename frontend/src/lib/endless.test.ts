import assert from "node:assert/strict";
import { test } from "node:test";
import { BATCH, continuation, MIN_FRESH, pickContinuation, upcomingCount, type Source } from "./endless.ts";
import type { Track } from "./types.ts";

let n = 0;
const song = (title: string, artist = `Artist ${title}`, extra: Partial<Track> = {}): Track => ({
  id: `id-${++n}-${title}`, title, artist: { id: `a-${artist}`, name: artist }, album: null, genre: null, license: null, status: "ready",
  duration_ms: 200000, cover_url: null, error: null, created_at: "2026-01-01", ...extra,
});
const rng = (seed: number) => () => ((seed = (seed * 1664525 + 1013904223) % 4294967296) / 4294967296);
const many = (k: number, prefix = "s") => Array.from({ length: k }, (_, i) => song(`${prefix}${i}`));

test("upcomingCount is how many songs are left after the current one", () => {
  assert.equal(upcomingCount(5, 1), 3);
  assert.equal(upcomingCount(5, 4), 0);
  assert.equal(upcomingCount(0, -1), 0);
});

test("nothing already in the queue, unplayable, or repeated in the list is added", () => {
  const queue = many(3, "q");
  const fresh = many(6, "f");
  const failed = song("broken", "x", { status: "failed" });
  const out = pickContinuation([...queue, ...fresh, fresh[0], failed], queue, queue[2], { style: "standard", limit: 20 });
  assert.deepEqual(out.map((t) => t.id), fresh.map((t) => t.id)); // standard keeps the server's order
});

test("songs heard in the last hours are left out while there is enough else", () => {
  const queue = [song("now")];
  const heard = many(3, "heard"), others = many(25, "other");
  const now = Date.now();
  const recent = new Map(heard.map((t) => [t.id, now - 30 * 60 * 1000]));
  const out = pickContinuation([...heard, ...others], queue, queue[0], { style: "standard", limit: 20, recentlyPlayed: recent, now });
  assert.equal(out.length, 20);
  assert.ok(out.every((t) => !recent.has(t.id)));
});

test("a small library does not run dry: the least recently heard songs top the list up", () => {
  const queue = [song("now")];
  const pool = many(6, "p");
  const now = Date.now();
  const recent = new Map(pool.map((t, i) => [t.id, now - (10 + i) * 60 * 1000])); // all heard, p5 the longest ago
  const out = pickContinuation(pool, queue, queue[0], { style: "standard", limit: 4, recentlyPlayed: recent, now });
  assert.equal(out.length, 4);
  assert.deepEqual(out.map((t) => t.title), ["p5", "p4", "p3", "p2"]);
});

test("smart style separates the same singer more than a plain random order does", () => {
  const queue = [song("now", "Lead")];
  const same = Array.from({ length: 6 }, (_, i) => song(`same${i}`, "Same Singer"));
  const others = Array.from({ length: 6 }, (_, i) => song(`other${i}`, `Other ${i}`));
  const adjacent = (list: Track[]) => list.slice(1).filter((t, i) => t.artist.id === list[i].artist.id).length;
  let smart = 0, random = 0;
  const runs = 60;
  for (let seed = 1; seed <= runs; seed++) {
    smart += adjacent(pickContinuation([...same, ...others], queue, queue[0], { style: "smart", limit: 12, rng: rng(seed) }));
    const r = rng(seed + 1000), plain = [...same, ...others]; // Fisher-Yates: a uniformly random order
    for (let i = plain.length - 1; i > 0; i--) { const j = Math.floor(r() * (i + 1)); [plain[i], plain[j]] = [plain[j], plain[i]]; }
    random += adjacent(plain);
  }
  assert.ok(smart < random * 0.8, `smart ${smart / runs} vs random ${random / runs} same-singer neighbours per 12 songs`);
});

function source(similarById: Record<string, Track[]>, made: Track[] = []): Source & { calls: string[] } {
  const calls: string[] = [];
  return {
    calls,
    similar: async (id) => { calls.push(`similar:${id}`); return similarById[id] ?? []; },
    madeForYou: async () => { calls.push("made"); return made; },
  };
}

test("enough similar songs: only the similar list is asked", async () => {
  const queue = [song("a"), song("b")];
  const src = source({ [queue[1].id]: many(12, "sim") });
  const out = await continuation(queue, 0, src, { style: "standard" });
  assert.equal(out.length, 12);
  assert.deepEqual(src.calls, [`similar:${queue[1].id}`]);
});

test("too few fresh songs: it widens to the current song's neighbours, then Made for you", async () => {
  const queue = [song("a"), song("b"), song("c")];
  const dup = queue[0]; // similar() returns a song that is already queued: no use
  const src = source({ [queue[2].id]: [dup, song("x1")], [queue[1].id]: [song("y1")] }, many(10, "mfy"));
  const out = await continuation(queue, 1, src, { style: "standard" });
  assert.ok(out.length >= MIN_FRESH && out.length <= BATCH);
  assert.deepEqual(src.calls, [`similar:${queue[2].id}`, `similar:${queue[1].id}`, "made"]);
  assert.ok(out.some((t) => t.title.startsWith("mfy")));
});

test("a failing server never throws: it adds nothing and tries again later", async () => {
  const queue = [song("a")];
  const broken: Source = { similar: async () => { throw new Error("502"); }, madeForYou: async () => { throw new Error("502"); } };
  assert.deepEqual(await continuation(queue, 0, broken, { style: "smart" }), []);
  assert.deepEqual(await continuation([], -1, broken, { style: "smart" }), []);
});
