"use client";

import type { ReactNode } from "react";
import { CloseIcon } from "@/components/icons";
import { useSlideIn } from "@/lib/useSlideIn";
import { useSwipe } from "@/lib/swipe";

interface Props {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  headerExtra?: ReactNode;
  testId: string;
  ariaLabel: string;
  children: ReactNode;
}

/** Shared shell for the right-hand sliding panels (Queue, Lyrics, Similar tracks): consistent
 *  slide-in/out animation and swipe-right-to-close, so the three don't each reimplement it
 *  slightly differently. On desktop it ends above the player bar so the bar's seek line stays fully visible. Stays mounted through the close transition (see useSlideIn) so a
 *  swipe or the close button plays an actual exit animation instead of the panel just vanishing. */
export default function SidePanel({ open, onClose, title, subtitle, headerExtra, testId, ariaLabel, children }: Props) {
  const { mounted, shown } = useSlideIn(open);
  const swipe = useSwipe({ onSwipeRight: onClose });

  if (!mounted) return null;

  return (
    <aside
      data-testid={testId}
      aria-label={ariaLabel}
      aria-hidden={!open}
      onPointerDown={swipe.onPointerDown}
      onPointerUp={swipe.onPointerUp}
      className={`glass-surface fixed inset-y-0 right-0 z-40 flex w-full max-w-sm flex-col pt-[env(safe-area-inset-top)] transition-transform duration-200 ease-out motion-reduce:transition-none md:bottom-28 md:top-3 md:right-3 md:rounded-2xl ${shown ? "translate-x-0" : "translate-x-[110%]"}`}
    >
      <header className="flex items-center justify-between gap-2 border-b border-white/10 px-4 py-3">
        <div className="min-w-0">
          <h2 className="font-semibold">{title}</h2>
          {subtitle && <p className="truncate text-xs text-muted">{subtitle}</p>}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {headerExtra}
          <button aria-label={`Close ${title.toLowerCase()}`} onClick={onClose} className="flex size-11 items-center justify-center rounded-full text-muted hover:text-fg">
            <CloseIcon />
          </button>
        </div>
      </header>
      <div className="flex-1 overflow-y-auto pb-40 md:pb-4">{children}</div>
    </aside>
  );
}
