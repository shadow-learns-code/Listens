"""Song processing from the app: see what is waiting, pause it, resume it, or start it right now.

"Processing" is the conversion of an uploaded song (HLS streams, cover art, audio analysis) done by the worker. Pausing sets
the switch in listens_common.processing, which only the Render worker looks at: workers running on a laptop keep going.
A song that is already being converted is finished first, never cut off half way.
"""
import logging
import uuid

import requests
from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from redis import Redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from listens_common import processing
from listens_common.config import Settings, get_settings
from listens_common.db import get_db
from listens_common.models import Track
from listens_common.web import current_user_id, get_redis

log = logging.getLogger("catalogue")

router = APIRouter(prefix="/processing", tags=["processing"])

TICK_SECONDS = 240  # how long one request to the worker may keep draining the queue
MAX_TICKS = 3  # a free-tier worker converts one song at a time, so a few rounds cover a typical batch


class ProcessingOut(BaseModel):
    paused: bool
    waiting: int
    processing: int
    ready: int
    failed: int


class PauseIn(BaseModel):
    paused: bool


def _state(db: Session, r: Redis, user_id: uuid.UUID) -> ProcessingOut:
    counts = dict(db.execute(select(Track.status, func.count()).where(Track.uploader_id == user_id).group_by(Track.status)).all())
    return ProcessingOut(
        paused=processing.is_paused(r), waiting=counts.get("uploaded", 0), processing=counts.get("processing", 0),
        ready=counts.get("ready", 0), failed=counts.get("failed", 0),
    )


@router.get("", response_model=ProcessingOut)
def get_state(user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db), r: Redis = Depends(get_redis)) -> ProcessingOut:
    return _state(db, r, user_id)


@router.put("", response_model=ProcessingOut)
def set_state(
    body: PauseIn, user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db), r: Redis = Depends(get_redis),
) -> ProcessingOut:
    processing.set_paused(r, body.paused)
    return _state(db, r, user_id)


def run_worker(worker_url: str, r: Redis) -> None:
    """Ask the worker to drain the queue, again while songs are still waiting (and processing is not paused)."""
    for _ in range(MAX_TICKS):
        if processing.is_paused(r):
            return
        try:
            res = requests.post(f"{worker_url.rstrip('/')}/tick", params={"max_seconds": TICK_SECONDS}, timeout=TICK_SECONDS + 60)
            handled = res.json().get("handled", 0) if res.ok else 0
        except Exception:  # noqa: BLE001 - the worker may be asleep or busy; the 6-hourly tick and local workers still cover it
            log.warning("could not reach the worker at %s", worker_url, exc_info=True)
            return
        if handled == 0:
            return


@router.post("/start", response_model=ProcessingOut)
def start_now(
    tasks: BackgroundTasks, user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db),
    r: Redis = Depends(get_redis), settings: Settings = Depends(get_settings),
) -> ProcessingOut:
    """Resume (if paused) and wake the worker so waiting songs are processed now, not at the next scheduled run."""
    processing.set_paused(r, False)
    state = _state(db, r, user_id)
    if state.waiting:
        tasks.add_task(run_worker, settings.worker_url, r)
    return state
