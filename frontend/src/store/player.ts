import { create } from "zustand";
import { get as apiGet } from "@/lib/api";
import { BATCH, continuation, REFILL_AT, upcomingCount } from "@/lib/endless";
import { getRecentPlays } from "@/lib/recentPlays";
import { smartShuffle } from "@/lib/shuffle";
import type { Track } from "@/lib/types";
import { useLikes } from "@/store/likes";

export type Quality = "auto" | number; // number = kbps (96 / 160 / 320)
export type RepeatMode = "off" | "all" | "one";
export type ShuffleStyle = "standard" | "smart";

const PREFS_KEY = "listens.player";
let inflight: Promise<number> | null = null; // the refill in progress, if any (shared so the end of the queue can wait for it)

interface Prefs {
  volume: number;
  quality: Quality;
  monoAudio: boolean;
  volumeNormalization: boolean;
  crossfadeSeconds: number;
  gaplessPlayback: boolean;
  autoplaySimilar: boolean;
  shuffleStyle: ShuffleStyle;
  dataSaver: boolean;
  defaultPlaylistPublic: boolean;
  compactRows: boolean;
  followSystemTransparency: boolean;
  notifyPush: boolean;
  notifyEmail: boolean;
}

const DEFAULT_PREFS: Prefs = {
  volume: 0.8,
  quality: "auto",
  monoAudio: false,
  volumeNormalization: false,
  crossfadeSeconds: 0,
  gaplessPlayback: true,
  autoplaySimilar: true,
  shuffleStyle: "smart",
  dataSaver: false,
  defaultPlaylistPublic: true,
  compactRows: false,
  followSystemTransparency: false,
  notifyPush: true,
  notifyEmail: true,
};

function loadPrefs(): Prefs {
  try {
    const p = JSON.parse(localStorage.getItem(PREFS_KEY) ?? "{}");
    return {
      volume: typeof p.volume === "number" ? Math.min(1, Math.max(0, p.volume)) : DEFAULT_PREFS.volume,
      quality: p.quality === "auto" || typeof p.quality === "number" ? p.quality : DEFAULT_PREFS.quality,
      monoAudio: typeof p.monoAudio === "boolean" ? p.monoAudio : DEFAULT_PREFS.monoAudio,
      volumeNormalization: typeof p.volumeNormalization === "boolean" ? p.volumeNormalization : DEFAULT_PREFS.volumeNormalization,
      crossfadeSeconds: typeof p.crossfadeSeconds === "number" ? Math.min(12, Math.max(0, p.crossfadeSeconds)) : DEFAULT_PREFS.crossfadeSeconds,
      gaplessPlayback: typeof p.gaplessPlayback === "boolean" ? p.gaplessPlayback : DEFAULT_PREFS.gaplessPlayback,
      autoplaySimilar: typeof p.autoplaySimilar === "boolean" ? p.autoplaySimilar : DEFAULT_PREFS.autoplaySimilar,
      // stored as "shuffleMode": the old "shuffleStyle" key held a different pair of values and its "standard" was just the old default
      shuffleStyle: p.shuffleMode === "standard" ? "standard" : DEFAULT_PREFS.shuffleStyle,
      dataSaver: typeof p.dataSaver === "boolean" ? p.dataSaver : DEFAULT_PREFS.dataSaver,
      defaultPlaylistPublic: typeof p.defaultPlaylistPublic === "boolean" ? p.defaultPlaylistPublic : DEFAULT_PREFS.defaultPlaylistPublic,
      compactRows: typeof p.compactRows === "boolean" ? p.compactRows : DEFAULT_PREFS.compactRows,
      followSystemTransparency: typeof p.followSystemTransparency === "boolean" ? p.followSystemTransparency : DEFAULT_PREFS.followSystemTransparency,
      notifyPush: typeof p.notifyPush === "boolean" ? p.notifyPush : DEFAULT_PREFS.notifyPush,
      notifyEmail: typeof p.notifyEmail === "boolean" ? p.notifyEmail : DEFAULT_PREFS.notifyEmail,
    };
  } catch {
    return DEFAULT_PREFS;
  }
}

function savePrefs(p: Prefs) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify({ ...p, shuffleMode: p.shuffleStyle }));
  } catch {
    /* ignore: storage unavailable (private mode) - prefs just won't survive a reload */
  }
}

/** Fisher-Yates: every order equally likely. */
function plainShuffle<T>(items: T[]): T[] {
  const out = [...items];
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
}

interface PlayerState {
  queue: Track[];
  index: number; // -1 when the queue is empty
  playing: boolean; // user intent; PlayerEngine makes the <audio> element follow it
  currentTime: number;
  duration: number;
  buffering: boolean;
  error: string | null;
  volume: number;
  muted: boolean;
  quality: Quality;
  levels: number[]; // kbps renditions the current track offers (empty until its manifest loads)
  queueOpen: boolean;
  lyricsOpen: boolean;
  similarOpen: boolean;
  nowPlayingOpen: boolean;
  shuffle: boolean;
  preShuffleQueue: Track[] | null; // set while shuffle is on, so turning it off can restore order
  repeatMode: RepeatMode;
  seekRequest: { to: number; n: number } | null;
  /** Set by whichever store action caused the most recent track change, read (and cleared)
   *  by PlayerEngine to tag the outgoing track's analytics event as a "skip" with a reason,
   *  vs. a natural play_end. Null means "no deliberate change happened" (e.g. first track). */
  lastSkipReason: string | null;

  // --- Settings (frontend/src/app/(app)/profile/*), persisted to localStorage ---------------
  monoAudio: boolean; // Playback: down-mix to a single channel (Web Audio, see PlayerEngine)
  volumeNormalization: boolean; // Playback: a dynamics compressor evens out loud/quiet tracks
  crossfadeSeconds: number; // Playback: fade out/in across a track boundary (0 = off, max 12s)
  gaplessPlayback: boolean; // Playback: prefetch the next track more aggressively
  autoplaySimilar: boolean; // Playback: keep playing similar tracks once the queue runs out
  shuffleStyle: ShuffleStyle; // Playback: shuffle algorithm (see lib/shuffle.ts)
  dataSaver: boolean; // Data-saving: forces the lowest quality rendition
  defaultPlaylistPublic: boolean; // Privacy: default visibility for newly created playlists
  compactRows: boolean; // Content & display: denser track-list rows
  followSystemTransparency: boolean; // Content & display: obey the OS "reduce transparency" setting (off = always show glass)
  notifyPush: boolean; // Notifications: local-only preference (no push infra exists yet)
  notifyEmail: boolean; // Notifications: local-only preference (no email infra exists yet)

  playQueue: (tracks: Track[], startIndex?: number) => void;
  addToQueue: (track: Track) => void;
  playNext: (track: Track) => void;
  removeAt: (i: number) => void;
  jumpTo: (i: number) => void;
  next: () => void;
  /** Endless play: add similar songs when few are left (see lib/endless.ts). Resolves to how many were added. */
  refillQueue: (force?: boolean) => Promise<number>;
  prev: () => void;
  toggle: () => void;
  setPlaying: (p: boolean) => void;
  requestSeek: (seconds: number) => void;
  setVolume: (v: number) => void;
  toggleMute: () => void;
  setQuality: (q: Quality) => void;
  setQueueOpen: (open: boolean) => void;
  setLyricsOpen: (open: boolean) => void;
  setSimilarOpen: (open: boolean) => void;
  setNowPlayingOpen: (open: boolean) => void;
  toggleShuffle: () => void;
  cycleRepeat: () => void;
  setMonoAudio: (v: boolean) => void;
  setVolumeNormalization: (v: boolean) => void;
  setCrossfadeSeconds: (v: number) => void;
  setGaplessPlayback: (v: boolean) => void;
  setAutoplaySimilar: (v: boolean) => void;
  setShuffleStyle: (v: ShuffleStyle) => void;
  setDataSaver: (v: boolean) => void;
  setDefaultPlaylistPublic: (v: boolean) => void;
  setCompactRows: (v: boolean) => void;
  setFollowSystemTransparency: (v: boolean) => void;
  setNotifyPush: (v: boolean) => void;
  setNotifyEmail: (v: boolean) => void;
  // engine -> store
  setTime: (t: number) => void;
  setDuration: (d: number) => void;
  setBuffering: (b: boolean) => void;
  setError: (e: string | null) => void;
  setLevels: (l: number[]) => void;
}

function persistPrefs(s: PlayerState) {
  savePrefs({
    volume: s.volume, quality: s.quality, monoAudio: s.monoAudio, volumeNormalization: s.volumeNormalization,
    crossfadeSeconds: s.crossfadeSeconds, gaplessPlayback: s.gaplessPlayback, autoplaySimilar: s.autoplaySimilar,
    shuffleStyle: s.shuffleStyle, dataSaver: s.dataSaver, defaultPlaylistPublic: s.defaultPlaylistPublic,
    compactRows: s.compactRows, followSystemTransparency: s.followSystemTransparency, notifyPush: s.notifyPush, notifyEmail: s.notifyEmail,
  });
}

export const usePlayer = create<PlayerState>((set, get) => ({
  queue: [],
  index: -1,
  playing: false,
  currentTime: 0,
  duration: 0,
  buffering: false,
  error: null,
  muted: false,
  levels: [],
  queueOpen: false,
  lyricsOpen: false,
  similarOpen: false,
  nowPlayingOpen: false,
  shuffle: false,
  preShuffleQueue: null,
  repeatMode: "off",
  seekRequest: null,
  lastSkipReason: null,
  ...DEFAULT_PREFS,

  playQueue: (tracks, startIndex = 0) =>
    set((s) => {
      const current = s.index >= 0 ? s.queue[s.index] : null;
      const next = tracks.length ? tracks[startIndex] : null;
      const reason = current && next && current.id !== next.id ? "track_select" : s.lastSkipReason;
      return { queue: tracks, index: tracks.length ? startIndex : -1, playing: tracks.length > 0, currentTime: 0, error: null, lastSkipReason: reason };
    }),
  addToQueue: (track) =>
    set((s) => (s.queue.length ? { queue: [...s.queue, track] } : { queue: [track], index: 0, playing: true, currentTime: 0 })),
  playNext: (track) =>
    set((s) => {
      if (!s.queue.length) return { queue: [track], index: 0, playing: true, currentTime: 0 };
      const q = [...s.queue];
      q.splice(s.index + 1, 0, track);
      return { queue: q };
    }),
  removeAt: (i) =>
    set((s) => {
      const queue = s.queue.filter((_, k) => k !== i);
      if (!queue.length) return { queue, index: -1, playing: false };
      // removing before the current track shifts it left; removing the current one lets the next slide in
      const index = i < s.index ? s.index - 1 : Math.min(s.index, queue.length - 1);
      return { queue, index, lastSkipReason: i === s.index ? "track_select" : s.lastSkipReason };
    }),
  jumpTo: (i) =>
    set((s) => (i >= 0 && i < s.queue.length ? { index: i, playing: true, currentTime: 0, error: null, lastSkipReason: "track_select" } : s)),
  next: () => {
    const s = get();
    if (s.index + 1 < s.queue.length) {
      set({ index: s.index + 1, playing: true, currentTime: 0, error: null, lastSkipReason: "next_button" });
      return;
    }
    if (s.repeatMode === "all" && s.queue.length) {
      set({ index: 0, playing: true, currentTime: 0, error: null, lastSkipReason: "next_button" });
      return;
    }
    if (s.autoplaySimilar && s.queue.length) {
      // the queue ran out before the early refill finished (or it failed): add songs now and carry straight on
      get().refillQueue(true).then((added) => {
        const s2 = get();
        if (added && s2.index + 1 < s2.queue.length) set({ index: s2.index + 1, playing: true, currentTime: 0, error: null, lastSkipReason: "next_button" });
        else set({ playing: false });
      });
      return;
    }
    set({ playing: false }); // end of queue: stop
  },
  refillQueue: (force = false) => {
    if (inflight) return inflight; // already fetching: callers (e.g. the end of the queue) wait for that one
    const s = get();
    if (!s.autoplaySimilar || !s.queue.length || s.index < 0) return Promise.resolve(0);
    if (!force && upcomingCount(s.queue.length, s.index) > REFILL_AT) return Promise.resolve(0);
    inflight = (async () => {
      const picked = await continuation(
        s.queue, s.index,
        { similar: (id, limit) => apiGet<Track[]>(`/api/catalogue/tracks/${id}/similar?limit=${limit}`), madeForYou: (limit) => apiGet<Track[]>(`/api/catalogue/home/made-for-you?limit=${limit}`) },
        { style: s.shuffleStyle, liked: useLikes.getState().ids, recentlyPlayed: getRecentPlays(), limit: BATCH },
      );
      if (!picked.length) return 0;
      set((s2) => ({ queue: [...s2.queue, ...picked], preShuffleQueue: s2.preShuffleQueue ? [...s2.preShuffleQueue, ...picked] : null }));
      return picked.length;
    })().finally(() => {
      inflight = null;
    });
    return inflight;
  },
  prev: () => {
    const s = get();
    // like most players: past the first 3 s "previous" restarts the track instead of going back
    if (s.currentTime > 3 || s.index <= 0) get().requestSeek(0);
    else set({ index: s.index - 1, playing: true, currentTime: 0, error: null, lastSkipReason: "previous_button" });
  },
  toggle: () => set((s) => (s.index >= 0 ? { playing: !s.playing } : s)),
  setPlaying: (playing) => set({ playing }),
  requestSeek: (to) => set((s) => ({ seekRequest: { to, n: (s.seekRequest?.n ?? 0) + 1 }, currentTime: to })),
  setVolume: (volume) => {
    set({ volume, muted: false });
    persistPrefs(get());
  },
  toggleMute: () => set((s) => ({ muted: !s.muted })),
  setQuality: (quality) => {
    set({ quality });
    persistPrefs(get());
  },
  // Mutually exclusive: all three are fixed inset-y-0 side panels, so more than one open at once would overlap.
  setQueueOpen: (queueOpen) => set({ queueOpen, lyricsOpen: queueOpen ? false : get().lyricsOpen, similarOpen: queueOpen ? false : get().similarOpen }),
  setLyricsOpen: (lyricsOpen) => set({ lyricsOpen, queueOpen: lyricsOpen ? false : get().queueOpen, similarOpen: lyricsOpen ? false : get().similarOpen }),
  setSimilarOpen: (similarOpen) => set({ similarOpen, queueOpen: similarOpen ? false : get().queueOpen, lyricsOpen: similarOpen ? false : get().lyricsOpen }),
  // Independent of the three above: the mobile full-screen "Now Playing" sheet sits *below*
  // them in z-order, so opening Queue/Lyrics/Similar from inside it just layers on top.
  setNowPlayingOpen: (nowPlayingOpen) => set({ nowPlayingOpen }),
  toggleShuffle: () =>
    set((s) => {
      if (s.shuffle) {
        // restore the pre-shuffle order, keeping whatever's currently playing as current
        const restored = s.preShuffleQueue ?? s.queue;
        const current = s.queue[s.index];
        const newIndex = current ? restored.findIndex((t) => t.id === current.id) : s.index;
        return { shuffle: false, queue: restored, index: newIndex < 0 ? s.index : newIndex, preShuffleQueue: null };
      }
      // shuffle only the upcoming portion - history and the current track stay put
      const before = s.queue.slice(0, s.index + 1);
      const after = s.queue.slice(s.index + 1);
      const shuffled = s.shuffleStyle === "smart"
        ? smartShuffle(after, s.queue[s.index] ?? null, { liked: useLikes.getState().ids, recentlyPlayed: getRecentPlays() })
        : plainShuffle(after);
      return { shuffle: true, preShuffleQueue: s.queue, queue: [...before, ...shuffled] };
    }),
  cycleRepeat: () =>
    set((s) => ({ repeatMode: s.repeatMode === "off" ? "all" : s.repeatMode === "all" ? "one" : "off" })),
  setMonoAudio: (monoAudio) => {
    set({ monoAudio });
    persistPrefs(get());
  },
  setVolumeNormalization: (volumeNormalization) => {
    set({ volumeNormalization });
    persistPrefs(get());
  },
  setCrossfadeSeconds: (crossfadeSeconds) => {
    set({ crossfadeSeconds: Math.min(12, Math.max(0, crossfadeSeconds)) });
    persistPrefs(get());
  },
  setGaplessPlayback: (gaplessPlayback) => {
    set({ gaplessPlayback });
    persistPrefs(get());
  },
  setAutoplaySimilar: (autoplaySimilar) => {
    set({ autoplaySimilar });
    persistPrefs(get());
  },
  setShuffleStyle: (shuffleStyle) => {
    set({ shuffleStyle });
    persistPrefs(get());
  },
  setDataSaver: (dataSaver) => {
    // Data saver takes effect immediately, like a real one would - it wouldn't be much of a
    // "saver" if it only applied starting with your next manual quality change.
    set({ dataSaver, quality: dataSaver ? 96 : get().quality });
    persistPrefs(get());
  },
  setDefaultPlaylistPublic: (defaultPlaylistPublic) => {
    set({ defaultPlaylistPublic });
    persistPrefs(get());
  },
  setCompactRows: (compactRows) => {
    set({ compactRows });
    persistPrefs(get());
  },
  setFollowSystemTransparency: (followSystemTransparency) => {
    set({ followSystemTransparency });
    persistPrefs(get());
  },
  setNotifyPush: (notifyPush) => {
    set({ notifyPush });
    persistPrefs(get());
  },
  setNotifyEmail: (notifyEmail) => {
    set({ notifyEmail });
    persistPrefs(get());
  },
  setTime: (currentTime) => set({ currentTime }),
  setDuration: (duration) => set({ duration }),
  setBuffering: (buffering) => set({ buffering }),
  setError: (error) => set({ error }),
  setLevels: (levels) => set({ levels }),
}));

export const currentTrack = (s: PlayerState): Track | null => (s.index >= 0 ? s.queue[s.index] ?? null : null);

/** Call once on the client to restore saved preferences. */
export function hydratePlayerPrefs() {
  usePlayer.setState(loadPrefs());
}
