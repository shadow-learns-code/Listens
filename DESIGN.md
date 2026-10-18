# Design System: Shadow Listens
**Project ID:** none. This file describes the app's own design language, read from `frontend/src/app/globals.css`
and the live screens (a Stitch project was not available when it was written).

Use it as the source of truth when prompting Stitch, or any designer, to make a new screen that belongs next to the
existing ones.

## 1. Visual Theme & Atmosphere

A **dark, quiet, late-night music room**. The base is a hue-neutral graphite, so the colour of the screen comes from
only two places: one deep rose accent, and the cover art of whatever is playing, which tints the whole backdrop.
There is no decorative gradient and no second brand colour.

Surfaces are **liquid glass**: translucent white layers over the backdrop with strong blur, a bright specular edge on
the top rim and a soft inner glow, in the manner of a modern phone's Now Playing screen. The feel is *airy, soft,
unhurried*, with generous space around content and few borders.

Motion is gentle and musical (equaliser bars, a soft pop on the selected tab, a slow cover fade-in). Every animation
has a calm fallback when the listener asks for reduced motion, and glass can be flattened for reduced transparency.

## 2. Color Palette & Roles

| Name | Hex | Role |
|---|---|---|
| **Midnight Graphite** | `#0c0d11` | Page background. Slightly blue-black, never pure black. |
| **Pale Moonlight** | `#eceef2` | Primary text and the play button's fill. |
| **Dusty Silver** | `#a6aab6` | Secondary text, captions, inactive icons. |
| **Deep Rose (the one accent)** | `#f0587a` | Primary actions, the current track, progress, focus rings, selected states. Desaturated on purpose so it sits with the neutrals instead of glowing. Dark text on it measures about 5.9:1. |
| **Warm Ember** | `#ff9a5c` | Errors and destructive hints (warm orange, softer than red). |
| **Mint Signal** | `#4fe0b0` | Success, "Ready" and "done" states. |
| **Glass Veil** | white at 7% | Cards and panels over the backdrop. |
| **Glass Lift** | white at 11% | Raised or hovered surfaces, inputs. |
| **Glass Hairline** | white at 14% | One-pixel dividers and outlines. |
| **Resting Glow** | `#f0587a` at 12% | A faint radial glow in the top-left corner of the page when nothing is playing. |

Colour is never the only signal: states also change shape, label or icon.

## 3. Typography Rules

- **Geist Sans** for everything, falling back to the system UI font (SF/Segoe/Roboto). **Geist Mono** only for
  technical values.
- Page titles are large and semibold with slightly tight tracking ("Upload music"). Section headings are medium weight
  at roughly 18 px. Body and list text is regular. Song titles are the brightest text on any row; artists and counts
  drop to Dusty Silver.
- Long titles are cut with an ellipsis, never wrapped into a second line inside a row.
- Small status labels (for example "Ready", "Waiting to start") sit in small pills at a lighter weight.

## 4. Component Stylings

* **Buttons:** **Pill-shaped.** The primary action is solid Deep Rose with dark text. Secondary actions are
  transparent with a Glass Hairline outline and light text. The player's play button is a Pale Moonlight circle with
  dark glyphs. Minimum touch height is 44 px. Pressed buttons shrink very slightly; disabled ones drop to 40% opacity.
* **Cards and containers:** **Generously rounded corners (16 px).** Glass Veil fill, a blurred and saturated backdrop,
  a bright inner top edge and a soft, low shadow. Cards float; they are not outlined boxes.
* **Chrome surfaces** (sidebar, player bar, menus, side panels): the same glass with a darker tint so text stays
  readable over busy album art.
* **Mobile tab bar:** a floating clear-glass capsule near the bottom. The selected tab is a lighter rose-tinted
  "lens" sliding under the label, which stays white.
* **Inputs and forms:** **Subtly rounded corners (8 px)**, Glass Lift fill, a Glass Hairline stroke that turns Deep
  Rose on focus. Labels sit above in Dusty Silver. Checkboxes and range sliders use the accent.
* **List rows:** 8 px corners, a 44 to 48 px cover thumbnail on the left (rounded, never circular), title over artist,
  then like, queue and add actions on the right. Dividers are Glass Hairline. The playing row shows animated
  equaliser bars in the accent.
* **Progress and timelines:** a thin track with a rose fill that glides between updates, and a handle that grows under
  a finger. On the lyrics screen the active line fills left to right like a karaoke sweep.
* **Loading:** five-bar sound loader for unknown waits; faint shimmering skeleton panels shaped like the real content;
  a glass banner ("Waking up the server...") only when a request takes longer than about 3 seconds.
* **Status pills:** small, fully rounded; Mint Signal for ready, accent for working, Dusty Silver for idle or paused.

## 5. Layout Principles

- **Whitespace over lines.** Sections are separated by space and a quiet heading, not heavy borders.
- **One column on phones**, with a 16 px side gutter, content flowing under the floating tab bar (the page keeps
  enough bottom padding to clear the tab bar and the mini player). No horizontal page scroll.
- **Desktop** adds a collapsible left sidebar and optional right-hand panels (queue, lyrics, similar songs) that
  slide in and shrink the song list instead of covering it.
- **Edge-to-edge on phones:** safe-area insets are respected for the status bar, gesture bar and notch.
- **Shelves** (horizontal carousels of covers) for discovery, **lists** for libraries; the cover art is always the
  hero and its colours are allowed to wash the backdrop.
- A hierarchy of one focal element per screen: the playing cover, the page's single primary button, or the form.
