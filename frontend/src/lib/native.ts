/** The Android app runs this site inside its own window and exposes `window.NativeApp` to it (android/…/NativeBridge.java).
 *  On the web (or an iPhone) the object does not exist and everything here is a no-op. */
export interface NativeApp {
  isApp(): boolean;
  version(): string;
  updatePlayback(json: string): void;
  updateUpload(json: string): void;
  retry(): void;
}

declare global {
  interface Window {
    NativeApp?: NativeApp;
    /** The app calls this when a notification / lock-screen button is pressed. */
    __listensNative?: { onCommand: (command: string, argument?: number) => void };
  }
}

export function nativeApp(): NativeApp | null {
  return typeof window !== "undefined" && window.NativeApp ? window.NativeApp : null;
}

export const isNativeApp = () => nativeApp() !== null;

export interface PlaybackReport {
  active: boolean;
  title?: string;
  artist?: string;
  album?: string;
  artwork?: string;
  playing?: boolean;
  positionMs?: number;
  durationMs?: number;
  hasNext?: boolean;
  hasPrev?: boolean;
}

/** Decides when a playback report is worth sending. The position ticks ~4 times a second, but the phone extrapolates it
 *  on its own while playing, so only report a change of song or state, a seek (the position jumping away from where it
 *  should be), or a slow resync. */
export class PlaybackReporter {
  private lastKey = "";
  private lastPos = 0;
  private lastAt = 0;

  private readonly send: (report: PlaybackReport) => void;
  private readonly now: () => number;

  constructor(send: (report: PlaybackReport) => void, now: () => number = Date.now) {
    this.send = send;
    this.now = now;
  }

  report(r: PlaybackReport): void {
    const { positionMs = 0, ...rest } = r;
    const key = JSON.stringify(rest);
    const t = this.now();
    const expected = this.lastPos + (r.playing ? t - this.lastAt : 0);
    const jumped = Math.abs(positionMs - expected) > 2000;
    if (key !== this.lastKey || jumped || t - this.lastAt > 20_000) {
      this.lastKey = key;
      this.lastPos = positionMs;
      this.lastAt = t;
      this.send({ ...rest, positionMs });
    }
  }
}
