"use client";

import { SoundBars } from "@/components/Loaders";
import { useNet } from "@/store/net";

/** Shown while a request has been pending for several seconds. On free hosting an idle server sleeps and takes
 *  20-30 s to wake on the first request, which otherwise looks like a frozen page. There is no real progress to
 *  report, so it shows elapsed time and a sweeping bar rather than a fake percentage. */
export default function WakingBanner() {
  const waitedMs = useNet((s) => s.waitedMs);
  if (waitedMs <= 0) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="waking-banner"
      className="glass-surface pointer-events-none fixed inset-x-3 top-[max(0.75rem,env(safe-area-inset-top))] z-[60] mx-auto max-w-md animate-fade-in overflow-hidden rounded-2xl px-4 py-3 md:top-5"
    >
      <div className="flex items-center gap-3">
        <SoundBars size={22} label="Waking the server" />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium">Waking up the server</div>
          <div className="text-xs text-muted">It sleeps when idle, so the first load can take up to 30 seconds.</div>
        </div>
        <span className="shrink-0 font-mono text-xs tabular-nums text-muted">{Math.round(waitedMs / 1000)}s</span>
      </div>
      <div aria-hidden className="indeterminate mt-3 h-0.5 rounded-full bg-white/10" />
    </div>
  );
}
