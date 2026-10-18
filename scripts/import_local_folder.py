#!/usr/bin/env python3
"""Import a folder of songs straight into the live library from this machine.

Skips Render, Cloudflare and the job queue entirely: each file's original is uploaded to B2, a track
row is created exactly like POST /upload does, and the track is then transcoded HERE with the same
code the worker uses (HLS renditions, cover, tempo/energy, duplicate cleanup).

Files that are already in the library are skipped: byte-identical (same sha256) or the same song in a
different file (listens_common.dedupe: same title + artist, compatible album). Dry run by default.

    set -a; source .env.prod; set +a; export PYTHONUTF8=1
    .venv/Scripts/python scripts/import_local_folder.py "C:/Users/bhara/Music" --user Shadow            # dry run
    .venv/Scripts/python scripts/import_local_folder.py "C:/Users/bhara/Music" --user Shadow --apply    # do it

Needs DATABASE_URL and S3_* from the environment (.env.prod) and ffmpeg/ffprobe on PATH.
Safe to stop and re-run: finished files are skipped, anything half-done is retried.
"""
import argparse
import hashlib
import importlib.util
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "worker"))  # the worker's `app` package (jobs, transcode)

from sqlalchemy import func, or_, select  # noqa: E402

from app import jobs  # noqa: E402
from listens_common import albums, artists, dedupe  # noqa: E402
from listens_common.config import get_settings  # noqa: E402
from listens_common.db import get_sessionmaker  # noqa: E402
from listens_common.models import Album, Artist, Track, User  # noqa: E402
from listens_common.storage import get_s3  # noqa: E402

# Same extension list as the upload endpoint.
ALLOWED_EXT = {".mp3", ".flac", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".aac"}


def _load_tags_module():
    """services/catalogue/app/tags.py, loaded by path: it can't be imported as `app.tags` because
    the worker's package already owns the name `app`."""
    spec = importlib.util.spec_from_file_location("catalogue_tags", ROOT / "services" / "catalogue" / "app" / "tags.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tags_mod = _load_tags_module()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def find_or_create_artist(db, user_id, name):
    artist = db.scalar(select(Artist).where(Artist.owner_user_id == user_id, func.lower(Artist.name) == name.lower()))
    if artist is None:
        artist = Artist(name=name, owner_user_id=user_id)
        db.add(artist)
        db.flush()
    return artist


def find_or_create_album(db, artist, title):
    album = db.scalar(select(Album).where(Album.artist_id == artist.id, func.lower(Album.title) == title.lower()))
    if album is None:
        album = Album(artist_id=artist.id, title=title)
        db.add(album)
        db.flush()
    return album


def describe(path: Path):
    """(title, artist, album, genre) the way the upload endpoint would pick them."""
    with open(path, "rb") as fh:
        tags = tags_mod.read_tags(fh)
    title = (tags.get("title") or path.stem).strip()[:200]
    artist = (tags.get("artist") or "Unknown Artist").strip()[:200]
    album = tags.get("album")
    return title, artist, album.strip()[:200] if album else None, tags.get("genre")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path)
    ap.add_argument("--user", required=True, help="username or email of the account that owns the songs")
    ap.add_argument("--license", default="Personal library", help="license text stored on each track")
    ap.add_argument("--workers", type=int, default=2, help="tracks transcoded in parallel (default 2)")
    ap.add_argument("--apply", action="store_true", help="really import (default: dry run)")
    args = ap.parse_args()

    files = sorted(p for p in args.folder.rglob("*") if p.is_file() and p.suffix.lower() in ALLOWED_EXT)
    if not files:
        sys.exit(f"No audio files found under {args.folder}")

    sessions = get_sessionmaker()
    settings, s3 = get_settings(), get_s3()
    with sessions() as db:
        user = db.scalar(select(User).where(or_(func.lower(User.username) == args.user.lower(),
                                                func.lower(User.email) == args.user.lower())))
        if user is None:
            sys.exit(f"No user '{args.user}'")
        user_id = user.id

    print(f"{len(files)} audio files in {args.folder}; importing into '{args.user}' ({'APPLY' if args.apply else 'dry run'})")
    new_ids, skipped, seen_songs = [], {"identical": 0, "same song": 0, "same song in this folder": 0}, []

    with sessions() as db:
        for i, path in enumerate(files, 1):
            sha = sha256_of(path)
            existing = db.scalar(select(Track).where(Track.uploader_id == user_id, Track.content_sha256 == sha))
            if existing is not None:
                if existing.status == "uploaded" and args.apply:  # registered by an interrupted run: finish it
                    new_ids.append(existing.id)
                    print(f"[{i}/{len(files)}] resume   {path.name}")
                else:
                    skipped["identical"] += 1
                continue
            title, artist, album, genre = describe(path)
            with open(path, "rb") as fh:
                duration_ms = tags_mod.read_duration_ms(fh)
            # the same strict check the upload endpoint applies: exact and spelling/credit variants, against the library
            match = dedupe.check_duplicate(db, user_id, title=title, credit=artist, album=album, duration_ms=duration_ms)
            if match:
                skipped["same song"] += 1
                print(f"[{i}/{len(files)}] same song {artist} - {title}  ({match.reason})")
                continue
            # ... and against the songs already chosen from this folder
            me = dedupe.SongInfo.from_upload(title, artist, album, duration_ms)
            if artist.lower() != "unknown artist" and any(dedupe.likely_same_song(me, other) for other in seen_songs):
                skipped["same song in this folder"] += 1
                print(f"[{i}/{len(files)}] twice    {artist} - {title}")
                continue
            seen_songs.append(me)
            print(f"[{i}/{len(files)}] {'import  ' if args.apply else 'would   '} {artist} - {title}")
            if not args.apply:
                new_ids.append(None)
                continue

            track_id = uuid.uuid4()
            source_key = f"originals/{track_id}/source{path.suffix.lower()}"
            s3.upload_file(str(path), settings.s3_bucket_audio, source_key)
            people = artists.ensure_artists(db, user_id, artist)  # one artist per person in the credit ("A/B" = A and B)
            artist_row = people[0]
            album_row = albums.find_or_create_album(db, people, album) if album else None  # cleaned title; one album per film
            track = Track(id=track_id, artist_id=artist_row.id, album_id=album_row.id if album_row else None,
                          uploader_id=user_id, title=title, genre=genre, license=args.license, status="uploaded",
                          source_key=source_key, content_sha256=sha, duration_ms=duration_ms)
            db.add(track)
            db.flush()
            artists.set_track_artists(db, track, people)
            db.commit()
            new_ids.append(track_id)

    print(f"\nSkipped: {sum(skipped.values())} ({', '.join(f'{v} {k}' for k, v in skipped.items() if v) or 'none'})")
    print(f"To import: {len(new_ids)}")
    if not args.apply:
        print("Dry run - nothing uploaded or changed. Re-run with --apply to import.")
        return

    def process(tid):
        with sessions() as db:
            try:
                result = jobs.process_track(db, s3, settings, tid)
                return tid, result, None
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
                db.rollback()
                db.execute(Track.__table__.update().where(Track.id == tid).values(status="failed", error=str(exc)[:1000]))
                db.commit()
                return tid, "failed", str(exc)[:200]

    ok = failed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for n, (tid, result, err) in enumerate(pool.map(process, new_ids), 1):
            ok += result in ("ready", "skipped")
            failed += result == "failed"
            print(f"  processed {n}/{len(new_ids)}  {str(tid)[:8]}  {result}{'  ' + err if err else ''}")
    print(f"\nDone: {ok} ready/handled, {failed} failed.")


if __name__ == "__main__":
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    main()
