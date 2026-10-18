"""HTTP entrypoint for the transcode worker, for deployments that can't run a long-lived
process (e.g. Render's free tier, which only bills/runs HTTP request-response services).
An external scheduler (see .github/workflows/cron-tick.yml) hits POST /tick periodically to
drain the queue a bit at a time, instead of the persistent jobs.run_forever() loop that
docker-compose still uses for local/self-hosted runs - that path is untouched.

A single real track can take minutes to transcode (CPU-bound FFmpeg work), which can outlast
a tick's own time budget or the platform's own request timeout. Neither loses work: the track
stays 'processing' and jobs.requeue_stuck() (called every tick via jobs.sweep) already resets
anything stuck past STUCK_PROCESSING on a later tick - the same recovery path an always-on
worker uses when it crashes mid-job.
"""
import logging
import socket
import time

import redis
from fastapi import Depends, FastAPI

from listens_common import processing
from listens_common.config import Settings, get_settings
from listens_common.db import get_sessionmaker
from listens_common.storage import get_s3

from . import jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("worker-http")

app = FastAPI(title="Shadow Listens - Worker (HTTP-triggered)")
_consumer = f"worker-{socket.gethostname()}"


def get_redis() -> redis.Redis:
    settings = get_settings()
    return redis.Redis.from_url(
        settings.redis_url, decode_responses=True,
        socket_timeout=15, socket_connect_timeout=5, health_check_interval=30,
    )


def get_sessions():
    return get_sessionmaker()


def get_s3_client():
    return get_s3()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/tick")
def tick(
    max_seconds: float = 120,
    block_ms: int = 2000,
    settings: Settings = Depends(get_settings),
    r: redis.Redis = Depends(get_redis),
    sessions=Depends(get_sessions),
    s3=Depends(get_s3_client),
) -> dict:
    """Reclaim stuck jobs, then drain the queue (one job per poll) until it's empty or
    max_seconds elapses. Bounded so a single invocation can't run forever.

    This is the Render worker. When processing is paused from the app (listens_common.processing) it does nothing; workers
    running on a laptop (scripts/run_local_worker.sh, scripts/process_queue.py) never look at the switch and keep going."""
    if processing.is_paused(r):
        log.info("tick skipped: processing is paused from the app")
        return {"handled": 0, "paused": True}
    jobs.ensure_group(r)
    jobs.sweep(r, sessions, s3, settings, _consumer)

    start = time.monotonic()
    handled = 0
    while time.monotonic() - start < max_seconds:
        if processing.is_paused(r):  # paused from the app while this run was going: finish the song just done, take no more
            log.info("tick stopping early: processing was paused from the app")
            break
        n = jobs.poll_once(r, sessions, s3, settings, _consumer, block_ms=block_ms)
        if n == 0:
            break
        handled += n
    log.info("tick handled %d job(s)", handled)
    return {"handled": handled}
