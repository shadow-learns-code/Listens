"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { AlbumIcon, HomeIcon, PlaylistIcon, ProfileIcon, SearchIcon, SidebarIcon, UploadIcon } from "@/components/icons";
import { Logo, LogoMark } from "@/components/Logo";
import { useAuth } from "@/store/auth";
import { useLayout } from "@/store/layout";

const NAV = [
  { href: "/", label: "Home", Icon: HomeIcon },
  { href: "/search", label: "Search", Icon: SearchIcon },
  { href: "/playlists", label: "Playlists", Icon: PlaylistIcon },
  { href: "/albums", label: "Albums", Icon: AlbumIcon },
  { href: "/upload", label: "Upload", Icon: UploadIcon },
  { href: "/profile", label: "Profile", Icon: ProfileIcon },
];

/** Just navigation: sign-out lives inside /profile now, not duplicated here too. */
export default function Sidebar() {
  const path = usePathname();
  const { user } = useAuth();
  const collapsed = useLayout((s) => s.sidebarCollapsed);
  const toggle = useLayout((s) => s.toggleSidebar);
  const active = (href: string) => (href === "/" ? path === "/" : path.startsWith(href));
  const activeIndex = NAV.findIndex(({ href }) => active(href));

  return (
    <>
      <nav
        aria-label="Main"
        data-collapsed={collapsed}
        className={`glass-surface fixed bottom-3 left-3 top-3 z-20 hidden flex-col overflow-hidden rounded-2xl p-3 transition-[width] duration-300 ease-out motion-reduce:transition-none md:flex ${collapsed ? "w-[4.25rem]" : "w-56"}`}
      >
        <div className={`mb-6 mt-1 flex items-center ${collapsed ? "flex-col gap-4" : "justify-between px-1"}`}>
          <Link href="/" aria-label="Shadow Listens home">{collapsed ? <LogoMark /> : <Logo />}</Link>
          <button
            data-testid="sidebar-toggle"
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-expanded={!collapsed}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            onClick={toggle}
            className="flex size-9 shrink-0 items-center justify-center rounded-lg text-muted transition-colors hover:bg-white/10 hover:text-fg"
          >
            <SidebarIcon />
          </button>
        </div>
        <ul className="space-y-1">
          {NAV.map(({ href, label, Icon }) => (
            <li key={href}>
              <Link
                href={href}
                title={collapsed ? label : undefined}
                aria-label={collapsed ? label : undefined}
                className={`flex items-center gap-3 rounded-lg py-2 text-sm transition-colors ${collapsed ? "justify-center px-0" : "px-3"} ${active(href) ? "bg-white/15 text-fg shadow-[inset_0_1px_0_rgba(255,255,255,0.25)]" : "text-muted hover:bg-white/10 hover:text-fg"}`}
              >
                <Icon /> {!collapsed && <span className="truncate">{label}</span>}
              </Link>
            </li>
          ))}
        </ul>
        <div className={`mt-auto truncate border-t border-line pt-4 text-sm text-muted ${collapsed ? "text-center" : ""}`} data-testid="whoami" title={user ? `@${user.username}` : undefined}>
          {user ? (collapsed ? user.username.slice(0, 1).toUpperCase() : `@${user.username}`) : ""}
        </div>
      </nav>

      {/* mobile: bottom tab bar. A glass pill slides to the active tab (spring-like overshoot), the active icon
          lifts and pops once when it becomes active. All motion is dropped for prefers-reduced-motion. */}
      <nav aria-label="Main" className="glass-clear fixed inset-x-3 bottom-[max(0.75rem,env(safe-area-inset-bottom))] z-30 flex h-14 rounded-full px-2.5 md:hidden">
        <span
          aria-hidden
          className="pointer-events-none absolute inset-y-1 left-2.5 transition-[transform,opacity] duration-300 ease-[cubic-bezier(0.34,1.4,0.64,1)] motion-reduce:transition-none"
          style={{ width: `calc((100% - 1.25rem) / ${NAV.length})`, transform: `translateX(${Math.max(activeIndex, 0) * 100}%)`, opacity: activeIndex < 0 ? 0 : 1 }}
        >
          <span className="glass-lens -mx-1.5 block h-full rounded-full" />
        </span>
        {NAV.map(({ href, label, Icon }, i) => {
          const isActive = i === activeIndex;
          return (
            <Link
              key={href}
              href={href}
              className={`relative z-10 flex flex-1 flex-col items-center justify-center gap-0.5 text-[11px] transition-[color,transform] duration-200 active:scale-90 motion-reduce:transition-none [text-shadow:0_1px_8px_rgba(0,0,0,0.55)] ${isActive ? "text-white" : "text-white/75"}`}
            >
              <span className={`block transition-transform duration-300 motion-reduce:transition-none ${isActive ? "-translate-y-0.5 scale-110 animate-tab-pop" : ""}`}>
                <Icon />
              </span>
              {label}
            </Link>
          );
        })}
      </nav>
    </>
  );
}
