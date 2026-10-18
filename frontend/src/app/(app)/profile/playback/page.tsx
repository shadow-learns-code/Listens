"use client";

import { SettingsPage, SettingsSection, SettingsRow } from "@/components/SettingsPage";
import Toggle from "@/components/Toggle";
import { usePlayer } from "@/store/player";

export default function PlaybackPage() {
  const p = usePlayer();

  return (
    <SettingsPage title="Playback">
      <SettingsSection title="Track transitions">
        <SettingsRow
          label="Gapless playback"
          description="Removes the pause between tracks."
          action={<Toggle checked={p.gaplessPlayback} onChange={p.setGaplessPlayback} label="Gapless playback" />}
        />
        <div className="px-4 py-3">
          <div className="mb-2 flex items-center justify-between text-sm">
            <span>Crossfade</span>
            <span className="font-mono text-xs text-muted">{p.crossfadeSeconds}s</span>
          </div>
          <input
            aria-label="Crossfade seconds"
            type="range"
            min={0}
            max={12}
            step={1}
            value={p.crossfadeSeconds}
            onChange={(e) => p.setCrossfadeSeconds(Number(e.target.value))}
            className="h-1 w-full cursor-pointer"
          />
          <p className="mt-1 text-xs text-muted">Crossfades the end of a track into the next.</p>
        </div>
      </SettingsSection>

      <SettingsSection title="Volume">
        <SettingsRow
          label="Volume normalisation"
          description="Runs a dynamics compressor so quiet and loud tracks sound closer in level."
          action={<Toggle checked={p.volumeNormalization} onChange={p.setVolumeNormalization} label="Volume normalisation" />}
        />
        <SettingsRow
          label="Mono audio"
          description="Down-mixes to a single channel - useful on a single mono speaker."
          action={<Toggle checked={p.monoAudio} onChange={p.setMonoAudio} label="Mono audio" />}
        />
      </SettingsSection>

      <SettingsSection title="Listening controls">
        <SettingsRow
          label="Endless play"
          description="Never stop: a few songs before the queue ends, add similar-mood songs (never ones you just heard), mixed with the smart shuffle."
          action={<Toggle checked={p.autoplaySimilar} onChange={p.setAutoplaySimilar} label="Endless play" />}
        />
      </SettingsSection>

      <SettingsSection title="Shuffle" description="Picks the shuffle algorithm used by the shuffle button in Now Playing.">
        <label className="flex cursor-pointer items-start gap-3 px-4 py-3">
          <input type="radio" name="shuffle-style" checked={p.shuffleStyle === "smart"} onChange={() => p.setShuffleStyle("smart")} className="mt-1" />
          <span>
            <span className="block text-sm">Smart</span>
            <span className="block text-xs text-muted">Plays your liked songs a little more often, avoids what you just heard, keeps the same artist or film from repeating, and flows between songs of similar energy so a soft melody is not followed by a loud beat.</span>
          </span>
        </label>
        <label className="flex cursor-pointer items-start gap-3 px-4 py-3">
          <input type="radio" name="shuffle-style" checked={p.shuffleStyle === "standard"} onChange={() => p.setShuffleStyle("standard")} className="mt-1" />
          <span>
            <span className="block text-sm">Standard</span>
            <span className="block text-xs text-muted">Shuffles tracks in a fully random order; same artist can land back to back.</span>
          </span>
        </label>
      </SettingsSection>
    </SettingsPage>
  );
}
