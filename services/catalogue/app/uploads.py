"""Resumable, chunked uploads: pause, resume or cancel a song's upload, and survive a dropped connection.

Why not one big POST: a phone on mobile data loses the connection mid-file, the free server sleeps, and a single request
cannot be paused. Instead the app cuts the file into 5 MiB parts and sends them one at a time:

    POST   /uploads/sessions                   start (checks type and size), returns the session and the part size
    GET    /uploads/sessions/{id}              which parts the server already has (what "resume" asks first)
    PUT    /uploads/sessions/{id}/parts/{n}    one part (idempotent: sending a part again just replaces it)
    POST   /uploads/sessions/{id}/complete     assemble, run the same checks as /upload, create the track
    DELETE /uploads/sessions/{id}              cancel and throw everything away

Memory stays small on both ends: the phone reads one 5 MiB slice at a time, and the server holds one part per request.
Parts go straight into a Backblaze multipart upload, so progress survives the free server sleeping or restarting (the
session lives in Redis, the bytes in B2). The server also keeps a local copy of the parts so the final checks (hash, tags,
repeat detection) do not have to download the file again; if that copy is gone (a restart) it is fetched from B2 instead.

Nothing is created in the library until `complete`, and a repeat of an existing song is discarded then, before it ever
becomes an object in the bucket.
"""
import hashlib
import json
import logging
import mimetypes
import os
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from redis import Redis
from sqlalchemy.orm import Session

from listens_common.config import Settings, get_settings
from listens_common.db import get_db
from listens_common.models import Track
from listens_common.storage import get_s3
from listens_common.web import current_user_id, get_redis

from . import ingest
from .schemas import UploadOut
from .serialize import track_out

log = logging.getLogger("catalogue")

router = APIRouter(prefix="/uploads/sessions", tags=["uploads"])

PART_SIZE = 5 * 1024 * 1024  # the S3/B2 minimum for every part but the last
SESSION_TTL = 3 * 24 * 3600
DONE_TTL = 24 * 3600
TMP_ROOT = Path(tempfile.gettempdir()) / "listens-uploads"


class SessionIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    license: str = Field(min_length=2, max_length=100)
    rights_confirmed: bool
    title: str | None = Field(None, max_length=200)
    artist: str | None = Field(None, max_length=200)
    album: str | None = Field(None, max_length=200)
    genre: str | None = Field(None, max_length=60)
    force: bool = False


class SessionOut(BaseModel):
    id: str
    filename: str
    size: int
    part_size: int
    total_parts: int
    received: list[int]


def total_parts(size: int) -> int:
    return max(1, -(-size // PART_SIZE))


def part_length(size: int, n: int) -> int:
    return min(PART_SIZE, size - (n - 1) * PART_SIZE)


def _meta_key(sid: str) -> str:
    return f"upload:{sid}:meta"


def _parts_key(sid: str) -> str:
    return f"upload:{sid}:parts"


def _done_key(sid: str) -> str:
    return f"upload:{sid}:done"


def _received(r: Redis, sid: str) -> list[int]:
    return sorted(int(n) for n in r.hkeys(_parts_key(sid)))


def _load(r: Redis, sid: str, user_id: uuid.UUID) -> dict:
    raw = r.get(_meta_key(sid))
    meta = json.loads(raw) if raw else None
    if meta is None or meta["user_id"] != str(user_id):  # someone else's session looks exactly like a missing one
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload session not found")
    return meta


def _forget(r: Redis, sid: str) -> None:
    r.delete(_meta_key(sid), _parts_key(sid))
    try:
        (TMP_ROOT / sid).unlink(missing_ok=True)
    except OSError:
        log.warning("could not remove the temporary file for upload %s", sid)


def _session_out(sid: str, meta: dict, received: list[int]) -> SessionOut:
    return SessionOut(id=sid, filename=meta["filename"], size=meta["size"], part_size=PART_SIZE,
                      total_parts=total_parts(meta["size"]), received=received)


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
def start(
    body: SessionIn, user_id: uuid.UUID = Depends(current_user_id), r: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> SessionOut:
    if not body.rights_confirmed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You must confirm you own the rights to this audio")
    ext = ingest.extension(body.filename)
    if ext not in ingest.ALLOWED_EXT:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"Unsupported file type; allowed: {sorted(ingest.ALLOWED_EXT)}")
    if body.size > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {settings.max_upload_mb} MB")

    track_id = uuid.uuid4()
    key = f"originals/{track_id}/source{ext}"
    content_type = mimetypes.guess_type(body.filename)[0] or "application/octet-stream"
    upload = get_s3().create_multipart_upload(Bucket=settings.s3_bucket_audio, Key=key, ContentType=content_type)
    sid = uuid.uuid4().hex
    meta = {
        "user_id": str(user_id), "filename": body.filename, "size": body.size, "track_id": str(track_id), "key": key,
        "s3_upload_id": upload["UploadId"],
        "fields": body.model_dump(include={"license", "title", "artist", "album", "genre", "force"}),
    }
    r.set(_meta_key(sid), json.dumps(meta), ex=SESSION_TTL)
    return _session_out(sid, meta, [])


@router.get("/{sid}", response_model=SessionOut)
def status_of(sid: str, user_id: uuid.UUID = Depends(current_user_id), r: Redis = Depends(get_redis)) -> SessionOut:
    meta = _load(r, sid, user_id)
    return _session_out(sid, meta, _received(r, sid))


def _store_part(meta: dict, sid: str, n: int, body: bytes, settings: Settings, r: Redis) -> None:
    response = get_s3().upload_part(
        Bucket=settings.s3_bucket_audio, Key=meta["key"], UploadId=meta["s3_upload_id"], PartNumber=n, Body=body,
    )
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / sid
    with open(path, "r+b" if path.exists() else "wb") as fh:
        fh.seek((n - 1) * PART_SIZE)
        fh.write(body)
    r.hset(_parts_key(sid), str(n), response["ETag"])
    r.expire(_parts_key(sid), SESSION_TTL)


@router.put("/{sid}/parts/{n}")
async def put_part(
    sid: str, n: int, request: Request, user_id: uuid.UUID = Depends(current_user_id), r: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict:
    meta = _load(r, sid, user_id)
    if not 1 <= n <= total_parts(meta["size"]):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Part number must be between 1 and {total_parts(meta['size'])}")
    expected = part_length(meta["size"], n)
    body = await request.body()
    if len(body) != expected:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Part {n} must be exactly {expected} bytes (got {len(body)})")
    await run_in_threadpool(_store_part, meta, sid, n, body, settings, r)
    return {"part": n, "received": len(_received(r, sid))}


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _remember(r: Redis, sid: str, track: Track, duplicate: bool, reason: str | None) -> None:
    """If the answer to `complete` is lost on a flaky connection, the retry gets the same answer instead of a 404."""
    r.set(_done_key(sid), json.dumps({"track_id": str(track.id), "duplicate": duplicate, "reason": reason}), ex=DONE_TTL)


@router.post("/{sid}/complete", response_model=UploadOut)
def complete(
    sid: str, response: Response, user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db),
    r: Redis = Depends(get_redis), settings: Settings = Depends(get_settings),
) -> UploadOut:
    raw = r.get(_meta_key(sid))
    if raw is None:
        done = r.get(_done_key(sid))
        track = db.get(Track, uuid.UUID(json.loads(done)["track_id"])) if done else None
        if track is None or track.uploader_id != user_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload session not found")
        info = json.loads(done)
        response.status_code = status.HTTP_200_OK if info["duplicate"] else status.HTTP_201_CREATED
        return UploadOut(track=track_out(track, settings, user_id), duplicate=info["duplicate"], reason=info.get("reason"))

    meta = _load(r, sid, user_id)
    have = {int(k): v for k, v in r.hgetall(_parts_key(sid)).items()}
    missing = [n for n in range(1, total_parts(meta["size"]) + 1) if n not in have]
    if missing:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{len(missing)} of {total_parts(meta['size'])} parts have not arrived yet")

    s3, bucket, key = get_s3(), settings.s3_bucket_audio, meta["key"]

    def assemble() -> None:
        s3.complete_multipart_upload(
            Bucket=bucket, Key=key, UploadId=meta["s3_upload_id"],
            MultipartUpload={"Parts": [{"PartNumber": n, "ETag": have[n]} for n in sorted(have)]},
        )

    local = TMP_ROOT / sid
    assembled = False
    if not (local.exists() and local.stat().st_size == meta["size"]):
        # the server restarted since the parts arrived: build the object and read it back (rare)
        assemble()
        assembled = True
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        s3.download_file(bucket, key, str(local))

    fields = ingest.Fields(**meta["fields"])
    with open(local, "rb") as fh:
        prepared, match = ingest.inspect(db, user_id, fileobj=fh, filename=meta["filename"], digest=_sha256_of(local), fields=fields)

    if match:  # a repeat: discard the upload, never create the object
        if assembled:
            s3.delete_object(Bucket=bucket, Key=key)
        else:
            s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=meta["s3_upload_id"])
        _forget(r, sid)
        _remember(r, sid, match.track, True, match.reason)
        response.status_code = status.HTTP_200_OK
        return UploadOut(track=track_out(match.track, settings, user_id), duplicate=True, reason=match.reason)

    def store() -> None:
        if not assembled:
            assemble()

    def unstore() -> None:
        s3.delete_object(Bucket=bucket, Key=key)

    track = ingest.commit_track(
        db, r, user_id, prepared, track_id=uuid.UUID(meta["track_id"]), source_key=key, store=store, unstore=unstore,
    )
    _forget(r, sid)
    _remember(r, sid, track, False, None)
    log.info("uploaded track %s (%d bytes, %d parts) by %s", track.id, meta["size"], len(have), user_id)
    response.status_code = status.HTTP_201_CREATED
    return UploadOut(track=track_out(track, settings, user_id), duplicate=False)


@router.delete("/{sid}", status_code=status.HTTP_204_NO_CONTENT)
def cancel(
    sid: str, user_id: uuid.UUID = Depends(current_user_id), r: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    raw = r.get(_meta_key(sid))
    meta = json.loads(raw) if raw else None
    if meta is not None and meta["user_id"] == str(user_id):
        try:
            get_s3().abort_multipart_upload(Bucket=settings.s3_bucket_audio, Key=meta["key"], UploadId=meta["s3_upload_id"])
        except Exception:  # noqa: BLE001 - already gone is fine; the stale-upload sweep catches anything left
            log.warning("could not abort the multipart upload for session %s", sid)
        _forget(r, sid)
    return Response(status_code=status.HTTP_204_NO_CONTENT)  # cancelling twice, or a session that expired, is not an error
