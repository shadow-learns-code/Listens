"use client";

import { DevicesIcon, RecentsIcon, SettingsIcon, StatsIcon } from "@/components/icons";
import { SettingsRow } from "@/components/SettingsPage";
import { useAuth } from "@/store/auth";

/** The "tap your avatar" quick menu. Real multi-account switching and a paid-plan tier don't
 *  exist in this self-hosted app, so - unlike the reference this mirrors - there's no "Add
 *  account" or "Your Premium" row here: faking a subscription system would be actively
 *  misleading, not just incomplete. Listening stats and Recents are real, backed by actual
 *  play_events. */
export default function ProfilePage() {
  const { user } = useAuth();

  return (
    <div className="space-y-8">
      <header className="flex items-center gap-4">
        <div className="flex size-16 shrink-0 items-center justify-center rounded-full bg-raise text-2xl font-semibold text-accent">
          {user?.display_name?.[0]?.toUpperCase() ?? "?"}
        </div>
        <div className="min-w-0">
          <h1 className="truncate text-xl font-semibold">{user?.display_name}</h1>
          <p className="truncate text-sm text-muted">@{user?.username}</p>
        </div>
      </header>

      <nav aria-label="Profile menu" className="divide-y divide-line/60 rounded-xl border border-line">
        <SettingsRow href="/profile/listening-stats" label={<><StatsIcon /> Listening stats</>} />
        <SettingsRow href="/profile/recents" label={<><RecentsIcon /> Recents</>} />
        <SettingsRow href="/profile/settings" label={<><SettingsIcon /> Settings and privacy</>} />
        <SettingsRow href="/profile/install" label={<><DevicesIcon /> Get the app</>} />
      </nav>
    </div>
  );
}
