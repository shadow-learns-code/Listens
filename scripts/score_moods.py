#!/usr/bin/env python3
"""Fingerprint songs with the CLAP audio model and store their predicted emotions (tracks.mood_vector, tracks.moods).

This is the heavy step of mood detection, so it runs on a computer that has the model (your laptop), not on the free web
services. Songs that have not been scored yet still get a mood from the lighter signals the worker stores, so nothing waits
for this script; scoring makes the mood better.

    set -a; source .env.prod; set +a
    .venv/Scripts/python scripts/score_moods.py                    # dry run: shows what would be scored
    .venv/Scripts/python scripts/score_moods.py --apply            # score every ready song that has no mood yet
    .venv/Scripts/python scripts/score_moods.py --apply --all      # re-score everything (after retraining the model)
    .venv/Scripts/python scripts/score_moods.py --apply --from-npz fingerprints.npz   # store fingerprints made earlier

Reads two 6-second pieces from the middle of each song (a few hundred KB from Backblaze per song), embeds them, averages,
and stores the result. Needs torch + transformers and the CLAP model (laion/larger_clap_music, downloaded once to the
Hugging Face cache). One short database session per song, because Neon closes connections that sit idle.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "libs" / "common"))

import numpy as np  # noqa: E402
from sqlalchemy import select, update  # noqa: E402

from listens_common import emotion  # noqa: E402
from listens_common.config import get_settings  # noqa: E402
from listens_common.db import get_sessionmaker  # noqa: E402
from listens_common.models import Track  # noqa: E402
from listens_common.storage import get_s3  # noqa: E402

CLAP_MODEL = "laion/larger_clap_music"
SEGMENT_SECONDS, FRACTIONS, SAMPLE_RATE = 6, (0.30, 0.60), 48000


def segment_audio(s3, bucket: str, track_id: str, index: int):
    """(mono audio at 48 kHz, bytes read) of one 6 s HLS segment, or (None, 0)."""
    for rendition in ("q96", "q160"):
        try:
            body = s3.get_object(Bucket=bucket, Key=f"hls/{track_id}/{rendition}/seg_{index:03d}.ts")["Body"].read()
        except Exception:  # noqa: BLE001 - this rendition or segment does not exist
            continue
        with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
            f.write(body)
            path = f.name
        try:
            raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"], capture_output=True, check=True).stdout
        finally:
            os.unlink(path)
        return np.frombuffer(raw, dtype=np.float32), len(body)
    return None, 0


class Embedder:
    def __init__(self) -> None:
        try:
            import torch
            from transformers import ClapModel, ClapProcessor
        except ImportError as exc:
            raise SystemExit(f"scoring needs torch and transformers on this computer ({exc}). Run it on the laptop that has them.")
        self.torch = torch
        torch.set_num_threads(int(os.environ.get("SCORE_THREADS", "4")))
        self.processor = ClapProcessor.from_pretrained(CLAP_MODEL)
        self.model = ClapModel.from_pretrained(CLAP_MODEL).eval()

    def fingerprint(self, clips: list) -> np.ndarray:
        torch = self.torch
        with torch.no_grad():
            inputs = self.processor(audio=clips, sampling_rate=SAMPLE_RATE, return_tensors="pt")
            e = self.model.get_audio_features(**inputs)
            e = e.pooler_output if hasattr(e, "pooler_output") else e
            e = torch.nn.functional.normalize(e, dim=-1).mean(dim=0)
            return torch.nn.functional.normalize(e, dim=-1).numpy()


def store(sessions, track_id, fingerprint) -> None:
    vector, moods = emotion.payload(fingerprint)
    with sessions() as db:
        db.execute(update(Track).where(Track.id == track_id).values(mood_vector=vector, moods=moods))
        db.commit()


def run(apply: bool = False, everything: bool = False, limit: int | None = None, from_npz: str | None = None, budget_mb: float = 60) -> int:
    settings, s3, sessions = get_settings(), get_s3(), get_sessionmaker()
    with sessions() as db:
        query = select(Track.id, Track.title, Track.duration_ms).where(Track.status == "ready", Track.duration_ms.is_not(None))
        if not everything:
            query = query.where(Track.moods.is_(None))
        todo = [(str(i), t, d) for i, t, d in db.execute(query.order_by(Track.created_at))]
    if limit:
        todo = todo[:limit]

    if from_npz:
        z = np.load(from_npz, allow_pickle=True)
        known = dict(zip((str(i) for i in z["ids"].tolist()), z["emb"]))
        todo = [(i, t, d) for i, t, d in todo if i in known]
        print(f"{len(todo)} songs have a fingerprint in {from_npz}", flush=True)
        if apply:
            for n, (tid, title, _) in enumerate(todo, 1):
                store(sessions, tid, known[tid])
                if n % 20 == 0 or n == len(todo):
                    print(f"[{n}/{len(todo)}] stored", flush=True)
        else:
            print("dry run - nothing stored (use --apply)")
        return len(todo)

    print(f"{len(todo)} songs to score", flush=True)
    if not apply or not todo:
        if not apply:
            print("dry run - nothing scored (use --apply)")
        return 0
    embedder, read, done, t0 = Embedder(), 0, 0, time.time()
    for n, (tid, title, duration_ms) in enumerate(todo, 1):
        if read > budget_mb * 1_000_000:
            print(f"stopping: {read / 1e6:.0f} MB read from Backblaze (budget {budget_mb:.0f} MB); the rest wait for the next run", flush=True)
            break
        clips = []
        for frac in FRACTIONS:
            index = max(1, int(frac * duration_ms / 1000 / SEGMENT_SECONDS) + 1)
            audio, size = segment_audio(s3, settings.s3_bucket_audio, tid, index)
            read += size
            if audio is not None and audio.size > SAMPLE_RATE:
                clips.append(audio[: SAMPLE_RATE * 10])
        if not clips:
            print(f"[{n}/{len(todo)}] {title}: no audio found, skipped", flush=True)
            continue
        try:
            store(sessions, tid, embedder.fingerprint(clips))
            done += 1
        except Exception as exc:  # noqa: BLE001 - one bad song or a dropped connection must not stop the run
            print(f"[{n}/{len(todo)}] {title}: FAILED ({str(exc)[:80]}), will be retried next run", flush=True)
            continue
        if done % 10 == 0 or n == len(todo):
            print(f"[{n}/{len(todo)}] scored; {read / 1e6:.1f} MB read from Backblaze, {time.time() - t0:.0f}s", flush=True)
    print(f"finished: {done} scored", flush=True)
    return done


if __name__ == "__main__":
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="really score and store (default: show the plan)")
    ap.add_argument("--all", action="store_true", help="re-score songs that already have a mood")
    ap.add_argument("--limit", type=int, help="score at most this many songs")
    ap.add_argument("--from-npz", help="store fingerprints computed earlier (ids + emb arrays) instead of reading audio")
    ap.add_argument("--budget-mb", type=float, default=60, help="stop after reading this many MB from Backblaze")
    a = ap.parse_args()
    run(apply=a.apply, everything=a.all, limit=a.limit, from_npz=a.from_npz, budget_mb=a.budget_mb)
