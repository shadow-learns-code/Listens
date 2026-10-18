"""Turning a finished audio file into a track: shared by the one-shot /upload and the resumable /uploads/sessions flow.

Two steps so a repeat is refused BEFORE anything is stored in B2:

  inspect()       read the tags, lyrics and length, and run the strict repeat check (dedupe.check_duplicate)
  commit_track()  create the artists/album/track rows, store the audio (a callback, because the two upload flows store it
                  differently), commit, and queue the transcode job
"""
import logging
import os
import uuid
from dataclasses import dataclass
from typing import BinaryIO, Callable

from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from listens_common import albums as albumlib, artists, dedupe
from listens_common.models import Track

from .tags import read_duration_ms, read_lyrics, read_tags

log = logging.getLogger("catalogue")

TRANSCODE_STREAM = "jobs:transcode"
ALLOWED_EXT = {".mp3", ".flac", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".aac"}


@dataclass
class Fields:
    """What the uploader typed (all optional except the licence); tags in the file fill the gaps."""

    license: str
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    genre: str | None = None
    force: bool = False


@dataclass
class Prepared:
    title: str
    artist: str
    album: str | None
    genre: str | None
    license: str
    duration_ms: int | None
    lyrics: dict
    digest: str


def extension(filename: str | None) -> str:
    return os.path.splitext(filename or "")[1].lower()


def inspect(
    db: Session, user_id: uuid.UUID, *, fileobj: BinaryIO, filename: str | None, digest: str, fields: Fields,
) -> tuple[Prepared | None, dedupe.DuplicateMatch | None]:
    """(prepared, None) for a new song, or (None, match) when it repeats one already in the library."""
    tags = read_tags(fileobj)
    lyrics = read_lyrics(fileobj)
    stem = os.path.splitext(os.path.basename(filename or "Untitled"))[0]
    # Explicit values win over embedded tags, which win over the filename / "Unknown Artist".
    title = (fields.title or tags.get("title") or stem).strip()[:200]
    artist = (fields.artist or tags.get("artist") or "Unknown Artist").strip()[:200]
    album = (fields.album or tags.get("album") or None)
    album = album.strip() if album else None
    duration_ms = read_duration_ms(fileobj)
    if fields.force:
        # "force" lets a deliberate second version of a song through, but never the same file twice (the library is unique per
        # uploader and file content)
        same = db.scalar(select(Track).where(Track.uploader_id == user_id, Track.content_sha256 == digest))
        match = dedupe.DuplicateMatch(same, "this exact file is already in your library") if same is not None else None
    else:
        match = dedupe.check_duplicate(db, user_id, title=title, credit=artist, album=album, duration_ms=duration_ms, sha256=digest)
    if match:
        return None, match
    return Prepared(
        title=title, artist=artist, album=album, genre=(fields.genre or tags.get("genre") or None),
        license=fields.license.strip(), duration_ms=duration_ms, lyrics=lyrics, digest=digest,
    ), None


def commit_track(
    db: Session, r: Redis, user_id: uuid.UUID, p: Prepared, *, track_id: uuid.UUID, source_key: str,
    store: Callable[[], None], unstore: Callable[[], None],
) -> Track:
    """Create the rows and put the audio in storage. `store` makes the original exist at `source_key`; `unstore` removes it
    again if the database write then fails, so a failed upload leaves nothing behind."""
    # one artist per person in the credit ("A/B", "A, B" and "A;B" all credit A and B); the first is the primary artist
    people = artists.ensure_artists(db, user_id, p.artist)
    # the album title without store boilerplate; the same film under another first-listed singer is the same album
    album_row = albumlib.find_or_create_album(db, people, p.album) if p.album else None

    store()
    track = Track(
        id=track_id, artist_id=people[0].id, album_id=album_row.id if album_row else None, uploader_id=user_id,
        title=p.title, genre=p.genre, license=p.license, status="uploaded", source_key=source_key,
        content_sha256=p.digest, duration_ms=p.duration_ms,
        lyrics=p.lyrics.get("lyrics"), lyrics_synced=p.lyrics.get("lyrics_synced"),
    )
    db.add(track)
    db.flush()
    artists.set_track_artists(db, track, people)
    try:
        db.commit()
    except Exception:
        db.rollback()
        unstore()
        raise
    # If this enqueue is lost (Redis down), the worker's periodic sweep re-queues stuck 'uploaded' tracks.
    try:
        r.xadd(TRANSCODE_STREAM, {"track_id": str(track_id), "attempt": "0"})
    except Exception:
        log.exception("could not enqueue transcode job for %s; the worker sweep will pick it up", track_id)
    db.refresh(track)
    return track
