"use client";

import { recordPlay } from "@/lib/recentPlays";
import { creditText } from "@/lib/credit";
import Hls, { type Level } from "hls.js";
import { useEffect, useRef } from "react";
import { recordEvent } from "@/lib/events";
import { getStreamUrl, invalidateStreamUrl, prefetchTrack } from "@/lib/stream";
import { currentTrack, hydratePlayerPrefs, type Quality, usePlayer } from "@/store/player";

interface PlaySession {
  trackId: string;
  accumulatedMs: number; // actively-playing time, not wall-clock time (paused/seeking don't count)
  lastMediaTime: number; // audio.currentTime as of the last tick, to compute the next delta
  lastKnownPositionMs: number; // for a skip event's position_ms
  endedNaturally: boolean; // set by the 'ended' handler; distinguishes play_end from skip
}

/** Rendition bitrate in kbps. We read it from the variant URL (".../q160/index.m3u8") because the
 *  BANDWIDTH attribute includes container overhead (e.g. 175 000 for a "160k" stream). */
function levelKbps(level: Level): number {
  const url = Array.isArray(level.url) ? level.url[0] : (level.url as unknown as string | undefined);
  const m = url?.match(/\/q(\d+)\//);
  return m ? Number(m[1]) : Math.round(level.bitrate / 1000);
}

function applyQuality(hls: Hls, kbps: number[], quality: Quality, immediate: boolean) {
  let idx = -1; // -1 = hls.js adaptive bitrate
  if (quality !== "auto") {
    idx = kbps.indexOf(quality);
    if (idx < 0) idx = Math.max(0, kbps.filter((k) => k <= quality).length - 1); // best available at or below
  }
  // currentLevel flushes the buffer (instant but may glitch); nextLevel switches at the next segment.
  if (immediate) hls.currentLevel = idx;
  else hls.nextLevel = idx;
}

function onPlayError(e: unknown) {
  if ((e as DOMException)?.name === "AbortError") return; // interrupted by a newer load: expected
  const st = usePlayer.getState();
  st.setPlaying(false);
  st.setError("Playback was blocked. Press play to start.");
}

/** Crossfade-lite: a volume ramp across the last/first `crossfadeSeconds` of a track, not true
 *  overlapping playback of two streams (that would need two full HLS/decoder pipelines running
 *  at once - out of scope here). Still a real, audible transition tied to the slider, just a
 *  fade rather than a blend. */
function applyFade(audio: HTMLAudioElement, fadeGain: GainNode, ctx: AudioContext) {
  const seconds = usePlayer.getState().crossfadeSeconds;
  if (seconds <= 0 || !Number.isFinite(audio.duration)) {
    if (fadeGain.gain.value !== 1) fadeGain.gain.setTargetAtTime(1, ctx.currentTime, 0.05);
    return;
  }
  const remaining = audio.duration - audio.currentTime;
  if (remaining <= seconds) fadeGain.gain.setTargetAtTime(Math.max(0, remaining / seconds), ctx.currentTime, 0.05);
  else if (audio.currentTime < seconds) fadeGain.gain.setTargetAtTime(Math.min(1, audio.currentTime / seconds), ctx.currentTime, 0.05);
  else if (fadeGain.gain.value !== 1) fadeGain.gain.setTargetAtTime(1, ctx.currentTime, 0.05);
}

/**
 * Owns the <audio> element and keeps it in sync with the Zustand player store. Mounted once in the
 * (app) layout, so playback survives client-side navigation between pages.
 */
export default function PlayerEngine() {
  const audioRef = useRef<HTMLAudioElement>(null);
  const hlsRef = useRef<Hls | null>(null);
  const switching = useRef(false); // ignore the <audio> 'pause' events caused by swapping sources
  const pendingSeek = useRef<number | null>(null);
  const lastPositionState = useRef(0);
  const session = useRef<PlaySession | null>(null);
  const prevTrackId = useRef<string | null>(null);
  // Web Audio graph: source -> [compressor if normalizing] -> [mono downmix if enabled] -> fadeGain -> destination
  const audioCtxRef = useRef<AudioContext | null>(null);
  const sourceRef = useRef<MediaElementAudioSourceNode | null>(null);
  const compressorRef = useRef<DynamicsCompressorNode | null>(null);
  const monoRef = useRef<GainNode | null>(null);
  const fadeGainRef = useRef<GainNode | null>(null);

  const track = usePlayer(currentTrack);
  const nextTrack = usePlayer((s) => s.queue[s.index + 1] ?? null);
  const queueLength = usePlayer((s) => s.queue.length);
  const playing = usePlayer((s) => s.playing);
  const volume = usePlayer((s) => s.volume);
  const muted = usePlayer((s) => s.muted);
  const quality = usePlayer((s) => s.quality);
  const levels = usePlayer((s) => s.levels);
  const seekRequest = usePlayer((s) => s.seekRequest);
  const monoAudio = usePlayer((s) => s.monoAudio);
  const volumeNormalization = usePlayer((s) => s.volumeNormalization);
  const gaplessPlayback = usePlayer((s) => s.gaplessPlayback);

  // --- one-time wiring of <audio> events -> store ------------------------------------------
  useEffect(() => {
    hydratePlayerPrefs();
    const audio = audioRef.current!;
    const st = usePlayer.getState;
    const handlers: Record<string, () => void> = {
      timeupdate: () => {
        st().setTime(audio.currentTime);
        if (audioCtxRef.current && fadeGainRef.current) applyFade(audio, fadeGainRef.current, audioCtxRef.current);
        const s = session.current;
        if (s) {
          // Only count time that was actually spent playing: clamp the per-tick delta so a
          // seek (which can jump currentTime by minutes in one tick) can't be counted as
          // "played". A paused tick still updates lastMediaTime so resuming doesn't produce
          // one huge catch-up delta.
          if (!audio.paused) s.accumulatedMs += Math.max(0, Math.min(audio.currentTime - s.lastMediaTime, 1.5)) * 1000;
          s.lastMediaTime = audio.currentTime;
          s.lastKnownPositionMs = Math.round(audio.currentTime * 1000);
        }
        const now = Date.now();
        if (now - lastPositionState.current > 1000 && "mediaSession" in navigator && Number.isFinite(audio.duration)) {
          lastPositionState.current = now;
          try {
            navigator.mediaSession.setPositionState({
              duration: audio.duration,
              position: Math.min(audio.currentTime, audio.duration),
              playbackRate: audio.playbackRate,
            });
          } catch {
            /* some browsers reject odd values */
          }
        }
      },
      durationchange: () => Number.isFinite(audio.duration) && st().setDuration(audio.duration),
      loadedmetadata: () => {
        switching.current = false;
        if (pendingSeek.current != null) {
          audio.currentTime = pendingSeek.current;
          pendingSeek.current = null;
        }
      },
      waiting: () => st().setBuffering(true),
      canplay: () => st().setBuffering(false),
      playing: () => {
        switching.current = false;
        st().setBuffering(false);
        st().setError(null);
      },
      // pause caused by something outside the app (headset unplugged, OS media key): reflect it
      pause: () => {
        if (!audio.ended && !switching.current && st().playing) st().setPlaying(false);
      },
      play: () => {
        if (!st().playing) st().setPlaying(true);
      },
      ended: () => {
        if (session.current) session.current.endedNaturally = true;
        // repeat-one: loop the same track rather than advancing (next() handles repeat-all's
        // end-of-queue wraparound - this is the one case that isn't "go to a different track").
        if (st().repeatMode === "one") {
          audio.currentTime = 0;
          audioCtxRef.current?.resume();
          audio.play().catch(onPlayError);
          if (session.current) {
            session.current.lastMediaTime = 0;
            session.current.endedNaturally = false;
          }
          return;
        }
        st().next();
      },
      error: () => {
        switching.current = false;
        if (audio.src) st().setError("Couldn't play this track.");
      },
    };
    for (const [ev, fn] of Object.entries(handlers)) audio.addEventListener(ev, fn);
    return () => {
      for (const [ev, fn] of Object.entries(handlers)) audio.removeEventListener(ev, fn);
      hlsRef.current?.destroy();
      audio.pause();
    };
  }, []);

  // --- Web Audio graph: built once, rewired (not rebuilt) when mono/normalization toggle -------
  // createMediaElementSource can only ever be called once per <audio> element, so this effect's
  // empty deps array matters: it must run exactly once for the lifetime of this persistent player.
  useEffect(() => {
    const audio = audioRef.current!;
    const AudioCtx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AudioCtx) return; // very old browser: play back without the enhancement chain, not at all
    const ctx = new AudioCtx();
    const source = ctx.createMediaElementSource(audio);
    const compressor = ctx.createDynamicsCompressor(); // approximates "volume normalisation"
    const mono = ctx.createGain();
    mono.channelCount = 1;
    mono.channelCountMode = "explicit"; // forces the stereo input down to 1 channel per the Web Audio spec
    mono.channelInterpretation = "speakers";
    const fadeGain = ctx.createGain(); // crossfade-lite (see applyFade)
    audioCtxRef.current = ctx;
    sourceRef.current = source;
    compressorRef.current = compressor;
    monoRef.current = mono;
    fadeGainRef.current = fadeGain;
    return () => {
      ctx.close().catch(() => {});
    };
  }, []);

  // Rewires the graph when a toggle changes (cheap: at most two extra node hops, done rarely).
  useEffect(() => {
    const ctx = audioCtxRef.current;
    const source = sourceRef.current, compressor = compressorRef.current, mono = monoRef.current, fadeGain = fadeGainRef.current;
    if (!ctx || !source || !compressor || !mono || !fadeGain) return;
    source.disconnect();
    compressor.disconnect();
    mono.disconnect();
    fadeGain.disconnect();
    let node: AudioNode = source;
    if (volumeNormalization) {
      node.connect(compressor);
      node = compressor;
    }
    if (monoAudio) {
      node.connect(mono);
      node = mono;
    }
    node.connect(fadeGain);
    fadeGain.connect(ctx.destination);
  }, [monoAudio, volumeNormalization]);

  // --- load the current track ---------------------------------------------------------------
  useEffect(() => {
    const audio = audioRef.current!;
    const st = usePlayer.getState;
    hlsRef.current?.destroy();
    hlsRef.current = null;
    if (!track) {
      audio.removeAttribute("src");
      audio.load();
      st().setLevels([]);
      return;
    }
    let cancelled = false;
    switching.current = true;
    pendingSeek.current = null;
    st().setLevels([]);
    st().setDuration((track.duration_ms ?? 0) / 1000);
    st().setBuffering(true);

    const attach = (url: string, startPosition = -1) => {
      if (Hls.isSupported()) {
        hlsRef.current?.destroy();
        const hls = new Hls({
          // assume a good connection so "auto" starts at the top rendition; ABR then adapts down
          abrEwmaDefaultEstimate: 5_000_000,
          maxBufferLength: 30,
          startPosition,
        });
        hlsRef.current = hls;
        hls.on(Hls.Events.MANIFEST_PARSED, (_e, data) => {
          const kbps = data.levels.map(levelKbps);
          st().setLevels(kbps);
          applyQuality(hls, kbps, st().quality, true);
        });
        let mediaRecoveries = 0;
        hls.on(Hls.Events.ERROR, (_e, data) => {
          if (!data.fatal || cancelled) return;
          const code = (data as { response?: { code?: number } }).response?.code;
          if (code === 403) {
            // the signed URL expired mid-track: mint a new one and resume from where we were
            const resumeAt = audio.currentTime;
            invalidateStreamUrl(track.id);
            getStreamUrl(track.id)
              .then((fresh) => !cancelled && attach(fresh, resumeAt))
              .catch(() => st().setError("Stream link expired. Try playing the track again."));
          } else if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
          else if (data.type === Hls.ErrorTypes.MEDIA_ERROR && ++mediaRecoveries <= 2) hls.recoverMediaError();
          else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
            // retries didn't help: the browser can't decode this stream (e.g. a build without AAC)
            hls.destroy();
            st().setBuffering(false);
            st().setPlaying(false);
            st().setError("Your browser can't decode this audio format.");
          } else st().setError("Couldn't play this track.");
        });
        hls.loadSource(url);
        hls.attachMedia(audio);
      } else if (audio.canPlayType("application/vnd.apple.mpegurl")) {
        audio.src = url; // Safari / iOS: native HLS (no quality control available)
        if (startPosition > 0) pendingSeek.current = startPosition;
      } else {
        st().setError("This browser can't play HLS audio.");
        return false;
      }
      return true;
    };

    (async () => {
      try {
        const url = await getStreamUrl(track.id);
        if (cancelled) return;
        if (attach(url) && st().playing) {
          audioCtxRef.current?.resume();
          await audio.play().catch(onPlayError);
        }
      } catch {
        if (!cancelled) {
          switching.current = false;
          st().setBuffering(false);
          st().setError("Couldn't load this track.");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [track?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // --- event lifecycle: close out the outgoing track (play_end or skip), open the next one -------------
  // play_start drives "recently played"/Recents; play_end with ms_played >= 30s drives Listening
  // stats' top track (see services/catalogue/app/analytics.py). Whether the outgoing track's close event
  // is "play_end" or "skip" is decided by session.endedNaturally, set by the 'ended' handler above;
  // the skip *reason* comes from the store (set by next()/prev()/jumpTo()/playQueue()/removeAt()).
  useEffect(() => {
    const prev = prevTrackId.current;
    const closing = session.current;
    if (prev && closing && closing.trackId === prev) {
      if (closing.endedNaturally) {
        recordEvent({ event_type: "play_end", track_id: prev, ms_played: Math.round(closing.accumulatedMs) });
      } else {
        const reason = usePlayer.getState().lastSkipReason ?? "track_select";
        recordEvent({ event_type: "skip", track_id: prev, position_ms: closing.lastKnownPositionMs, reason });
      }
    }
    if (track) {
      session.current = { trackId: track.id, accumulatedMs: 0, lastMediaTime: 0, lastKnownPositionMs: 0, endedNaturally: false };
      recordEvent({ event_type: "play_start", track_id: track.id });
      recordPlay(track.id);
    } else {
      session.current = null;
    }
    prevTrackId.current = track?.id ?? null;
    usePlayer.setState({ lastSkipReason: null }); // consumed; the next transition starts clean
  }, [track?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // --- play/pause intent ----------------------------------------------------------------------
  useEffect(() => {
    const audio = audioRef.current!;
    if (!audio.src && !hlsRef.current) return; // nothing loaded yet; the load effect starts playback
    if (playing) {
      audioCtxRef.current?.resume();
      audio.play().catch(onPlayError);
    } else audio.pause();
  }, [playing]);

  // --- volume, seek, quality --------------------------------------------------------------------
  useEffect(() => {
    const audio = audioRef.current!;
    audio.volume = volume;
    audio.muted = muted;
  }, [volume, muted]);

  useEffect(() => {
    if (!seekRequest) return;
    const audio = audioRef.current!;
    if (audio.readyState > 0) audio.currentTime = seekRequest.to;
    else pendingSeek.current = seekRequest.to;
  }, [seekRequest]);

  useEffect(() => {
    if (hlsRef.current && levels.length) applyQuality(hlsRef.current, levels, quality, false);
  }, [quality, levels]);

  // --- prefetch the next track so "next" is instant -----------------------------------------------
  // "Gapless playback" can't be true sample-accurate gaplessness across two independently
  // transcoded HLS streams without a second decode pipeline (out of scope - see applyFade's
  // comment for the same trade-off on crossfade) - but prefetching the next track's manifest
  // and first segment *immediately* instead of waiting noticeably shortens the real gap, which
  // is what this toggle actually controls.
  useEffect(() => {
    if (!nextTrack) return;
    const t = setTimeout(() => {
      prefetchTrack(nextTrack.id, usePlayer.getState().quality).catch(() => {});
    }, gaplessPlayback ? 0 : 3000);
    return () => clearTimeout(t);
  }, [nextTrack?.id, track?.id, gaplessPlayback]); // eslint-disable-line react-hooks/exhaustive-deps

  // --- endless play: top the queue up while a few songs are still left, so there is never a gap --------------------
  useEffect(() => {
    if (track) void usePlayer.getState().refillQueue();
  }, [track?.id, queueLength]); // eslint-disable-line react-hooks/exhaustive-deps

  // --- lock-screen / media-key integration + tab title ----------------------------------------------
  useEffect(() => {
    if (!track) {
      document.title = "Shadow Listens";
      return;
    }
    document.title = `${track.title} · ${creditText(track)}`;
    if (!("mediaSession" in navigator)) return;
    navigator.mediaSession.metadata = new MediaMetadata({
      title: track.title,
      artist: creditText(track),
      album: track.album?.title ?? "",
      artwork: track.cover_url ? [{ src: new URL(track.cover_url, location.href).href, sizes: "600x600", type: "image/jpeg" }] : [],
    });
    const st = usePlayer.getState;
    const audio = audioRef.current!;
    const actions: [MediaSessionAction, MediaSessionActionHandler][] = [
      ["play", () => st().setPlaying(true)],
      ["pause", () => st().setPlaying(false)],
      ["previoustrack", () => st().prev()],
      ["nexttrack", () => st().next()],
      ["seekto", (d) => d.seekTime != null && st().requestSeek(d.seekTime)],
      ["seekbackward", () => st().requestSeek(Math.max(0, audio.currentTime - 10))],
      ["seekforward", () => st().requestSeek(audio.currentTime + 10)],
    ];
    for (const [a, h] of actions) {
      try {
        navigator.mediaSession.setActionHandler(a, h);
      } catch {
        /* unsupported action */
      }
    }
  }, [track?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if ("mediaSession" in navigator) navigator.mediaSession.playbackState = playing ? "playing" : "paused";
  }, [playing]);

  // --- keyboard shortcuts: space (play/pause), left/right (prev/next), up/down (volume) ------------
  useEffect(() => {
    const VOLUME_STEP = 0.05;
    function isTyping(el: EventTarget | null) {
      if (!(el instanceof HTMLElement)) return false;
      return el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.ctrlKey || e.metaKey || e.altKey || isTyping(e.target)) return;
      const st = usePlayer.getState();
      switch (e.key) {
        case " ":
        case "Spacebar":
          e.preventDefault(); // stop the page from scrolling
          st.toggle();
          break;
        case "ArrowRight":
          e.preventDefault();
          st.next();
          break;
        case "ArrowLeft":
          e.preventDefault();
          st.prev();
          break;
        case "ArrowUp":
          e.preventDefault();
          st.setVolume(Math.min(1, st.volume + VOLUME_STEP));
          break;
        case "ArrowDown":
          e.preventDefault();
          st.setVolume(Math.max(0, st.volume - VOLUME_STEP));
          break;
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return <audio ref={audioRef} preload="auto" data-testid="audio" className="hidden" />;
}
