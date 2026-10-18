"""Transcode job pipeline: Redis Stream + consumer group, at-least-once delivery.

Track state machine:   uploaded --claim--> processing --> ready
                                              |--> (retry) uploaded, up to MAX_ATTEMPTS, then failed

Why a Stream instead of a list: XREADGROUP + XACK means a job is only removed once handled, and
XAUTOCLAIM lets a healthy worker take over jobs from one that crashed mid-transcode.
Handling is idempotent (the atomic claim below), so a duplicate delivery is harmless.
Finished jobs are XDEL'd, so the stream only ever holds *outstanding* work. The sweep relies on
that: a track in 'uploaded' that is not in the stream has lost its job and gets re-enqueued.
"""
import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from redis import Redis
from redis.exceptions import RedisError, ResponseError
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from listens_common import dedupe, storage
from listens_common.config import Settings
from listens_common.models import Track
from listens_common.storage import delete_prefix

from . import audio_features, transcode

log = logging.getLogger("worker")

# HLS segments are content-addressed by track id + hash-free sequence number and never change
# once written (a re-transcode deletes the old prefix first - see delete_prefix below), so they're
# safe to cache for a year. Playlists are small and rarely re-fetched, but must-revalidate keeps a
# stale master.m3u8 from lingering if a track is ever reprocessed. Covers change even less often.
CACHE_CONTROL = {".m3u8": "public, max-age=3600, must-revalidate", ".ts": "public, max-age=31536000, immutable"}
COVER_CACHE_CONTROL = "public, max-age=86400"

STREAM = "jobs:transcode"
GROUP = "transcoders"
MAX_ATTEMPTS = 3
CLAIM_IDLE_MS = 10 * 60 * 1000  # a job unacked this long belongs to a dead worker
STUCK_UPLOADED = timedelta(minutes=2)  # grace period before we suspect a lost enqueue
STUCK_PROCESSING = timedelta(minutes=45)  # worker died mid-run


def ensure_group(r: Redis) -> None:
    try:
        r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


BETTER_QUALITY_FACTOR = 1.15


def _looks_better_quality(s3, settings: Settings, track: Track, twin: Track) -> bool:
    """Whether the waiting original is clearly higher bitrate than the ready copy it repeats, estimated from its size
    and the song's length, so a better re-upload is still transcoded and replaces the old copy (dedupe keeps the best
    source). Unknown on either side counts as "maybe better": the normal after-transcode check then decides."""
    duration_ms = track.duration_ms or twin.duration_ms
    if not twin.source_bitrate_kbps or not duration_ms or not track.source_key:
        return True
    size = s3.head_object(Bucket=settings.s3_bucket_audio, Key=track.source_key)["ContentLength"]
    return size * 8 / duration_ms > twin.source_bitrate_kbps * BETTER_QUALITY_FACTOR  # bits per ms == kbit/s


UPLOAD_THREADS = 4  # gentle enough for a weak connection (a tethered laptop), still several times faster than one at a time


def process_track(db: Session, s3, settings: Settings, track_id) -> str:
    """Transcode one track. Returns 'ready' | 'skipped'. Raises on failure (caller handles retry)."""
    # Atomic claim: only one worker can flip uploaded -> processing.
    claimed = db.execute(
        update(Track).where(Track.id == track_id, Track.status == "uploaded")
        .values(status="processing", processing_started_at=datetime.now(UTC))
    ).rowcount
    db.commit()
    if not claimed:
        return "skipped"  # already processing/ready/failed, or the track was deleted

    track = db.get(Track, track_id)
    # Last line of defence against repeats: judged from tags alone, BEFORE the original is downloaded from B2 (which
    # counts against the daily download cap) and transcoded. A song that repeats one already ready is removed.
    try:
        twin = dedupe.find_ready_twin(db, track)
        if twin is not None and not _looks_better_quality(s3, settings, track, twin):
            log.info("track %s repeats ready track %s (%s); removing it without transcoding", track_id, twin.id, twin.title)
            dedupe.remove_track(db, s3, settings, track, keep=twin)
            return "skipped"
    except Exception:  # noqa: BLE001 - the check must never block a genuine song
        db.rollback()
        log.exception("repeat check failed for track %s; processing it normally", track_id)
    source_key = track.source_key
    # Converting a song takes minutes, and Neon closes a connection that sits idle that long ("server closed the connection
    # unexpectedly" at the final write, after all the work). Give the connection back now; the next use checks out a fresh one.
    db.commit()
    with tempfile.TemporaryDirectory(prefix="transcode-") as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / "source"
        s3.download_file(settings.s3_bucket_audio, source_key, str(src))

        info = transcode.probe(src)
        renditions = transcode.select_renditions(info.bitrate_kbps)
        out = tmp_dir / "hls"
        transcode.transcode_hls(src, out, renditions, settings.hls_audio_codec)

        prefix = f"hls/{track_id}"
        delete_prefix(s3, settings.s3_bucket_audio, prefix + "/")  # clean leftovers from a prior attempt
        def put(f: Path) -> None:
            ctype = "application/vnd.apple.mpegurl" if f.suffix == ".m3u8" else "video/mp2t"
            s3.upload_file(str(f), settings.s3_bucket_audio, f"{prefix}/{f.relative_to(out).as_posix()}",
                           ExtraArgs={"ContentType": ctype, "CacheControl": CACHE_CONTROL[f.suffix]})

        # A song is a few hundred small files; one at a time, each waits a full round trip. Four at once is several times faster
        # on a slow or distant connection (boto3 clients are thread-safe).
        with ThreadPoolExecutor(max_workers=UPLOAD_THREADS) as pool:
            for _ in pool.map(put, [f for f in sorted(out.rglob("*")) if f.is_file()]):
                pass

        cover_key = None
        cover = tmp_dir / "cover.jpg"
        if transcode.extract_cover(src, cover):
            cover_key = f"tracks/{track_id}.jpg"
            s3.upload_file(str(cover), settings.s3_bucket_covers, cover_key,
                           ExtraArgs={"ContentType": "image/jpeg", "CacheControl": COVER_CACHE_CONTROL})

        features = audio_features.extract_features(src)

    ready_values = dict(
        status="ready", hls_prefix=prefix, duration_ms=info.duration_ms,
        source_bitrate_kbps=info.bitrate_kbps, cover_key=cover_key, error=None,
        tempo_bpm=features.get("tempo_bpm"), energy=features.get("energy"), audio_vector=features.get("vector"),
    )
    for attempt in (1, 2):  # a dropped connection at this point would throw the whole conversion away: try once more
        try:
            db.execute(update(Track).where(Track.id == track_id).values(**ready_values))
            db.commit()
            break
        except DBAPIError:
            db.rollback()
            if attempt == 2:
                raise
            log.warning("database connection dropped while saving track %s; retrying once", track_id)
    log.info("track %s ready (%d ms, %s kbps, %d renditions)", track_id, info.duration_ms, info.bitrate_kbps, len(renditions))
    try:
        # Keep the best copy of the song, delete the others (possibly this very track). A failure
        # here must not fail the transcode: the track is already ready and playable.
        removed = dedupe.resolve_duplicates(db, s3, settings, track_id)
        if removed:
            log.info("removed %d duplicate cop%s of track %s: %s", len(removed), "y" if len(removed) == 1 else "ies", track_id, removed)
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("duplicate cleanup failed for track %s; leaving both copies", track_id)
    return "ready"


def handle_message(r: Redis, sessions: sessionmaker, s3, settings: Settings, msg_id: str, fields: dict) -> None:
    try:
        track_id, attempt = uuid.UUID(fields["track_id"]), int(fields.get("attempt", 0))
    except (KeyError, ValueError):
        log.error("dropping malformed job %s: %r", msg_id, fields)  # poison message: never retry
        r.xack(STREAM, GROUP, msg_id)
        r.xdel(STREAM, msg_id)
        return
    with sessions() as db:
        try:
            process_track(db, s3, settings, track_id)
        except Exception as exc:  # noqa: BLE001 - any failure must end in retry or 'failed'
            db.rollback()
            log.exception("track %s failed (attempt %d)", track_id, attempt + 1)
            if attempt + 1 < MAX_ATTEMPTS:
                db.execute(update(Track).where(Track.id == track_id, Track.status == "processing").values(status="uploaded"))
                db.commit()
                r.xadd(STREAM, {"track_id": str(track_id), "attempt": str(attempt + 1)})
            else:
                db.execute(
                    update(Track).where(Track.id == track_id).values(status="failed", error=str(exc)[:1000])
                )
                db.commit()
    r.xack(STREAM, GROUP, msg_id)
    r.xdel(STREAM, msg_id)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite returns naive datetimes


def requeue_stuck(r: Redis, sessions: sessionmaker) -> int:
    """Safety net: reset hung 'processing' tracks, and re-enqueue 'uploaded' tracks whose job
    is missing from the stream (lost enqueue). Tracks merely waiting in the queue are left alone."""
    now = datetime.now(UTC)
    n = 0
    queued = {fields["track_id"] for _, fields in r.xrange(STREAM)}
    with sessions() as db:
        for t in db.scalars(select(Track).where(Track.status == "processing")):
            if t.processing_started_at is None or now - _aware(t.processing_started_at) > STUCK_PROCESSING:
                t.status = "uploaded"
        db.commit()
        for t in db.scalars(select(Track).where(Track.status == "uploaded")):
            if str(t.id) not in queued and now - _aware(t.created_at) > STUCK_UPLOADED:
                r.xadd(STREAM, {"track_id": str(t.id), "attempt": "0"})
                n += 1
    return n


BLOCK_MS = 5000


def poll_once(r: Redis, sessions: sessionmaker, s3, settings: Settings, consumer: str, block_ms: int = BLOCK_MS) -> int:
    """Read and handle at most one job (blocking up to block_ms). Returns the number handled."""
    try:
        resp = r.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=1, block=block_ms)
    except ResponseError as exc:
        # Real Redis says "NOGROUP ..."; fakeredis words the same condition differently.
        if "NOGROUP" not in str(exc) and "requires the key to exist" not in str(exc):
            raise
        ensure_group(r)  # Redis restarted/flushed without our group: recreate it and carry on
        return 0
    handled = 0
    for _stream, messages in resp or []:
        for msg_id, fields in messages:
            handle_message(r, sessions, s3, settings, msg_id, fields)
            handled += 1
    return handled


STALE_UPLOAD_AFTER = timedelta(days=3)
STALE_UPLOAD_CHECK_EVERY = 6 * 3600
_last_stale_upload_check = 0.0


def sweep_stale_uploads(s3, settings: Settings) -> int:
    """Abort resumable uploads that nobody finished (see catalogue/app/uploads.py); checked at most every few hours."""
    global _last_stale_upload_check
    if time.monotonic() - _last_stale_upload_check < STALE_UPLOAD_CHECK_EVERY and _last_stale_upload_check:
        return 0
    _last_stale_upload_check = time.monotonic()
    try:
        n = storage.abort_stale_multipart(s3, settings.s3_bucket_audio, STALE_UPLOAD_AFTER)
    except Exception:  # noqa: BLE001 - housekeeping must never break the sweep
        log.exception("stale upload cleanup failed")
        return 0
    if n:
        log.info("aborted %d unfinished uploads older than %s", n, STALE_UPLOAD_AFTER)
    return n


def sweep(r: Redis, sessions: sessionmaker, s3, settings: Settings, consumer: str) -> None:
    sweep_stale_uploads(s3, settings)
    # take over jobs a crashed worker never acked, then re-enqueue lost ones
    _, claimed, *_ = r.xautoclaim(STREAM, GROUP, consumer, min_idle_time=CLAIM_IDLE_MS, count=10)
    for msg_id, fields in claimed:
        handle_message(r, sessions, s3, settings, msg_id, fields)
    if (n := requeue_stuck(r, sessions)):
        log.info("sweep re-enqueued %d stuck tracks", n)


def run_forever(r: Redis, sessions: sessionmaker, s3, settings: Settings, consumer: str) -> None:
    ensure_group(r)
    last_sweep = 0.0
    while True:
        try:
            if time.monotonic() - last_sweep > 60:
                last_sweep = time.monotonic()
                sweep(r, sessions, s3, settings, consumer)
            poll_once(r, sessions, s3, settings, consumer)
        except (RedisError, OSError):
            # Redis unreachable or restarting: back off and retry instead of dying.
            log.exception("redis error; retrying in 3s")
            time.sleep(3)
        except Exception:  # noqa: BLE001 - never let one bad iteration kill the worker
            log.exception("unexpected error in worker loop; continuing")
            time.sleep(1)
