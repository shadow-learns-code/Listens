"""The switch behind "Pause processing" in the app.

It stops the worker that runs on Render (the HTTP-triggered one, see services/worker/app/http.py), and only that one:
workers started on a laptop never look at it, so they keep converting songs. A song Render is already converting is
finished first, never cut off half way. The switch lives in Redis. If Redis cannot be reached, processing is treated as
running, so a Redis hiccup never freezes it.
"""
import logging

from redis import Redis
from redis.exceptions import RedisError

log = logging.getLogger("processing")

PAUSE_KEY = "processing:paused"


def is_paused(r: Redis) -> bool:
    try:
        return bool(r.exists(PAUSE_KEY))
    except RedisError:
        log.warning("could not read the processing switch; treating processing as running")
        return False


def set_paused(r: Redis, paused: bool) -> None:
    if paused:
        r.set(PAUSE_KEY, "1")
    else:
        r.delete(PAUSE_KEY)
