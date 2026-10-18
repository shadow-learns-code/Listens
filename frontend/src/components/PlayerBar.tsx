"use client";

import Cover from "@/components/Cover";
import { creditText } from "@/lib/credit";
import Timeline from "@/components/Timeline";
import { LyricsIcon, MuteIcon, NextIcon, PauseIcon, PlayIcon, PrevIcon, QueueIcon, SimilarIcon, VolumeIcon } from "@/components/icons";
import LikeButton from "@/components/LikeButton";
import QualitySelect from "@/components/QualitySelect";
import { formatTime } from "@/lib/format";
import { useSwipe } from "@/lib/swipe";
import { currentTrack, usePlayer } from "@/store/player";

/** Persistent mini-player: lives in the (app) layout so it stays put (and keeps playing) across
 *  pages. On mobile this is deliberately slim - cover, title, play/pause, next - because six
 *  44px icon buttons (prev/play/next/lyrics/queue/similar) squeezed into the same row left
 *  *zero* room for the title (confirmed by screenshot: no text rendered at all). Every other
 *  control lives in NowPlayingSheet, one tap or swipe-up away. */
export default function PlayerBar() {
  const track = usePlayer(currentTrack);
  const p = usePlayer();
  const openNowPlaying = useSwipe({
    onTap: () => p.setNowPlayingOpen(true),
    onSwipeLeft: p.next,
    onSwipeRight: p.prev,
  });
  if (!track) return null;
  const duration = p.duration || (track.duration_ms ?? 0) / 1000;
  const hasNext = p.index + 1 < p.queue.length;

  return (
    <div
      data-testid="player-bar"
      className="glass-surface fixed inset-x-3 bottom-[calc(4.75rem+max(0px,env(safe-area-inset-bottom)-0.75rem))] z-30 overflow-hidden rounded-2xl md:bottom-3 md:left-[calc(var(--side)+1.5rem)] md:right-3 md:transition-[left] md:duration-300"
    >
      {/* thin progress line on mobile (the full slider is desktop-only) */}
      <div className="h-0.5 bg-white/15 md:hidden">
        <div className="h-full bg-accent transition-[width]" style={{ width: duration ? `${Math.min(100, (p.currentTime / duration) * 100)}%` : 0 }} />
      </div>
      <div
        data-testid="now-playing-trigger"
        role="button"
        tabIndex={0}
        aria-label="Open now playing"
        onPointerDown={openNowPlaying.onPointerDown}
        onPointerUp={openNowPlaying.onPointerUp}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && p.setNowPlayingOpen(true)}
        className="mx-auto grid max-w-6xl grid-cols-[1fr_auto] items-center gap-3 px-3 py-2 md:grid-cols-[1fr_minmax(320px,2fr)_1fr] md:px-4"
      >
        {/* Tapping anywhere on the bar opens Now Playing (mobile) or does nothing (desktop, where
            the sheet is md:hidden) - every actual control below stops propagation so its own
            click/tap isn't *also* treated as "open the sheet". */}
        <div className="flex min-w-0 items-center gap-3">
          <Cover id={track.id} url={track.cover_url} size={44} />
          <div className="min-w-0 flex-1">
            <div data-testid="now-title" className="truncate text-sm font-medium">{track.title}</div>
            {/* Plain text, not a link: tapping anywhere on the bar - title or artist - opens Now
                Playing, matching every mobile music app. A nested Link here would navigate away
                instead, which is exactly the bug this replaced. */}
            <div className="truncate text-xs text-muted">{creditText(track)}</div>
            {p.error && <div role="alert" data-testid="player-error" className="truncate text-xs text-danger">{p.error}</div>}
          </div>
          <div onPointerDown={(e) => e.stopPropagation()} onPointerUp={(e) => e.stopPropagation()}>
            <LikeButton trackId={track.id} />
          </div>
        </div>

        <div className="flex flex-col items-center gap-1">
          <div className="flex items-center gap-1">
            {/* size-11 = 44px, the WCAG/Material minimum touch target; these are the most-tapped controls in the app */}
            <button
              data-testid="prev" aria-label="Previous" onClick={p.prev}
              onPointerDown={(e) => e.stopPropagation()} onPointerUp={(e) => e.stopPropagation()}
              className="hidden size-11 items-center justify-center rounded-full text-muted hover:text-fg md:flex"
            >
              <PrevIcon />
            </button>
            <button
              data-testid="play-toggle"
              aria-label={p.playing ? "Pause" : "Play"}
              onClick={p.toggle}
              onPointerDown={(e) => e.stopPropagation()} onPointerUp={(e) => e.stopPropagation()}
              className="flex size-12 items-center justify-center rounded-full bg-fg text-ink transition-transform hover:scale-105 active:scale-95"
            >
              {p.playing ? <PauseIcon /> : <PlayIcon />}
            </button>
            <button
              data-testid="next" aria-label="Next" onClick={p.next} disabled={!hasNext}
              onPointerDown={(e) => e.stopPropagation()} onPointerUp={(e) => e.stopPropagation()}
              className="flex size-11 items-center justify-center rounded-full text-muted transition-opacity hover:text-fg disabled:opacity-30"
            >
              <NextIcon />
            </button>
          </div>
          <div
            onPointerDown={(e) => e.stopPropagation()} onPointerUp={(e) => e.stopPropagation()}
            className="hidden w-full items-center gap-2 font-mono text-xs tabular-nums text-muted md:flex"
          >
            <span data-testid="time-current" className="w-10 text-right">{formatTime(p.currentTime)}</span>
            <Timeline testId="seek" value={p.currentTime} max={duration} onSeek={p.requestSeek} className="flex-1" />
            <span data-testid="time-total" className="w-10">{formatTime(duration)}</span>
          </div>
        </div>

        <div
          onPointerDown={(e) => e.stopPropagation()} onPointerUp={(e) => e.stopPropagation()}
          className="hidden items-center justify-end gap-3 md:flex"
        >
          <QualitySelect />
          <button aria-label={p.muted ? "Unmute" : "Mute"} onClick={p.toggleMute} className="flex size-9 items-center justify-center text-muted hover:text-fg">
            {p.muted || p.volume === 0 ? <MuteIcon /> : <VolumeIcon />}
          </button>
          <input
            data-testid="volume"
            aria-label="Volume"
            type="range"
            min={0}
            max={1}
            step={0.01}
            value={p.muted ? 0 : p.volume}
            onChange={(e) => p.setVolume(Number(e.target.value))}
            className="h-1 w-24 cursor-pointer"
          />
          <button aria-label="Lyrics" data-testid="lyrics-toggle-desktop" onClick={() => p.setLyricsOpen(!p.lyricsOpen)} className={`flex size-9 items-center justify-center rounded-full hover:text-fg ${p.lyricsOpen ? "text-accent" : "text-muted"}`}>
            <LyricsIcon />
          </button>
          <button aria-label="Queue" data-testid="queue-toggle-desktop" onClick={() => p.setQueueOpen(!p.queueOpen)} className={`flex size-9 items-center justify-center rounded-full hover:text-fg ${p.queueOpen ? "text-accent" : "text-muted"}`}>
            <QueueIcon />
          </button>
          <button aria-label="Similar tracks" data-testid="similar-toggle-desktop" onClick={() => p.setSimilarOpen(!p.similarOpen)} className={`flex size-9 items-center justify-center rounded-full hover:text-fg ${p.similarOpen ? "text-accent" : "text-muted"}`}>
            <SimilarIcon />
          </button>
        </div>
      </div>
    </div>
  );
}
