# Shadow Listens

A Spotify-inspired music streaming platform, built as a portfolio project, with original branding
and artwork. Music comes only from files you upload and own, or Creative Commons / royalty-free
tracks (Free Music Archive, Jamendo, etc.). Built in phases, each one finished and tested before
the next starts. It is live, running entirely on free tiers (see "Deployment (free tier)").

- ✅ **Phase 1 – Foundation:** monorepo, Docker Compose stack, Alembic schema, auth service
  (JWT access + rotating refresh tokens), API gateway.
- ✅ **Phase 2 – Catalogue & streaming:** upload → Backblaze B2 → FFmpeg → HLS (96/160/320 kbps), signed
  short-lived stream URLs, web player (queue, seek, volume, quality, prefetch, persistent mini-player), full-text search.
- ✅ **Phase 3 – Library & social:** playlists (CRUD, reorder, collaborative), likes, follows,
  recently played, Home sections (recently played / made for you / new uploads), synced lyrics.
- ✅ **Phase 4 – Event tracking:** play/skip events → Redis Streams → a consumer service, verified
  streams (30s threshold), and an analytics page (top tracks, skip rate, listening by hour).
- ✅ **Phase 5 – Recommendations:** implicit-feedback ALS (nightly `recommender` service), ALS-ranked
  "made for you" (max 2 per artist), "similar tracks" via vector cosine similarity, cold-start
  fallback via `librosa` tempo/energy then same-artist/genre.
- ✅ **Phase 6 – Smarter suggestions and shuffle** (tuned for Tamil film music): a hybrid recommender that works for
  one listener on a small library, plus a flow-aware shuffle. See "Suggestions and shuffle" below.
- ✅ **Phase 7 – Live on free tiers:** two Cloudflare Workers (the site, and a cache in front of Backblaze B2) in front of
  Render, Neon and Upstash, at zero cost. See "Deployment (free tier)".
- ✅ **Phase 8 – Liquid-glass UI and polish:** glass design and a mobile tab bar, "Recently played" that updates live,
  loading skeletons, a "waking up the server" banner, an animated seek bar and lyrics timeline, and a collapsible
  desktop sidebar. See "Interface".

## Layout

```
.
├── docker-compose.yml          # local stack: postgres, redis, migrate, auth, catalogue, stream, worker, events, recommender, frontend, gateway
├── render.yaml                 # the same services as Render blueprints (free tier)
├── cloudflare-worker.js        # the site Worker: routes /api/* straight to the right service, warms sleepers, retries
├── wrangler.toml               # config for the site Worker (+ cloudflare-worker.test.mjs)
├── cdn/                        # the CDN Worker: Cloudflare cache in front of Backblaze B2 (own README, tests, live verifier)
├── .env.example                # copy to .env
├── gateway/nginx.conf          # local single entry point (:8080); in production the site Worker does this job
├── .github/workflows/cron-tick.yml   # pokes the background services on a slow schedule (free-tier friendly)
├── libs/common/                # shared: settings, SQLAlchemy models, JWT/bcrypt, URL signing, S3 helper, dedupe, artist credits
│   └── tests/                  # migration-vs-models, signing, dedupe, artists, music metadata
├── migrations/                 # Alembic 0001-0006: schema, catalogue + full-text, lyrics, audio features + vectors, taste features, track_artists
├── services/
│   ├── auth/                   # register / login / refresh / logout / profile
│   ├── catalogue/               # upload, tracks, artists, albums, search, playlists, likes, follows, lyrics, home, events, analytics, recommendations
│   ├── stream/                  # issues signed URLs, serves HLS + covers from private storage (Range/206 + Cache-Control pass-through)
│   ├── worker/                  # transcode: FFmpeg -> HLS renditions + cover art + librosa audio features
│   ├── events/                  # drains the Redis event stream (fallback path) into play_events
│   └── recommender/              # scheduled job: builds the interaction matrix, trains ALS, writes user/track vectors
├── frontend/                   # Next.js 15 · React 19 · TypeScript · Tailwind 4 · Zustand · hls.js (+ unit tests, Playwright e2e)
└── scripts/
    ├── import_folder.py        # bulk-import a music folder through the public API
    ├── import_local_folder.py  # import a folder straight into the database + B2 and transcode it on this PC
    ├── run_local_worker.sh     # run the transcode worker on this PC against production (fast; Render's is the slow fallback)
    ├── enrich_library.py       # fetch artist relatedness / tags and audio fingerprints for the recommender
    ├── dedupe_library.py       # list / delete duplicate songs (same title + artist, compatible album)
    ├── dedupe_similar.py       # the same, for spelling/credit variants of a song; also deletes their B2 files
    ├── clean_b2_orphans.py     # delete B2 folders and covers that no song refers to
    ├── clean_album_titles.py   # strip "(Original Motion Picture Soundtrack)" etc. and merge repeated albums
    ├── process_queue.py        # transcode the waiting songs on this PC, N at a time, within a B2 download budget
    ├── split_artist_credits.py # one artist per person: split "A, B, C" credit rows and credit each song to everyone on it
    ├── normalize_artists.py    # merge artists that differ only by "/" vs "," credit separators (superseded by the split)
    ├── register_b2_orphans.py  # add songs that are in the B2 bucket but missing from the app
    └── setup_b2_cors.py        # one-off: apply CORS rules to the B2 audio/covers buckets
```

## Run

Object storage is **Backblaze B2** (S3-compatible API, not a compose service) — create a private
bucket each for audio and covers, and an Application Key (Read and Write) in the B2 console first.

```bash
cp .env.example .env            # change JWT_SECRET/STREAM_SIGNING_SECRET, and fill in the real S3_* values for your B2 account+buckets
docker compose up --build
```

Open **http://localhost:8080**, register, go to **Upload**, and add a few files. Status goes
*Queued → Processing… → Ready* as the worker transcodes (seconds per track). Then play them from **Home**.

Scale transcoding with more workers: `docker compose up --scale worker=3`.

### Import your music folder

```bash
pip install -r scripts/requirements.txt
python scripts/import_folder.py "C:\path\to\Music" -u <username> --license "Personal library" --i-own-the-rights
# add --dry-run first to see what it would do; --base-url http://<server> for a remote server
```

Re-runnable: each file's SHA-256 is checked with the server first, so already-imported files are skipped
without being re-uploaded. Tags (title/artist/album/genre) are read server-side; untagged files fall back to
the filename and "Unknown Artist". Budget disk for originals **plus** HLS (roughly 1.5–2× the source size).

## Deployment (free tier)

```
browser
  ├─► Cloudflare Worker  shadow-listens        (cloudflare-worker.js)
  │     /api/auth ─────────► Render  auth       ─┐
  │     /api/catalogue ────► Render  catalogue ──┼─► Neon Postgres, Upstash Redis
  │     /api/stream ───────► Render  stream    ─┘   (signs audio links)
  │     everything else ───► Render  frontend  (Next.js)
  │
  └─► Cloudflare Worker  shadow-listens-cdn    (cdn/)   audio + covers, cached ─► Backblaze B2 (private buckets)

background (slow, on a schedule): Render worker (transcode) · events · recommender, poked by GitHub Actions cron-tick.yml
```

| Piece | Where | Why |
|---|---|---|
| Web app + APIs | Render free web services | free, but each one **sleeps after ~15 min idle** |
| Database / Redis | Neon / Upstash | free tiers |
| Audio, covers | Backblaze B2 (private) | free 10 GB, but a **1 GB/day download cap** and a daily cap on read requests |
| Cache in front of B2 | `cdn/` Worker | serves repeat plays from Cloudflare's cache so B2 is read once per file (see `cdn/README.md`) |
| Front door | `cloudflare-worker.js` | routes straight to each service, warms sleeping ones, retries safe requests |

**Cold starts.** A sleeping service answers its first request after ~20-30 s. Called from outside Render that request
simply waits and succeeds. Called from *inside* Render (the old nginx gateway talking to a sleeping sibling) it gets an
instant `502`, which is why the site Worker talks to the services directly. It also pings auth/catalogue/stream when the
home or login page is opened, so they wake while you type, and retries `GET`s that hit a `502/503/504`. The UI shows a
"Waking up the server" banner when any request is pending for more than 3 s.

**Background work.** `.github/workflows/cron-tick.yml` pokes the worker every 6 h (UTC), events at 02:50 and the
recommender at 03:00. It is deliberately infrequent: Render's free plan gives the whole workspace 750 instance-hours a
month, and keeping services awake around the clock would use them all. For faster transcoding run the worker on your own
PC (`scripts/run_local_worker.sh`, against production) while the laptop is on; each job goes to exactly one worker.
A manual-only `warm-front-services` job wakes the user-facing services before you share the link.

**Migrations.** The auth service runs `alembic upgrade head` every time it starts (including every wake-up). So a migration
must never be applied to the database before the push that contains it: the deployed auth image would fail with
`Can't locate revision identified by '00NN'` and sign-in stays down until the push lands. A migration that adds columns the
other services then read goes in **two pushes**: first only the migration file (auth applies it on its next start; check
`alembic_version` in Neon), then the code and models that use the columns. Pushing both at once lets catalogue query a column
that does not exist yet for the minute auth takes to wake and migrate.

**Repeat check (before a song goes in).** Every way a song enters the library runs `dedupe.check_duplicate` first: the web
upload, `import_local_folder.py` and `register_b2_orphans.py` (the import through the API uses the upload endpoint). Tiers, cheapest
first: identical bytes (SHA-256); the same title and the same people credited (any separators or order) with a compatible
length; and a spelling or credit variant of the same recording (title with "(From ...)" and romanisation noise folded,
the same version so a slowed, live or remix edit is never a repeat, a compatible length from the file's own header, and a
shared artist or album). A refused upload returns `duplicate: true` with a plain-language `reason` (shown on the Upload page)
and writes nothing to B2; `force=true` overrides it for a deliberate second version. The worker repeats the check from the
tags before it downloads an original, so a repeat costs no storage bandwidth (a clearly better-quality re-upload, judged from
file size, is still processed and replaces the old copy), and after transcoding the best copy of a group is kept.

**Settings that matter.**

| Variable | Meaning |
|---|---|
| `STREAM_PUBLIC_PREFIX` | where signed links point; set to the CDN Worker's address so audio goes through the cache |
| `STREAM_SIGNING_SECRET` | must be identical on the stream service and the CDN Worker (it verifies the links itself) |
| `EVENTS_VIA_QUEUE` | `false` (default): play events are written to Postgres directly; `true`: via the Redis stream and the events service |
| `S3_*` | Backblaze B2 endpoint, region, keys and bucket names |

**Backblaze limits.** Reading originals for transcoding counts against the same daily download cap as playback. Import in
small batches (the scripts report how many bytes they will download), and let the CDN absorb playback.
`cdn/test/verify_live.py` checks the live Worker end to end (cache `MISS` then `HIT`, bad tokens refused).

**Library maintenance.**

```bash
set -a; source .env.prod; set +a                        # production values (gitignored)
.venv/Scripts/python scripts/split_artist_credits.py   # one artist per person in a credit (--apply to commit)
.venv/Scripts/python scripts/dedupe_library.py          # exact duplicate songs            (--apply to delete)
.venv/Scripts/python scripts/dedupe_similar.py          # spelling/credit variants + files (--apply to delete)
.venv/Scripts/python scripts/clean_b2_orphans.py        # B2 files no song refers to       (--apply to delete)
.venv/Scripts/python scripts/clean_album_titles.py      # tidy album titles, merge repeats (--apply to save)
.venv/Scripts/python scripts/process_queue.py --budget-mb 300 --workers 2 --apply   # transcode the waiting songs here
.venv/Scripts/python scripts/register_b2_orphans.py     # songs in B2 but not in the app   (--apply to add)
```

**Artists.** A song can have several artists. Tags separate collaborators with `/`, `,` or `;`, so every credit is split into
people (`listens_common/artists.py`): one artist row per person, a `track_artists` link per song and person (in credit
order), and `tracks.artist_id` is the first credited person (the primary artist, which albums hang off). Everyone
credited sees the song on their page and in search; the API returns `artist` (primary) and `artists` (all). Known Tamil
artists' spelling variants are one person (`Yuvanshankar Raja` = `Yuvan Shankar Raja`, `SPB`; aliases live in
`tamil_seed.py`). Duplicate detection compares the whole set of people, so `A/B` and `B, A` are the same credit.
Uploads and the import scripts do this automatically; `split_artist_credits.py` converted the existing library.

## Android app

`android/` is a native Android app (package `com.shadowlistens.app`, no third-party libraries): a full-screen WebView of the live
site plus what a browser cannot do. A foreground service with a media notification keeps music playing with the screen off
(wake and Wi-Fi locks only while sound or an upload is in progress; the notification has previous / play-pause / next and
shows the cover and progress). It also gives the file picker, native dialogs, edge-to-edge insets, and a "You're offline" page
that retries by itself. Build and install:

```
# JDK 21, Gradle 8.10 and the Android SDK (platform 35, build-tools 35) as in android/README.md; the signing key is android/keystore.properties (gitignored, back it up)
cd android && VERSION_CODE=3 VERSION_NAME=2.0.1 gradle assembleRelease      # -> app/build/outputs/apk/release/app-release.apk
adb install -r app/build/outputs/apk/release/app-release.apk
cp app/build/outputs/apk/release/app-release.apk ../frontend/public/app/shadow-listens.apk   # served at /app/shadow-listens.apk
```

The site detects the app (`window.NativeApp`, `frontend/src/lib/native.ts`) and sends it what is playing
(`NativeBridge.tsx`). Android's WebView has no `navigator.mediaSession`, so an app that talks to a site that predates this
(or where the bridge is missing) still works: `android/app/src/main/assets/native-shim.js` is injected after each page load,
reads the site's own player (title, artist, cover, the `<audio>` element, the previous / play / next buttons) and presses
those buttons when a notification button is tapped. It stands down as soon as the site reports to the app itself. The
website is also an installable PWA (`manifest.webmanifest`, `sw.js`: app shell served from cache so it opens instantly even
while the server sleeps; the Profile > Get the app page explains installing on Android and iPhone).

## Uploads and song processing

**Resumable upload** (`services/catalogue/app/uploads.py`, `frontend/src/lib/uploader.ts`, `store/uploads.ts`). The Upload page
sends each file in 5 MiB parts (one file at a time, so memory stays small on a phone), each part straight into a Backblaze
multipart upload, with the session in Redis: `POST /uploads/sessions` to start, `PUT .../parts/{n}`, `GET` to see what arrived,
`POST .../complete`, `DELETE` to cancel. You can **pause, resume and cancel**; a dropped connection retries with backoff and
resumes by itself when you are back online; a reload resumes if you pick the same file again. Nothing is created in the library
until `complete`, and the same strict repeat check as a normal upload runs there, discarding a repeat without ever making a
storage object. The queue lives outside the page, so uploads carry on while you browse, and the app's notification shows
progress with a Pause / Resume button. The worker aborts multipart uploads abandoned for 3 days (they would otherwise count as
stored data).

**Song processing control** (`services/catalogue/app/processing_api.py`, the *Song processing* card on the Upload page). Shows how
many of your songs are waiting, converting and ready, with **Start processing now** (wakes the Render worker, which drains
the queue) and **Pause / Resume** (a flag in Redis, `listens_common/processing.py`). Pause stops **only the Render worker**;
workers on your laptop (`scripts/run_local_worker.sh`, `scripts/process_queue.py`) never look at it and keep going. A song
already being converted is finished first. Catalogue reaches the worker at `WORKER_URL` (defaults to the production worker).
`process_queue.py` re-queues any song left in `processing` for more than 45 minutes by a stopped run, so an interrupted run can
no longer leave a song stuck for days; the worker also gives the database connection back before a long conversion (Neon closes
idle connections) and uploads the stream files four at a time.

## Interface

- **Liquid-glass design** with a rose accent; every glass surface falls back to a solid colour when the system asks for
  reduced transparency. Mobile has a floating glass tab bar with a sliding lens; desktop has a sidebar that **collapses to
  an icon rail** (button at its top, remembered per browser), and the Lyrics / Queue / Similar panels slide in from the
  right while the page makes room for them and stops above the player bar so its seek line stays visible.
- **Loading states.** Skeletons shaped like the content (track rows, album grid, detail header, lyrics) instead of
  "Loading…", sound-bar loaders for short waits, a progress state on the sign-in button, and the "Waking up the server"
  banner (elapsed seconds + sweeping bar; no fake percentage) once a request has been pending for 3 s. Play events and
  background refreshes never raise the banner. Code: `components/Loaders.tsx`, `WakingBanner.tsx`, `store/net.ts`.
- **Timeline.** The seek bar (`components/Timeline.tsx`) is still a real range input (keyboard, screen readers and
  tap-to-seek work) with a fill that glides between position updates, a handle that grows under a finger and softly
  pulses while playing, and a sweeping track while buffering. In the lyrics view the current line fills left to right
  with its progress. All motion stops under `prefers-reduced-motion`.
- **Search filters.** Chips for All / Artist / Album / Genre / Mood. Artist finds every song they are credited on, Album its
  songs, Genre a tag, and Mood takes everyday words ("sad", "chill", "party") and lists the clearest examples first; Genre and
  Mood show tap-to-pick lists with counts (`/search?by=...`, `/search/facets`).
- **Thumbnails.** Signed cover links are valid for at least a full day-window, Recently played always uses the server's fresh
  copy of a song (and drops songs the server no longer has), and a cover that cannot load shows the placeholder, never a
  broken-image icon.
- **Back navigation.** One plain back arrow per page on phones; settings pages show only their own "‹" beside the title.
- **Recently played** reflects what you actually listened to (a song counts after 8 s, so skips don't), updates live
  without a refresh from an on-device list merged with the server's.

## What to test

1. **Auth:** register → you land on Home, sign out, sign in (by username or email). `/upload` while signed out redirects to login.
2. **Upload:** pick 2+ files, tick the rights box (the button stays disabled until you do). Watch statuses flip to *Ready*.
   Re-upload the same file → "already uploaded". Upload a text file renamed `.mp3` → it ends *Failed* with a reason.
3. **Player:** play/pause, drag the seek bar (the fill follows, the handle breathes while playing), volume, **Quality** (Auto / 96 / 160 / 320 — only renditions that exist
   for that track are listed), next/previous (previous restarts the track if you're >3 s in), the queue panel (jump, remove),
   *Next*/＋ buttons on rows. Navigate between pages while playing: music and mini-player keep going.
4. **Mobile:** open on a phone (or a narrow window): bottom tab bar, compact player, lock-screen controls (Media Session).
5. **Search:** prefix + multi-word, case-insensitive; matching an artist name returns that artist's tracks.
6. **Streaming security:** in dev-tools copy a segment URL; change one character of the token → 403; wait past expiry → 403.
   `POST /api/stream/tracks/<id>/url` without a token → 401.
7. **Likes:** heart a track from any list or the player bar; it stays highlighted after a reload.
8. **Playlists:** create one, add tracks (from a track row's playlist icon), reorder with the up/down arrows, rename, make it
   public/collaborative, and — from a second account — confirm you can add/reorder tracks on a collaborative playlist but
   can't rename or delete it. Delete it from the owner account.
9. **Follow:** on an artist page, Follow/Following toggles and persists.
10. **Home sections:** Recently played (after you've played something), Made for you (never empty, even for a brand-new
    account — it falls back to new releases), New uploads.
11. **Lyrics:** open the lyrics panel (text icon next to the queue icon) on any track. Most files show "No lyrics available"
    unless they have embedded ID3 `USLT`/`SYLT` tags (or Vorbis/MP4 equivalents) — see the note under "How it works".
12. **Analytics:** play a track for 30+ seconds (check it against the Network tab: a `play_end` event with `ms_played >= 30000`
    fires on the next track change), then check the Analytics page — it should show up under *Top tracks*. Click Next on a
    track and confirm a `skip` event fires with `reason: "next_button"`. Skip rate needs at least 2 plays of a track to appear.
13. **Similar tracks:** open the similar-tracks panel (sparkle icon next to the queue icon) on any playing track — it never
    shows the track itself. Brand-new uploads with no listening history yet fall back to tempo/energy proximity, then
    same-artist/genre, so the panel is never empty as long as *some* other ready track exists.
14. **Made for you:** once `services/recommender` has trained at least once (immediately on stack startup, then every
    `RECOMMENDER_TRAIN_INTERVAL_HOURS`), a user with enough listening history gets ALS-ranked picks, capped at 2 per
    artist; everyone else still sees the Phase 3 heuristic/new-releases fallback — the section is never empty.
15. **Loading and cold start:** throttle or delay the API in dev-tools: pages show skeletons, a request pending for 3 s
    shows the "Waking up the server" banner, and both disappear when the data arrives. Sign in shows a progress state.
16. **Desktop layout:** collapse and expand the sidebar (it stays collapsed after a reload); open Lyrics and confirm the
    page content narrows beside it and the player bar's seek line is fully visible below the panel.
17. **Artists:** a song credited `A/B` shows "A, B", appears on both A's and B's pages and in a search for either, and
    tapping a name on the Now Playing sheet opens that person. `A/B` and `B, A` uploaded twice are one song.

## Tests

```bash
python -m venv .venv
.venv\Scripts\pip install -e "libs/common[web,storage]" -r services/catalogue/requirements.txt -r services/worker/requirements.txt -r services/recommender/requirements.txt pytest httpx fakeredis alembic "moto[server]"   # Windows
# source .venv/bin/activate && pip install ...                                                                                                   # macOS/Linux
# needs ffmpeg + ffprobe on PATH; set OPENBLAS_NUM_THREADS=1 to avoid BLAS/thread-pool contention under pytest
(cd libs/common        && pytest)   # signing, dedupe, artist credits, "migrations produce exactly the ORM schema"
(cd services/auth      && pytest)
(cd services/catalogue && pytest)   # upload, catalogue, search, playlists, likes, follows, lyrics, events, analytics, recommendations
(cd services/stream    && pytest)   # signed URLs, path whitelist, expiry/tamper cases
(cd services/worker    && pytest)   # real FFmpeg transcodes, retry/failed/sweep logic, librosa audio-feature extraction
(cd services/events    && pytest)   # Redis Stream consumer: insert/retry/drop/sweep logic
(cd services/recommender && pytest) # matrix weighting/clamping, ALS training + vector persistence, a block-structure correctness check

# the Cloudflare Workers, and the frontend's pure logic (shuffle, recents, wait timing):
node --test cloudflare-worker.test.mjs       # site Worker routing, Location rewriting, retries
(cd cdn && npm test)                         # CDN Worker token verification, against tokens minted by the Python signer
(cd frontend && npm run test:unit)           # shuffle, recents merge, request-wait timing
python cdn/test/e2e_local.py                 # CDN Worker against a local fake B2 (needs wrangler)
python cdn/test/verify_live.py               # the deployed CDN Worker: cache MISS then HIT, bad tokens refused

# Postgres full-text search (incl. "planner really uses the GIN indexes" check) needs a real database:
TEST_PG_URL=postgresql+psycopg2://user@localhost:5432/scratch_db  (cd services/catalogue && pytest tests/test_search_postgres.py)
```

Backend tests need `S3_REGION=us-east-1` in the environment (the S3 client refuses to start without a region).

Browser end-to-end, against a running stack - four specs: the core player flow (register → upload →
transcode → play → seek → quality → queue → persistence → search → logout), playlists/likes/follow/lyrics,
and two covering events/analytics (the client fires the right event with the right reason; the analytics
page renders seeded data correctly):

```bash
cd frontend && npm install && npx playwright install chromium
E2E_BASE_URL=http://localhost:8080 npx playwright test
```

Playwright's bundled Chromium ships without an AAC decoder (confirmed: it plays `audio/mpeg` but not
`audio/mp4;codecs="mp4a.40.2"`), so with the stack's default `HLS_AUDIO_CODEC=aac` the playback assertions
fail there — not an app bug, a missing codec in that specific browser build. Two ways to actually exercise it:
`E2E_BROWSER_CHANNEL=msedge npx playwright test` (uses the real, codec-complete system Edge — this is how
the AAC default was verified below), or set `HLS_AUDIO_CODEC=mp3` in `.env` and use the bundled Chromium as-is.

## How it works

**Upload pipeline.** `POST /api/catalogue/upload` (multipart) validates extension/size/rights, hashes the file (dedupe per user),
reads tags with `mutagen`, stores the original in Backblaze B2 (`originals/<id>/source.ext`), inserts the track as `uploaded`,
and `XADD`s a job to the Redis Stream `jobs:transcode`. A worker (consumer group, at-least-once) atomically claims the track
(`uploaded → processing`), runs `ffprobe` (rejects non-audio), then one FFmpeg run produces AAC renditions as 6 s MPEG-TS
segments + a master playlist, uploads them to `hls/<id>/…`, extracts embedded cover art, and marks the track `ready` (or retries
twice, then `failed` with the reason). A periodic sweep re-queues jobs that were lost or whose worker died.
Renditions above the source's quality are skipped (a 128 kbps MP3 gets 96 + 160, not a pointless 320).

**Signed URLs.** `POST /api/stream/tracks/<id>/url` (JWT) returns `/api/stream/hls/<token>/master.m3u8`. The token (HMAC-SHA256,
track id + expiry) sits in the URL *path*, so every relative reference inside the playlists inherits it with no rewriting
(S3 presigned URLs sign exactly one object, which doesn't work for HLS). The stream service verifies it and reads from the
private bucket itself; only a fixed whitelist of file names is servable. Lifetime = track length + 10 min (15 min – 6 h);
the player transparently re-requests a URL if one expires mid-track.

**Player.** One `<audio>` element lives in the `(app)` layout (so it persists across navigation), driven by a Zustand store.
hls.js handles HLS (Safari uses native HLS). Quality: `Auto` = adaptive; fixed levels switch at the next segment. The next
track's URL, playlists and first segment are prefetched into the HTTP cache ~3 s after a track starts.

**Search.** Postgres `to_tsvector('simple', …) @@ to_tsquery('daft:* & pu:*')` over tracks, artists, albums and playlists
(public + your own), backed by expression GIN indexes (migration 0002) and ranked with `ts_rank`.

**Library & social.** Playlists, likes, follows and `play_events` are all Phase-1 tables; Phase 3 is purely new endpoints
over them (`services/catalogue/app/library.py`), no new microservice. A playlist is visible to everyone if `is_public`,
otherwise only to its owner; any authenticated user can add/remove/reorder tracks on an `is_collaborative` playlist (there's
no per-playlist member list — "collaborative" means "anyone with an account on this server"). Reordering renumbers the whole
list by position, which is fine at personal-playlist sizes. "Made for you" is a heuristic (liked/played artists, excluding
already-played tracks, falling back to newest releases so it's never empty); the hybrid recommender (Phase 6) now
ranks it. "Recently played" lists the last distinct songs you listened to for at least 8 seconds.

**Lyrics.** Read at upload time with `mutagen`: ID3 `USLT` (plain) and `SYLT` (time-synced, converted to LRC
`[mm:ss.xx]text` lines; only millisecond-based `SYLT` timing can be converted — frame-based timing is skipped, not
guessed), or the equivalent Vorbis/MP4 tags as a fallback. Served on demand via `GET /tracks/<id>/lyrics` rather than
embedded in every track response. The player's Lyrics panel highlights the current synced line against playback position.
**Most files won't have any of this** — checked across the 160 real MP3s used for Phase 2 testing, zero had embedded
lyrics in any common frame, so expect "No lyrics available" unless a file was tagged with a tool that writes `USLT`/`SYLT`.

**Event tracking.** `POST /api/catalogue/events` (play_start / play_end / skip) is a hot path (fires on every track
load and every skip). By default it inserts the row into `play_events` directly (one cheap write, and the history is
current immediately, which "Recently played" needs); if that write fails it falls back to `XADD`ing the event to the
Redis Stream `jobs:events`, and with `EVENTS_VIA_QUEUE=true` every event goes through the stream. A dedicated `events`
service (the same consumer-group pattern as the transcode worker) drains that stream into `play_events`. `like` and `add_to_playlist` aren't client-submittable event types; they're published server-side as a
side effect of the existing `/likes` and `/playlists/*/tracks` endpoints, so there's one write path per mutation, not two.
The player tracks actual played-time per track from `timeupdate` deltas (clamped per tick so a seek can't inflate it),
and tags the outgoing track's closing event as `play_end` (natural completion, via the `ended` handler) or `skip` (an
explicit reason - `next_button` / `previous_button` / `track_select` - threaded through the player store by whichever
action caused the change). A "stream" only counts for the Analytics page once `ms_played >= 30000`.

**Recommendations (Phase 5, ALS).** `services/recommender` builds a weighted user×track interaction matrix from
`play_events`/`likes`/`playlist_tracks` — a full listen (≥90% of duration) is +1, every repeat full
listen beyond the first is an additional +2, a save (like or playlist-add) is +3, an early skip
(<30s in) is −2, summed per pair and clamped to ≥0 (confidence-weighted implicit ALS needs
non-negative input; a heavily-skipped track just contributes no positive signal rather than being
fed as a literal negative). It trains `implicit`'s `AlternatingLeastSquares` on that matrix
immediately on startup and every `RECOMMENDER_TRAIN_INTERVAL_HOURS` after (default 24h — "nightly"),
storing the resulting user/track latent-factor vectors as plain JSON arrays (`user_vectors`,
`track_vectors`) rather than a Postgres-specific `ARRAY` or a vector-DB extension, so the schema
stays portable to SQLite in tests. `services/catalogue/app/recommendations.py` scores "made for
you" as a dot product of a user's vector against every unplayed ready track's vector (capped at 2
tracks per artist), falling back to the Phase 3 heuristic when a user has no vector yet; "similar
tracks" is cosine similarity between two tracks' vectors, falling back to z-scored tempo/energy
proximity (`services/worker/app/audio_features.py`, extracted via `librosa` at upload time) and
then same-artist/genre for a track with no vector and no usable audio features. The ALS math
itself — the confidence/preference split, the alternating-least-squares derivation, why it
converges — is explained in detail in code comments in `services/recommender/app/train.py`, per
the original spec's request to show the maths, not just use the library.

## Key decisions

- **Tokens:** access JWT stateless (15 min); refresh JWT stateful in Redis, rotated on use (atomic `GETDEL`), revoked on logout.
- **Why not store audio in Postgres/Git:** blobs belong in object storage; the DB keeps metadata + keys.
- **Redis Streams, not a list:** a job is only removed after it's handled (`XACK`+`XDEL`), and crashed workers' jobs are reclaimed.
- **No FKs on `play_events`; polymorphic `follows`; VARCHAR+CHECK instead of PG enums** (see `libs/common/listens_common/models.py`).
- **Tokens in `localStorage`:** fine for a self-hosted app; production would use httpOnly cookies + CSRF protection.
- **HLS playlist paths use `.as_posix()`, not `str(Path)`:** a real Windows-specific bug found during testing — `ffmpeg`
  on Windows wrote literal backslashes into the generated `.m3u8` playlist text (`q96\index.m3u8`), which breaks every
  HLS player since segment references are URLs. Fixed in `services/worker/app/transcode.py`; harmless on Linux too.
- **MinIO, then Backblaze B2:** the project started on MinIO (and had to swap its image mid-way); storage is now
  Backblaze B2, which brought a daily download cap and led to the CDN Worker.
- **The 502 that was not a cold start:** requests that hop *inside* Render to a sleeping sibling service get an instant
  `502` instead of waiting, while the same request from outside waits ~25 s and succeeds. Moving the routing into the
  site Worker (outside Render) removed the failure rather than papering over it with retries.
- **Backblaze's cap looks like a missing file:** an exhausted daily cap returns 403, which reads like "object not found".
  The CDN Worker reports it as 503 (retryable) and never caches a failure.
- **Artist credits:** tags separate collaborators with `/`, `,` or `;`, so one credit became several artist rows.
  Credits now have one canonical spelling and one comparison key (`listens_common/artists.py`).
- **Python's `0 or -1` falsy-zero trap:** the first track added to a playlist (position 0) was getting duplicated at
  position 0 again, because `(max_position or -1) + 1` treats a real `0` the same as "no rows yet". Fixed to an explicit
  `is None` check; covered by a regression test.
- **Redis Stream fields are always strings:** `PlayEvent.user_id`/`track_id` are typed `Uuid` columns, so the events
  consumer needs an explicit `uuid.UUID(...)` conversion - passing the raw string straight through raised deep inside
  SQLAlchemy's parameter binding. Same bug class the transcode worker didn't have (it never round-trips UUIDs through
  Redis), caught immediately by the consumer's own test suite.
- **CSS percentage heights need a definite-height ancestor:** the listening-by-hour bar chart's columns are sized by
  content (`items-end`, no stretch), so a bar using `height: X%` had nothing to resolve against and silently rendered
  at 0px. Fixed to a computed pixel height. Caught only by looking at an actual screenshot - the e2e test checked the
  bars' `data-count` attributes, which were correct, not their rendered geometry, which wasn't.
- **`libgomp.so.1` missing at runtime:** `implicit`'s compiled ALS extension is linked against OpenMP, which the
  `python:3.12-slim` base image doesn't ship; `services/recommender` crash-looped on `import implicit.als` in the real
  container even though every local/pytest run (run as a normal user with a differently-resolved environment) was fine.
  Fixed by installing `libgomp1` via apt. Only surfaced once actually deployed via Docker — exactly why this phase's
  plan called for a real container rebuild, not just local pytest, before calling it done.
- **`numba` JIT caching needs a writable cache dir:** `librosa`'s internal `@jit(cache=True)` functions try to write
  their compiled-code cache next to the source file in `site-packages` by default — not writable by the non-root `app`
  user the worker container runs as, so every real upload failed audio-feature extraction with "cannot cache function:
  no locator available" (silently, since extraction is deliberately non-fatal — it only showed up by checking
  `tempo_bpm`/`energy` actually landed in Postgres after a real upload, not from any test or log scan). Fixed by
  pointing `NUMBA_CACHE_DIR` at `/tmp`. Local pytest never caught this either, for the same root cause as the
  `libgomp1` bug: it runs as a normal user with full write access to `site-packages`.
- **A capped recommendation list can get "topped up" past its cap:** made-for-you's ALS path correctly capped results
  to 2 tracks per artist, but the heuristic/new-releases fallback that fills remaining slots when ALS doesn't have
  enough data didn't know about that cap, so a thin ALS result could end up right back over the limit. Fixed by
  re-applying the cap once, after all three tiers (ALS, heuristic, fallback) are combined. Caught by a dedicated test
  seeding one artist with more tracks than the cap allows.

## Verified

This was run for real, four times over: rebuilding Phase 1 from scratch, Phase 2, Phase 3, and Phase 4 — on actual
Docker Desktop (Windows), not a simulated or sandboxed substitute.

- `docker compose up --build`: all 10 containers reach healthy/running.
- Full upload → transcode → stream pipeline exercised via curl against the live gateway: register, upload (tags read
  correctly from a real file), poll to `ready`, fetch the signed URL, master playlist, a variant playlist, a segment
  (right content-type and byte count), cover art, full-text search finding the track, a tampered/garbage token → 403,
  an unauthenticated request → 401.
- **Real-world import test:** 8 real MP3s (3–7 minutes each, Tamil film songs with multi-artist tags) copied from the
  user's own `Music` folder, imported via `scripts/import_folder.py` against the live stack. All 8 transcoded to `ready`
  with correct multi-artist tags and realistic durations within ~50 s on a single worker; re-running the import correctly
  skipped all 8 via SHA-256 dedup without re-uploading.
- MinIO bucket contents inspected directly (`mc ls`) — originals, HLS renditions (96/160 kbps; 320 kbps correctly
  skipped for the lower-bitrate sources), and cover art all present at the expected keys.
- **Browser end-to-end** (Playwright): the full flow — register, upload, wait for transcoding, play, pause/resume, seek,
  volume, quality switching, queue (add/next/jump), prefetch (next track needs no new URL request), persistent
  mini-player across page navigation, search, session persistence across reload, logout — passes against the live
  stack, run twice: once with `HLS_AUDIO_CODEC=mp3` (Playwright's bundled Chromium lacks an AAC decoder — confirmed via
  `canPlayType`), and once with the **actual production default**, `HLS_AUDIO_CODEC=aac`, using the real system Edge
  browser (confirmed via `canPlayType` to have full AAC support) — so the real default is verified end-to-end, not just
  the CI-friendly fallback.
- At the end of Phase 5: 117 Python tests and a clean `tsc --noEmit`/`next build`. **Today:** 224 Python tests
  (common 54, auth 16, catalogue 93 + 5 skipped without Postgres, stream 8, worker 26, events 12, recommender 15),
  11 site-Worker tests, 6 CDN-Worker tests and 24 frontend unit tests, all passing; the new interface states were also
  checked in a real browser with a mocked, deliberately slow API (skeletons, banner timing, sign-in state, seek-bar fill,
  reduced motion, sidebar collapse and persistence, panel layout).
- **Phase 3:** a full backend flow via curl against the live stack (lyrics extraction and retrieval, likes, follows,
  a collaborative playlist edited by a second account, reorder, recently-played ordering, made-for-you fallback); a
  Playwright spec covering playlists/likes/follow/lyrics; the Phase 2 player spec updated for the new multi-section
  Home page and rerun 4× consecutively *without* resetting the database to confirm no cross-run flakiness; screenshots
  confirming synced-lyrics highlighting tracks real playback position.
- **Phase 4:** the full events pipeline exercised via curl/Python against the live stack using a real uploaded track
  (events → Redis Stream → the events consumer → Postgres → analytics endpoints, all independently confirmed); two new
  Playwright specs (the client fires the right event with the right reason, confirmed via network interception; the
  analytics page renders seeded top-tracks/skip-rate/listening-by-hour data correctly); all four e2e specs passing
  together from a clean database; a screenshot that caught the bar-chart CSS bug above.
- **Phase 5:** migration 0004 (audio-feature columns + vector tables) verified on both SQLite (models-equivalence
  test) and real Postgres (upgrade → downgrade → upgrade cycle on a disposable container); a full clean rebuild from
  zero (`docker compose down`, every project image deleted, all Docker build cache pruned, every image rebuilt with
  `--no-cache`) — all 11 containers reach healthy/running including the new `recommender` service; a real upload
  through the live gateway confirmed `tempo_bpm`/`energy` land in Postgres after the fix below; `recommender` logged
  a real successful ALS training run against genuine accumulated interaction data; `/home/made-for-you` and
  `/tracks/<id>/similar` exercised via curl against the live stack, both the ALS path and every cold-start fallback
  tier; a new Playwright assertion opens the similar-tracks panel against the live stack and a screenshot confirms it
  renders correctly. Two real bugs only reproduced inside the actual container (never in local pytest, which runs
  under a different user/permissions model) — `libgomp.so.1` missing for `implicit`, and `numba`'s JIT cache needing
  a writable directory for `librosa` — plus a made-for-you artist-cap leak caught by a dedicated test. All described
  under "Key decisions" and covered by regression tests.
- Across all five phases, seven genuine bugs were found and fixed during this process (not pre-existing, caught by
  testing): FFmpeg wrote Windows-style backslash paths into generated HLS playlists (Phase 2); `0 or -1` duplicated a
  playlist's first track position (Phase 3); a UUID-as-string type mismatch and a CSS percentage-height bug (both
  Phase 4); missing `libgomp.so.1`, a non-writable `numba` JIT cache directory, and a made-for-you artist-cap leak
  (all Phase 5, all only caught by testing the real Docker container, not local pytest).

## Suggestions and shuffle

ALS (Phase 5) needs many listeners; with one user and a small library it learns very little. "Made for you" and
"similar tracks" are therefore a hybrid (`services/catalogue/app/taste.py`) that compares tracks on:

| Signal | Source |
|---|---|
| Audio fingerprint (timbre, harmony, brightness) | `librosa` in the worker, stored in `tracks.audio_vector` |
| **Mood** (sad ↔ happy, calm ↔ intense) | estimated from the fingerprint, see below |
| Tempo and loudness (half/double time counts as a match) | `librosa` |
| Artist relatedness | ListenBrainz "similar artists" (real listening sessions) and MusicBrainz tags, stored in `artist_meta` |
| Same film (album title), era, language | album tag, album date, Tamil script, and a built-in 80s/90s/modern Tamil singer and composer list (`tamil_seed.py`) |

**Named moods.** Every song also carries scores for Love, Break-up, Sad, Pleasant, Happy, Party, Mass, Vibe, Nostalgic, Devotional
and Motivational (`listens_common/emotion.py`); the strongest (up to three, clearly above your library's average) are its tags,
used by the Search > Mood chips and the similarity below. Songs get them from the lighter signals above (a direction on the
sad-happy / calm-energetic plane). Songs scored by `scripts/score_moods.py` on a computer with the CLAP audio model blend in a
learned model as well: CLAP turns two 6 s pieces of the song into a 512-number fingerprint, and a kernel-ridge regression
trained on 1,840 music clips that listeners rated on 28 emotions (Cowen et al. 2020, `baobaoh/13-dimensions-music-emotions`,
`scripts/train_emotion_model.py`) predicts those ratings, which are combined into the moods. Held-out accuracy (Spearman
correlation with listener ratings) is about 0.3 on average: good for energizing (0.55), calm (0.46) and sad (0.39), weak for
romantic (0.27) and joyful (0.29), and useless for "painful" (0.09, so no mood uses it). It was trained on Western clips, so
Tamil film songs are a domain shift: treat it as a second opinion, not ground truth. `MOOD_LEARNED_WEIGHT` (default 0.5) sets
its share and `0` switches it off with no deploy. Songs without scores are unaffected. Scoring needs the `0007` columns
(`mood_vector`, `moods`) and reads a few hundred KB per song from Backblaze:

```
.venv/Scripts/python scripts/score_moods.py                 # plan
.venv/Scripts/python scripts/score_moods.py --apply         # score songs that have no mood yet
```

**Mood-based.** Similar tracks follow the *mood* of the song you play, not its artist: a sad song's neighbours are sad
songs, whoever sings them. `listens_common/mood.py` places each song on two axes (valence: sad/dark ↔ happy/bright, arousal:
calm/soft ↔ energetic/intense, the circumplex model used in music-emotion research) from the stored fingerprint, so no new
audio processing is needed. Valence comes from key mode (major vs minor via the Krumhansl-Schmuckler key profiles on the
chroma means), brightness and tempo; arousal from loudness, brightness, percussiveness and tempo; each is z-scored across
*your* library. When both songs have a mood it is the leading similarity term (40%), artist closeness drops to 7%, a mood
mismatch scales the whole score down to 30%, and the co-listening (ALS) boost and the diversity re-ranking can no longer pull
in a song of the wrong mood. It is an estimate, not ground truth (mode is a weak signal for modal Indian film music, and the
beat tracker can halve or double tempo), which is why it is blended with the other signals. Checked on this library: a sad
seed (`Mannipaaya`) returns 10 of 10 melancholic songs, an upbeat one (`My Name is Billa`) 10 of 10 upbeat, across many artists.

Your taste is the songs you liked, saved or finished (repeats count more) minus early skips; a candidate scores by its
closeness to the nearest of those, so a 90s and a 2020s taste can coexist. Results are re-ranked (MMR) so they are
varied, and blended with ALS when it has trained. Everything degrades gracefully: a missing signal is skipped.

Fetch the open data for your library (dry run by default; needs `.env.prod` values in the environment):

```
.venv/Scripts/python scripts/enrich_library.py --apply            # artist relatedness + tags
.venv/Scripts/python scripts/enrich_library.py --audio --apply    # fingerprints for songs uploaded before this existed
```

**Shuffle** (`frontend/src/lib/shuffle.ts`, Settings > Playback > Shuffle > Smart) draws each next song at random but
weighted: liked songs a bit more often, songs played in the last 6 hours less, the same artist or film back to back far
less, and songs of similar energy and tempo to the previous one more, so a soft melody is not followed by a loud beat.
Nothing is excluded, it is a bias.

**Endless play** (`frontend/src/lib/endless.ts`, Settings > Playback > Endless play, on by default) keeps the music going. When
two songs or fewer are left, the player asks for songs similar in mood to the last one (`/tracks/{id}/similar`, widening to the
current song's neighbours and then Made for you when there are too few), drops anything already in the queue or heard in the last 3
hours (relaxing that only if the library is small), orders the rest with the smart shuffle (variety and flow) and quietly adds up
to 20. It is requested early, so there is no gap even while the free-tier server wakes; if the queue still runs out first, the
end of the queue waits for the same request and carries straight on. Unit tests: `cd frontend && npm run test:unit`.

## Next

Measure the suggestions against your listening (skip rate on recommended songs vs. the old ALS and heuristic picks), and
use all credited artists (not just the primary) in the recommender's artist graph and the listening-stats "top artist".
