#!/usr/bin/env python3
"""Transcode the songs waiting in the queue ('uploaded') on THIS computer, several at a time, within a download budget.

Reading each original from Backblaze counts against its daily download cap (1 GB/day on the free tier, shared with
playback), so the script sizes the whole queue with ONE list call, then takes songs oldest-first until the next one
would push the total over --budget-mb, and stops there. The rest stay queued for the next run (or Render's slow cron).
A song that fails (e.g. Neon dropped the connection) is put back in the queue, not marked failed.

    set -a; source .env.prod; set +a
    .venv/Scripts/python scripts/process_queue.py --budget-mb 450 --workers 2            # dry run: shows the plan
    .venv/Scripts/python scripts/process_queue.py --budget-mb 450 --workers 2 --apply

Uses the worker's own jobs.process_track (FFmpeg HLS + cover + librosa features), one short database session per
song because Neon closes connections that sit idle. Each worker needs roughly 300 MB of RAM.
"""
import argparse
import os
import sys
import time
from datetime import UTC, datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "libs" / "common"))
sys.path.insert(0, str(ROOT / "services" / "worker"))  # the worker's `app` package

from sqlalchemy import select, update  # noqa: E402

from app import jobs  # noqa: E402
from listens_common.config import get_settings  # noqa: E402
from listens_common.db import get_sessionmaker  # noqa: E402
from listens_common.models import Track  # noqa: E402
from listens_common.storage import get_s3  # noqa: E402


def original_sizes(s3, bucket: str) -> dict[str, int]:
    """{object key: bytes} for everything under originals/ (paginated list calls, no downloads)."""
    sizes, token = {}, None
    while True:
        kw = {"Bucket": bucket, "Prefix": "originals/", "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        sizes.update({o["Key"]: o["Size"] for o in page.get("Contents", [])})
        if not page.get("IsTruncated"):
            return sizes
        token = page["NextContinuationToken"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--budget-mb", type=float, default=450, help="stop before downloading more than this many MB of originals")
    ap.add_argument("--workers", type=int, default=2, help="songs transcoded in parallel (default 2)")
    ap.add_argument("--apply", action="store_true", help="really process (default: show the plan)")
    args = ap.parse_args()

    settings, s3, sessions = get_settings(), get_s3(), get_sessionmaker()
    # A run that was stopped (or a computer that went to sleep) mid-song leaves it 'processing' for good: nothing else puts it
    # back, so it would sit there for days. Anything 'processing' for longer than a conversion can take is stuck: re-queue it.
    with sessions() as db:
        stuck = db.execute(
            update(Track).where(Track.status == "processing", Track.processing_started_at < datetime.now(UTC) - jobs.STUCK_PROCESSING)
            .values(status="uploaded", processing_started_at=None)
        ).rowcount
        db.commit()
    if stuck:
        print(f"re-queued {stuck} song(s) stuck in 'processing' from an interrupted run", flush=True)
    sizes = original_sizes(s3, settings.s3_bucket_audio)
    with sessions() as db:
        waiting = [(t.id, t.title, t.source_key) for t in db.scalars(select(Track).where(Track.status == "uploaded").order_by(Track.created_at)).unique()]

    budget, used, plan = int(args.budget_mb * 1_000_000), 0, []
    for tid, title, key in waiting:
        size = sizes.get(key)
        if size is None:
            print(f"skip   {title}: original not found in B2 ({key})")
            continue
        if used + size > budget:
            break
        used += size
        plan.append((tid, title, size))
    left = len(waiting) - len(plan)
    print(f"{len(waiting)} songs waiting; this run: {len(plan)} songs, {used / 1e6:.0f} MB of downloads "
          f"(budget {args.budget_mb:.0f} MB), {left} stay queued; {args.workers} worker(s)", flush=True)
    if not args.apply:
        print("dry run - nothing processed (use --apply)")
        return

    def process(item):
        tid, title, size = item
        started = time.time()
        try:
            with sessions() as db:
                result = jobs.process_track(db, s3, settings, tid)
            return title, result, size, time.time() - started
        except Exception as exc:  # noqa: BLE001 - one bad song must not stop the run
            with sessions() as db:
                db.execute(update(Track).where(Track.id == tid, Track.status == "processing").values(status="uploaded"))
                db.commit()
            return title, f"FAILED, left queued ({str(exc)[:80]})", size, time.time() - started

    ready = failed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for n, (title, result, size, secs) in enumerate(pool.map(process, plan), 1):
            ready += result == "ready"
            failed += result.startswith("FAILED")
            print(f"[{n}/{len(plan)}] {title}: {result} ({size / 1e6:.1f} MB, {secs:.0f}s)", flush=True)
    print(f"finished: {ready} ready, {failed} failed and left queued, {left} not attempted (over budget)", flush=True)


if __name__ == "__main__":
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    main()
