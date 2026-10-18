"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import ProcessingCard from "@/components/ProcessingCard";
import TrackList from "@/components/TrackList";
import { api, get } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import type { Track } from "@/lib/types";
import { isFinished, overall, useUploads, type UploadItem } from "@/store/uploads";

const LICENSES = ["Own work", "Personal library", "CC BY 4.0", "CC BY-SA 4.0", "CC BY-NC 4.0", "CC0 / Public domain"];

function statusText(it: UploadItem): string {
  switch (it.state) {
    case "queued": return "waiting";
    case "uploading": return `${Math.round((it.sent / it.size) * 100)}%`;
    case "finishing": return "checking and saving…";
    case "paused": return it.message ?? "paused";
    case "done": return "done";
    case "duplicate": return `already in your library${it.reason ? ` (${it.reason})` : ""}`;
    case "error": return it.message ?? "failed";
  }
}

export default function UploadPage() {
  const [files, setFiles] = useState<File[]>([]);
  const [license, setLicense] = useState(LICENSES[0]);
  const [rights, setRights] = useState(false);
  const [meta, setMeta] = useState({ title: "", artist: "", album: "", genre: "" });
  const { items, paused, add, pause, resume, cancel, clearFinished } = useUploads();
  const total = overall(items);
  const busy = total.active;
  const [mine, setMine] = useState<Track[]>([]);
  const input = useRef<HTMLInputElement>(null);

  const refresh = useCallback(() => get<Track[]>("/api/catalogue/tracks/mine").then(setMine).catch(() => {}), []);
  useEffect(() => { refresh(); }, [refresh]);
  // While anything is still being transcoded, poll so statuses flip to "Ready" on their own.
  const working = mine.some((t) => t.status === "uploaded" || t.status === "processing");
  useEffect(() => {
    if (!working) return;
    const t = setInterval(refresh, 2500);
    return () => clearInterval(t);
  }, [working, refresh]);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!files.length || !rights) return;
    const fields = { license, ...Object.fromEntries(Object.entries(meta).filter(([, v]) => v.trim()).map(([k, v]) => [k, v.trim()])) };
    add(files, fields); // the queue lives outside this page: leaving it does not stop the upload
    setFiles([]);
    setMeta({ title: "", artist: "", album: "", genre: "" });
    if (input.current) input.current.value = "";
  }

  // Refresh "Your uploads" whenever a file finishes sending.
  const finishedCount = items.filter((it) => isFinished(it.state)).length;
  useEffect(() => { if (finishedCount) refresh(); }, [finishedCount, refresh]);

  async function remove(t: Track) {
    if (!confirm(`Delete “${t.title}”? This removes the audio too.`)) return;
    await api(`/api/catalogue/tracks/${t.id}`, { method: "DELETE" });
    refresh();
  }

  const field = "w-full rounded-lg border border-line bg-raise px-3 py-2 outline-none focus:border-accent";
  return (
    <div className="space-y-8">
      <h1 className="text-2xl font-semibold tracking-tight">Upload music</h1>

      <form onSubmit={submit} className="glass-card space-y-4 rounded-2xl border border-line bg-panel p-5" data-testid="upload-form">
        <div>
          <label htmlFor="file" className="mb-1 block text-sm text-muted">Audio files (mp3, flac, wav, ogg, opus, m4a)</label>
          <input
            id="file" ref={input} data-testid="file-input" type="file" multiple
            accept=".mp3,.flac,.wav,.ogg,.oga,.opus,.m4a,.aac,audio/*"
            onChange={(e) => setFiles(Array.from(e.target.files ?? []))}
            className="block w-full text-sm text-muted file:mr-3 file:rounded-full file:border-0 file:bg-accent file:px-4 file:py-2 file:font-semibold file:text-ink"
          />
        </div>

        {files.length === 1 && (
          <div className="grid gap-3 sm:grid-cols-2">
            {(["title", "artist", "album", "genre"] as const).map((k) => (
              <div key={k}>
                <label htmlFor={k} className="mb-1 block text-sm capitalize text-muted">{k} <span className="text-xs">(optional, read from tags)</span></label>
                <input id={k} className={field} value={meta[k]} onChange={(e) => setMeta({ ...meta, [k]: e.target.value })} />
              </div>
            ))}
          </div>
        )}

        <div>
          <label htmlFor="license" className="mb-1 block text-sm text-muted">License</label>
          <select id="license" data-testid="license" className={field} value={license} onChange={(e) => setLicense(e.target.value)}>
            {LICENSES.map((l) => <option key={l}>{l}</option>)}
          </select>
        </div>

        <label className="flex items-start gap-3 text-sm">
          <input data-testid="rights" type="checkbox" checked={rights} onChange={(e) => setRights(e.target.checked)} className="mt-1" />
          <span>I own this audio or have the right to use it, and I understand uploads are for my private library.</span>
        </label>

        <button
          type="submit" data-testid="upload-submit" disabled={!files.length || !rights}
          className="rounded-full bg-accent px-6 py-2 font-semibold text-ink disabled:opacity-40"
        >
          {files.length > 1 ? `Upload ${files.length} files` : "Upload"}
        </button>
      </form>

      {items.length > 0 && (
        <div className="space-y-2" data-testid="upload-progress">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span className="text-muted" data-testid="upload-summary">
              {busy ? `${paused ? "Paused" : "Uploading"} · file ${Math.min(total.index, total.count)} of ${total.count} · ${total.percent}%` : "All done"}
            </span>
            <span className="ml-auto flex gap-2">
              {busy && (paused
                ? <button type="button" data-testid="upload-resume" onClick={resume} className="min-h-11 rounded-full bg-accent px-4 font-semibold text-ink">Resume</button>
                : <button type="button" data-testid="upload-pause" onClick={pause} className="min-h-11 rounded-full border border-line px-4">Pause</button>)}
              {items.some((it) => isFinished(it.state)) && (
                <button type="button" onClick={clearFinished} className="min-h-11 rounded-full border border-line px-4 text-muted">Clear finished</button>
              )}
            </span>
          </div>
          <ul className="space-y-2">
            {items.map((it) => (
              <li key={it.id} className="glass-card rounded-lg border border-line bg-panel px-4 py-2 text-sm" data-state={it.state}>
                <div className="flex items-center justify-between gap-3">
                  <span className="min-w-0 truncate">{it.name} <span className="text-xs text-muted">{formatBytes(it.size)}</span></span>
                  <span className="flex shrink-0 items-center gap-3">
                    <span className={it.state === "error" ? "text-danger" : it.state === "done" ? "text-ok" : "text-muted"}>{statusText(it)}</span>
                    {!isFinished(it.state) && (
                      <button type="button" aria-label={`Cancel ${it.name}`} onClick={() => cancel(it.id)} className="grid min-h-11 min-w-11 place-items-center rounded-full text-muted hover:text-ink">✕</button>
                    )}
                  </span>
                </div>
                {(it.state === "uploading" || it.state === "paused" || it.state === "finishing") && (
                  <div className="mt-1 h-1 rounded bg-line"><div className="h-1 rounded bg-accent transition-[width]" style={{ width: `${(it.sent / it.size) * 100}%` }} /></div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <ProcessingCard />

      <section>
        <h2 className="mb-2 text-lg font-medium">Your uploads</h2>
        {mine.length === 0 ? <p className="text-sm text-muted">Nothing uploaded yet.</p> : <TrackList tracks={mine} showStatus onDelete={remove} />}
      </section>
    </div>
  );
}
