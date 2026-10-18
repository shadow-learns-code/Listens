"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import AmbientBackdrop from "@/components/AmbientBackdrop";
import BackButton from "@/components/BackButton";
import { CenteredLoader } from "@/components/Loaders";
import LyricsPanel from "@/components/LyricsPanel";
import NativeBridge from "@/components/NativeBridge";
import NavHistory from "@/components/NavHistory";
import RecentsTracker from "@/components/RecentsTracker";
import NowPlayingSheet from "@/components/NowPlayingSheet";
import PlayerBar from "@/components/PlayerBar";
import PlayerEngine from "@/components/PlayerEngine";
import QueuePanel from "@/components/QueuePanel";
import Sidebar from "@/components/Sidebar";
import SimilarTracksPanel from "@/components/SimilarTracksPanel";
import { get } from "@/lib/api";
import type { User } from "@/lib/types";
import { useAuth } from "@/store/auth";
import { useLayout } from "@/store/layout";
import { useLikes } from "@/store/likes";
import { usePlayer } from "@/store/player";

/**
 * Authenticated shell. Because this layout stays mounted while you navigate between its pages,
 * the player engine, mini-player and queue keep running: that is the "persistent player".
 */
export default function AppLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const { ready, accessToken, user, load, setUser } = useAuth();
  const loadLikes = useLikes((s) => s.load);
  const collapsed = useLayout((s) => s.sidebarCollapsed);
  const loadLayout = useLayout((s) => s.load);
  // any of the right-hand panels open: on desktop the page makes room for it instead of sliding underneath
  const panelOpen = usePlayer((s) => s.lyricsOpen || s.queueOpen || s.similarOpen);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { loadLayout(); }, [loadLayout]);
  useEffect(() => {
    if (ready && !accessToken) router.replace("/login");
  }, [ready, accessToken, router]);
  useEffect(() => {
    if (accessToken && !user) get<User>("/api/auth/me").then(setUser).catch(() => {});
  }, [accessToken, user, setUser]);
  useEffect(() => {
    if (accessToken) loadLikes();
  }, [accessToken, loadLikes]);

  if (!ready || !accessToken) return <CenteredLoader label="Starting Shadow Listens" />;

  return (
    // --side is the desktop sidebar's footprint; the main column and the player bar both offset by it
    <div
      className={`min-h-dvh transition-[padding] duration-300 ease-out motion-reduce:transition-none md:pl-[var(--side)] ${panelOpen ? "md:pr-[25rem]" : ""}`}
      style={{ "--side": collapsed ? "5.25rem" : "15rem" } as React.CSSProperties}
    >
      <AmbientBackdrop />
      <NavHistory />
      <NativeBridge />
      <RecentsTracker />
      <Sidebar />
      <main className="mx-auto max-w-6xl px-4 pb-44 pt-[calc(1.5rem+env(safe-area-inset-top))] md:px-8">
        <BackButton />
        <div key={pathname} className="animate-fade-in">{children}</div>
      </main>
      <PlayerBar />
      <NowPlayingSheet />
      <QueuePanel />
      <LyricsPanel />
      <SimilarTracksPanel />
      <PlayerEngine />
    </div>
  );
}
