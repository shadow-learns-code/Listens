import assert from "node:assert/strict";
import { test } from "node:test";
import { PlaybackReporter, type PlaybackReport } from "./native.ts";

const song = { active: true, title: "A", artist: "B", playing: true, durationMs: 200_000, hasNext: true, hasPrev: false };

function reporter() {
  const sent: PlaybackReport[] = [];
  let now = 1_000_000;
  const r = new PlaybackReporter((x) => sent.push(x), () => now);
  return { r, sent, tick: (ms: number) => (now += ms) };
}

test("the first report is always sent", () => {
  const { r, sent } = reporter();
  r.report({ ...song, positionMs: 0 });
  assert.equal(sent.length, 1);
});

test("a normal ticking position is not re-sent (the phone extrapolates it)", () => {
  const { r, sent, tick } = reporter();
  r.report({ ...song, positionMs: 0 });
  for (let i = 1; i <= 20; i++) {
    tick(250);
    r.report({ ...song, positionMs: i * 250 });
  }
  assert.equal(sent.length, 1);
});

test("pausing, a new song and a changed queue are sent at once", () => {
  const { r, sent, tick } = reporter();
  r.report({ ...song, positionMs: 0 });
  tick(500);
  r.report({ ...song, playing: false, positionMs: 500 });
  tick(500);
  r.report({ ...song, playing: false, title: "C", positionMs: 0 });
  tick(500);
  r.report({ ...song, playing: false, title: "C", hasNext: false, positionMs: 0 });
  assert.equal(sent.length, 4);
});

test("a seek (the position jumping) is sent, a paused position is compared to where it stood", () => {
  const { r, sent, tick } = reporter();
  r.report({ ...song, positionMs: 0 });
  tick(1000);
  r.report({ ...song, positionMs: 90_000 });
  assert.equal(sent.length, 2);
  tick(1000);
  r.report({ ...song, positionMs: 91_000 });
  assert.equal(sent.length, 2);
});

test("a slow resync is sent even when nothing changed", () => {
  const { r, sent, tick } = reporter();
  r.report({ ...song, positionMs: 0 });
  tick(21_000);
  r.report({ ...song, positionMs: 21_000 });
  assert.equal(sent.length, 2);
});
