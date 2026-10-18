"""Helpers shared by service test-suites (not imported by production code)."""
import os
import socket
import subprocess
import uuid
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from .config import Settings
from .models import Base
from .security import create_token


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def configure_test_env() -> None:
    """Call at the top of a conftest, BEFORE importing the service (settings are cached)."""
    port = free_port()
    os.environ["S3_ENDPOINT_URL"] = f"http://127.0.0.1:{port}"
    os.environ["S3_ACCESS_KEY"] = "test"
    os.environ["S3_SECRET_KEY"] = "test"
    os.environ["_TEST_S3_PORT"] = str(port)


def start_s3_server():
    """In-process S3-compatible server (moto) standing in for MinIO. Returns the server."""
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=int(os.environ["_TEST_S3_PORT"]), verbose=False)
    server.start()
    return server


def make_buckets(settings: Settings) -> None:
    from .storage import get_s3

    get_s3.cache_clear()
    s3 = get_s3()
    for b in (settings.s3_bucket_audio, settings.s3_bucket_covers):
        s3.create_bucket(Bucket=b)


def clear_buckets(settings: Settings) -> None:
    from .storage import delete_prefix, get_s3

    s3 = get_s3()
    for b in (settings.s3_bucket_audio, settings.s3_bucket_covers):
        delete_prefix(s3, b, "")
        for upload in s3.list_multipart_uploads(Bucket=b).get("Uploads", []):  # unfinished resumable uploads too
            s3.abort_multipart_upload(Bucket=b, Key=upload["Key"], UploadId=upload["UploadId"])


def sqlite_sessionmaker():
    engine = create_engine("sqlite+pysqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def auth_header(settings: Settings, user_id: uuid.UUID) -> dict[str, str]:
    token, _, _ = create_token(settings, user_id, "access")
    return {"Authorization": f"Bearer {token}"}


def make_audio(path: Path, seconds: float = 3.0, *, title=None, artist=None, album=None, genre=None,
               cover: bool = False, bitrate: str | None = None) -> Path:
    """Synthesize a real tone file with ffmpeg (format from extension), optionally tagged."""
    plain = path.with_name(path.stem + "._plain" + path.suffix) if cover else path
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    for k, v in (("title", title), ("artist", artist), ("album", album), ("genre", genre)):
        if v:
            cmd += ["-metadata", f"{k}={v}"]
    if bitrate:
        cmd += ["-b:a", bitrate]
    cmd.append(str(plain))
    subprocess.run(cmd, check=True)
    if cover:  # embed a tiny JPEG as attached picture (separate pass: mixing a 1-frame input truncates audio)
        art = path.with_name(path.stem + "._cover.jpg")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", str(art)], check=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(plain), "-i", str(art), "-map", "0:a", "-map", "1:v",
                        "-c", "copy", "-disposition:v", "attached_pic", "-map_metadata", "0", str(path)], check=True)
    return path
