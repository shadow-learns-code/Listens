"use client";

import { useCallback, useEffect, useState } from "react";
import { api, getQuiet } from "@/lib/api";

interface Processing {
  paused: boolean;
  waiting: number;
  processing: number;
  ready: number;
  failed: number;
}

/** Song processing: the conversion that turns an uploaded song into something that can be played. Shows what is waiting and
 *  lets you start it right now, pause it, or resume it. It controls the server (Render) worker only: workers running on a
 *  laptop are not affected. */
export default function ProcessingCard() {
  const [p, setP] = useState<Processing | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  const load = useCallback(() => getQuiet<Processing>("/api/catalogue/processing").then(setP).catch(() => {}), []);
  useEffect(() => {
    load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [load]);

  async function act(path: string, init: RequestInit, message: string) {
    setBusy(true);
    setNote(message);
    try {
      setP(await api<Processing>(path, init));
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const start = () => act("/api/catalogue/processing/start", { method: "POST" }, "Starting. The server may take about 30 seconds to wake up.");
  const setPaused = (paused: boolean) =>
    act("/api/catalogue/processing", { method: "PUT", body: JSON.stringify({ paused }) }, paused ? "Server processing paused. The song being converted now will finish first. Workers on your laptop keep going." : "");

  if (!p || (!p.waiting && !p.processing && !p.paused && !p.failed)) return null; // nothing to control

  const active = p.processing > 0;
  const button = "min-h-11 rounded-full px-5 font-semibold disabled:opacity-40";
  return (
    <section className="glass-card space-y-3 rounded-2xl border border-line bg-panel p-5" data-testid="processing-card" aria-label="Song processing">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-lg font-medium">Song processing</h2>
        <span className={`rounded-full px-3 py-1 text-xs ${p.paused ? "bg-raise text-muted" : active ? "bg-accent/20 text-accent" : "bg-raise text-muted"}`} data-testid="processing-status">
          {p.paused ? "Paused" : active ? "Working" : p.waiting ? "Waiting to start" : "Idle"}
        </span>
      </div>
      <p className="text-sm text-muted" data-testid="processing-counts">
        {p.waiting} waiting · {p.processing} converting · {p.ready} ready{p.failed ? ` · ${p.failed} failed` : ""}
      </p>
      <div className="flex flex-wrap gap-2">
        {p.paused ? (
          <button type="button" data-testid="processing-resume" disabled={busy} onClick={() => setPaused(false)} className={`${button} border border-line`}>Resume</button>
        ) : (
          <button type="button" data-testid="processing-pause" disabled={busy || (!p.waiting && !p.processing)} onClick={() => setPaused(true)} className={`${button} border border-line`}>Pause</button>
        )}
        <button type="button" data-testid="processing-start" disabled={busy || !p.waiting} onClick={start} className={`${button} bg-accent text-ink`}>Start processing now</button>
      </div>
      {note && <p className="text-xs text-muted" role="status">{note}</p>}
    </section>
  );
}
