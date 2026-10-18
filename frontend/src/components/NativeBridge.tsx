"use client";

import { useEffect } from "react";
import { creditText } from "@/lib/credit";
import { nativeApp, PlaybackReporter } from "@/lib/native.ts";
import { currentTrack, usePlayer } from "@/store/player";
import { overall, useUploads } from "@/store/uploads";

/** Renders nothing. Inside the Android app: mirrors what is playing to the phone (notification, lock screen, Bluetooth) and
 *  carries the notification buttons back to the player. On the web it does nothing. */
export default function NativeBridge() {
  useEffect(() => {
    const app = nativeApp();
    if (!app) return;

    const reporter = new PlaybackReporter((r) => app.updatePlayback(JSON.stringify(r)));
    const push = () => {
      const s = usePlayer.getState();
      const t = currentTrack(s);
      if (!t) {
        reporter.report({ active: false });
        return;
      }
      const durationS = s.duration || (t.duration_ms ?? 0) / 1000;
      reporter.report({
        active: true,
        title: t.title,
        artist: creditText(t),
        album: t.album?.title ?? "",
        artwork: t.cover_url ? new URL(t.cover_url, location.href).href : "",
        playing: s.playing && !s.error,
        positionMs: Math.round(s.currentTime * 1000),
        durationMs: Math.round(durationS * 1000),
        hasNext: s.index + 1 < s.queue.length,
        hasPrev: s.index > 0,
      });
    };
    push();
    const unsubscribe = usePlayer.subscribe(push);

    // Upload progress for the notification (with its Pause / Resume button). Only changes are sent.
    let lastUpload = "";
    const pushUpload = () => {
      const { items, paused } = useUploads.getState();
      const o = overall(items);
      const label = `File ${Math.min(o.index, o.count)} of ${o.count} · ${o.percent}%`;
      const json = JSON.stringify({ active: o.active, paused, percent: o.percent, label });
      if (json !== lastUpload) {
        lastUpload = json;
        app.updateUpload(json);
      }
    };
    pushUpload();
    const unsubscribeUploads = useUploads.subscribe(pushUpload);

    window.__listensNative = {
      onCommand(command, argument) {
        if (command === "upload-pause") return useUploads.getState().pause();
        if (command === "upload-resume") return useUploads.getState().resume();
        const s = usePlayer.getState();
        switch (command) {
          case "play": s.setPlaying(true); break;
          case "pause": s.setPlaying(false); break;
          case "next": s.next(); break;
          case "prev": s.prev(); break;
          case "seek": if (typeof argument === "number") s.requestSeek(argument); break;
        }
      },
    };
    return () => {
      unsubscribe();
      unsubscribeUploads();
      delete window.__listensNative;
    };
  }, []);
  return null;
}
