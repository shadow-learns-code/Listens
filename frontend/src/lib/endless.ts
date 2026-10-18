import type { Track } from "./types";
import { smartShuffle, type ShuffleOptions } from "./shuffle.ts"; // relative with extension: the node test runner has no "@/" alias

/** Endless play: music that never stops. Before the queue runs out, the player quietly adds more songs, so there is never a
 *  gap waiting for the server (which can take ~25 s to wake on the free tier).
 *
 *  What gets added is chosen in three steps:
 *   1. candidates   songs similar in mood to what is playing (the server's mood-aware /similar), with the listener's
 *                   "Made for you" picks as a backstop when that is too short
 *   2. filter       nothing already in the queue (this session), and, while there is enough, nothing heard in the last
 *                   few hours, so it never loops on the same few songs
 *   3. order        the smart shuffle (variety: not the same singer or film twice in a row; flow: close in energy and
 *                   tempo to the song before; liked songs a little more often), so the run feels varied but stays in mood
 */

/** Add more when this few songs are left after the current one. */
export const REFILL_AT = 2;
/** How many to ask for and add at a time. */
export const BATCH = 20;
/** Below this many fresh candidates, widen the search (the current song's neighbours, then Made for you). */
export const MIN_FRESH = 5;
const RECENT_WINDOW_MS = 3 * 60 * 60 * 1000;

export const upcomingCount = (queueLength: number, index: number) => Math.max(0, queueLength - index - 1);

export interface Source {
  similar(trackId: string, limit: number): Promise<Track[]>;
  madeForYou(limit: number): Promise<Track[]>;
}

export interface ContinuationOptions extends ShuffleOptions {
  style: "smart" | "standard";
  limit?: number;
}

/** Pure part: from candidate songs, pick what to add after `queue`. */
export function pickContinuation(candidates: Track[], queue: Track[], last: Track | null, o: ContinuationOptions): Track[] {
  const limit = o.limit ?? BATCH;
  const inQueue = new Set(queue.map((t) => t.id));
  const seen = new Set<string>();
  const fresh: Track[] = [];
  for (const t of candidates) {
    if (t.status !== "ready" || inQueue.has(t.id) || seen.has(t.id)) continue; // unplayable, already queued, or a repeat in the list
    seen.add(t.id);
    fresh.push(t);
  }
  const now = o.now ?? Date.now();
  const recent = o.recentlyPlayed;
  const notRecent = fresh.filter((t) => {
    const at = recent?.get(t.id);
    return at === undefined || now - at > RECENT_WINDOW_MS;
  });
  // prefer songs not heard lately, but a small library must not run dry: top up with the least-recent of the rest
  const pool = [...notRecent];
  if (pool.length < limit) {
    const rest = fresh.filter((t) => !notRecent.includes(t)).sort((a, b) => (recent?.get(a.id) ?? 0) - (recent?.get(b.id) ?? 0));
    pool.push(...rest.slice(0, limit - pool.length));
  }
  const ordered = o.style === "smart" ? smartShuffle(pool, last, o) : pool; // "standard" keeps the server's best-match-first order
  return ordered.slice(0, limit);
}

/** Fetches candidates (widening only when too few are fresh) and returns the songs to add. Never throws: a failed fetch
 *  just means nothing is added this time and the next change of song tries again. */
export async function continuation(queue: Track[], index: number, source: Source, o: ContinuationOptions): Promise<Track[]> {
  const last = queue[queue.length - 1] ?? null;
  const current = queue[index] ?? last;
  if (!last) return [];
  const safe = async (p: Promise<Track[]>) => p.catch(() => [] as Track[]);
  const want = o.limit ?? BATCH;

  let candidates = await safe(source.similar(last.id, 50));
  let picked = pickContinuation(candidates, queue, last, o);
  if (picked.length < MIN_FRESH && current && current.id !== last.id) {
    candidates = [...candidates, ...(await safe(source.similar(current.id, 50)))];
    picked = pickContinuation(candidates, queue, last, o);
  }
  if (picked.length < MIN_FRESH) {
    candidates = [...candidates, ...(await safe(source.madeForYou(50)))];
    picked = pickContinuation(candidates, queue, last, { ...o, limit: want });
  }
  return picked;
}
