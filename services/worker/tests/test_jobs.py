from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app import jobs
from listens_common import testing
from listens_common.models import Track


def keys(env, bucket=None, prefix=""):
    resp = env.s3.list_objects_v2(Bucket=bucket or env.cfg.s3_bucket_audio, Prefix=prefix)
    return sorted(o["Key"] for o in resp.get("Contents", []))


def test_process_track_end_to_end(env):
    src = testing.make_audio(env.dir / "s.mp3", seconds=8, cover=True, bitrate="192k")
    tid = env.add_track(src)
    with env.sessions() as db:
        assert jobs.process_track(db, env.s3, env.cfg, tid) == "ready"
    t = env.track(tid)
    assert t.status == "ready" and t.error is None
    assert 7500 <= t.duration_ms <= 8600 and t.source_bitrate_kbps == 192
    assert t.hls_prefix == f"hls/{tid}" and t.cover_key == f"tracks/{tid}.jpg"
    assert t.energy is not None and t.energy > 0  # cold-start features extracted during processing
    got = keys(env, prefix=f"hls/{tid}/")
    assert f"hls/{tid}/master.m3u8" in got and f"hls/{tid}/q96/index.m3u8" in got
    assert f"hls/{tid}/q160/seg_001.ts" in got
    assert not any("/q320/" in k for k in got)  # 192k source -> no 320k rendition
    assert keys(env, env.cfg.s3_bucket_covers) == [f"tracks/{tid}.jpg"]
    playlist = env.s3.head_object(Bucket=env.cfg.s3_bucket_audio, Key=f"hls/{tid}/master.m3u8")
    assert playlist["ContentType"] == "application/vnd.apple.mpegurl"
    assert playlist["CacheControl"] == jobs.CACHE_CONTROL[".m3u8"]
    segment = env.s3.head_object(Bucket=env.cfg.s3_bucket_audio, Key=f"hls/{tid}/q160/seg_001.ts")
    assert segment["CacheControl"] == jobs.CACHE_CONTROL[".ts"]
    cover = env.s3.head_object(Bucket=env.cfg.s3_bucket_covers, Key=f"tracks/{tid}.jpg")
    assert cover["CacheControl"] == jobs.COVER_CACHE_CONTROL


def test_process_is_idempotent_and_skips_non_uploaded(env):
    tid = env.add_track(testing.make_audio(env.dir / "s.mp3", seconds=2))
    with env.sessions() as db:
        assert jobs.process_track(db, env.s3, env.cfg, tid) == "ready"
        assert jobs.process_track(db, env.s3, env.cfg, tid) == "skipped"  # duplicate delivery
    import uuid
    with env.sessions() as db:
        assert jobs.process_track(db, env.s3, env.cfg, uuid.uuid4()) == "skipped"  # deleted track


def run_message(env, tid, attempt=0):
    jobs.ensure_group(env.redis)
    env.redis.xadd(jobs.STREAM, {"track_id": str(tid), "attempt": str(attempt)})
    (_, msgs), = env.redis.xreadgroup(jobs.GROUP, "w1", {jobs.STREAM: ">"}, count=1)
    msg_id, fields = msgs[0]
    jobs.handle_message(env.redis, env.sessions, env.s3, env.cfg, msg_id, fields)
    return msg_id


def test_bad_file_is_retried_then_marked_failed_with_reason(env):
    junk = env.dir / "junk.mp3"
    junk.write_bytes(b"not audio" * 500)
    tid = env.add_track(junk)

    for attempt in range(jobs.MAX_ATTEMPTS - 1):
        run_message(env, tid, attempt)
        assert env.track(tid).status == "uploaded"  # reset for retry
        pending = env.redis.xrange(jobs.STREAM)
        assert len(pending) == 1 and pending[0][1]["attempt"] == str(attempt + 1)  # retry job queued
        env.redis.xtrim(jobs.STREAM, maxlen=0)  # drain so the next loop reads only its own message
    run_message(env, tid, jobs.MAX_ATTEMPTS - 1)
    t = env.track(tid)
    assert t.status == "failed" and "Not a readable media file" in t.error
    assert env.redis.xlen(jobs.STREAM) == 0  # finished jobs are removed
    assert env.redis.xpending(jobs.STREAM, jobs.GROUP)["pending"] == 0


def test_sweep_requeues_lost_jobs_but_not_queued_or_fresh_ones(env):
    old = datetime.now(UTC) - timedelta(hours=1)
    paths = [testing.make_audio(env.dir / f"{i}.mp3", seconds=1) for i in range(4)]
    lost, queued, fresh, hung = (env.add_track(p) for p in paths)
    with env.sessions() as db:
        db.execute(update(Track).where(Track.id.in_([lost, queued, hung])).values(created_at=old))
        db.execute(update(Track).where(Track.id == hung).values(
            status="processing", processing_started_at=old))
        db.commit()
    env.redis.xadd(jobs.STREAM, {"track_id": str(queued), "attempt": "0"})  # waiting in line, not lost

    assert jobs.requeue_stuck(env.redis, env.sessions) == 2  # `lost` and (reset) `hung`
    ids = sorted(f["track_id"] for _, f in env.redis.xrange(jobs.STREAM))
    assert ids == sorted(str(i) for i in (lost, queued, hung))
    assert str(fresh) not in ids
    assert env.track(hung).status == "uploaded"
    assert jobs.requeue_stuck(env.redis, env.sessions) == 0  # second sweep adds nothing


def test_long_running_track_with_old_created_at_is_not_reset(env):
    """Bulk imports: created hours ago but started processing just now must not be re-queued."""
    tid = env.add_track(testing.make_audio(env.dir / "s.mp3", seconds=1))
    with env.sessions() as db:
        db.execute(update(Track).where(Track.id == tid).values(
            status="processing", created_at=datetime.now(UTC) - timedelta(hours=5),
            processing_started_at=datetime.now(UTC)))
        db.commit()
    assert jobs.requeue_stuck(env.redis, env.sessions) == 0
    assert env.track(tid).status == "processing"


def test_malformed_job_is_dropped_not_retried(env):
    jobs.ensure_group(env.redis)
    for bad in ({"track_id": "not-a-uuid"}, {"nope": "1"}):
        env.redis.xadd(jobs.STREAM, bad)
        (_, msgs), = env.redis.xreadgroup(jobs.GROUP, "w1", {jobs.STREAM: ">"}, count=1)
        jobs.handle_message(env.redis, env.sessions, env.s3, env.cfg, *msgs[0])
    assert env.redis.xlen(jobs.STREAM) == 0
    assert env.redis.xpending(jobs.STREAM, jobs.GROUP)["pending"] == 0


def test_poll_once_idle_returns_zero_and_recovers_from_missing_group(env):
    jobs.ensure_group(env.redis)
    assert jobs.poll_once(env.redis, env.sessions, env.s3, env.cfg, "w1", block_ms=10) == 0  # idle queue is fine
    env.redis.delete(jobs.STREAM)  # simulate Redis losing state (restart without persistence)
    assert jobs.poll_once(env.redis, env.sessions, env.s3, env.cfg, "w1", block_ms=10) == 0  # NOGROUP -> recreate
    tid = env.add_track(testing.make_audio(env.dir / "p.mp3", seconds=2))
    env.redis.xadd(jobs.STREAM, {"track_id": str(tid), "attempt": "0"})
    assert jobs.poll_once(env.redis, env.sessions, env.s3, env.cfg, "w1", block_ms=10) == 1
    assert env.track(tid).status == "ready"


def test_a_lower_quality_copy_is_deleted_once_it_is_ready(env):
    """The existing ready copy is 320 kbps; the new 192 kbps upload of the same song loses and goes."""
    from sqlalchemy import update

    twin = env.add_track(testing.make_audio(env.dir / "twin.mp3", seconds=8, bitrate="320k"), status="ready")
    with env.sessions() as db:
        db.execute(update(Track).where(Track.id == twin).values(source_bitrate_kbps=320, duration_ms=8000, title="t"))
        db.commit()
    new = env.add_track(testing.make_audio(env.dir / "new.mp3", seconds=8, bitrate="192k"))
    with env.sessions() as db:
        # clearly lower quality than the ready copy: dropped before it is transcoded at all
        assert jobs.process_track(db, env.s3, env.cfg, new) == "skipped"
    assert env.track(new) is None and env.track(twin) is not None
    assert keys(env, prefix=f"hls/{new}/") == []  # nothing of the loser was transcoded or kept in storage


def test_a_higher_quality_new_copy_replaces_the_old_one(env):
    from sqlalchemy import update

    old = env.add_track(testing.make_audio(env.dir / "old.mp3", seconds=8, bitrate="96k"), status="ready")
    with env.sessions() as db:
        db.execute(update(Track).where(Track.id == old).values(source_bitrate_kbps=96, duration_ms=8000, title="t"))
        db.commit()
    new = env.add_track(testing.make_audio(env.dir / "better.mp3", seconds=8, bitrate="192k"))
    with env.sessions() as db:
        assert jobs.process_track(db, env.s3, env.cfg, new) == "ready"
    assert env.track(old) is None and env.track(new).status == "ready"


def test_a_repeat_of_a_ready_song_is_removed_before_its_original_is_downloaded_or_transcoded(env):
    kept = env.add_track(testing.make_audio(env.dir / "keep.mp3", seconds=2), status="ready", key="originals/keep/source.mp3")
    repeat = env.add_track(testing.make_audio(env.dir / "again.mp3", seconds=2), key="originals/again/source.mp3")
    with env.sessions() as db:
        db.execute(update(Track).where(Track.id == kept).values(title="Mazhai Kuruvi", duration_ms=2000, source_bitrate_kbps=320))
        db.execute(update(Track).where(Track.id == repeat).values(title="Mazhai Kuruvi (From Film)"))  # same song, spelling noise
        db.commit()
        assert jobs.process_track(db, env.s3, env.cfg, repeat) == "skipped"
    assert env.track(repeat) is None  # the repeat is gone
    assert env.track(kept).status == "ready"
    assert keys(env, prefix="originals/again/") == []  # its original was deleted from storage
    assert keys(env, prefix=f"hls/{repeat}/") == []  # and nothing was transcoded


def test_a_different_song_by_the_same_artist_is_still_processed(env):
    env.add_track(testing.make_audio(env.dir / "a.mp3", seconds=2), status="ready", key="originals/a/source.mp3")
    other = env.add_track(testing.make_audio(env.dir / "b.mp3", seconds=3), key="originals/b/source.mp3")
    with env.sessions() as db:
        db.execute(update(Track).where(Track.status == "ready").values(title="First Song"))
        db.execute(update(Track).where(Track.id == other).values(title="Completely Different"))
        db.commit()
        assert jobs.process_track(db, env.s3, env.cfg, other) == "ready"


def test_unfinished_resumable_uploads_are_aborted_after_a_few_days_but_fresh_ones_are_kept():
    from datetime import UTC, datetime, timedelta

    from listens_common.storage import abort_stale_multipart

    now = datetime.now(UTC)

    class FakeS3:
        """Two pages of unfinished uploads with known ages (the in-process test S3 stamps every upload with a fixed old date)."""

        def __init__(self):
            self.aborted = []
            self.pages = {
                None: {"Uploads": [{"Key": "originals/old1/source.mp3", "UploadId": "a", "Initiated": now - timedelta(days=5)},
                                   {"Key": "originals/fresh/source.mp3", "UploadId": "b", "Initiated": now - timedelta(hours=2)}],
                       "IsTruncated": True, "NextKeyMarker": "k", "NextUploadIdMarker": "m"},
                "k": {"Uploads": [{"Key": "originals/old2/source.mp3", "UploadId": "c", "Initiated": now - timedelta(days=4)}], "IsTruncated": False},
            }

        def list_multipart_uploads(self, **kw):
            return self.pages[kw.get("KeyMarker")]

        def abort_multipart_upload(self, Bucket, Key, UploadId):
            self.aborted.append(UploadId)

    fake = FakeS3()
    assert abort_stale_multipart(fake, "bucket", timedelta(days=3)) == 2
    assert sorted(fake.aborted) == ["a", "c"]  # the two old ones, across both pages; the 2-hour-old upload is kept


def test_the_stale_upload_cleanup_really_runs_inside_the_sweep(env):
    """Guards the wiring (an import slip once made it fail silently): the sweep calls it and it reaches storage."""
    from unittest.mock import patch

    jobs._last_stale_upload_check = 0.0
    with patch.object(jobs.storage, "abort_stale_multipart", return_value=2) as abort:
        assert jobs.sweep_stale_uploads(env.s3, env.cfg) == 2
    abort.assert_called_once()
    assert jobs.sweep_stale_uploads(env.s3, env.cfg) == 0  # not again within the next few hours


def test_the_database_connection_is_released_before_the_long_conversion_and_a_dropped_one_is_retried(env):
    """Neon closes a connection that idles for minutes: the session must not hold one across the conversion, and the final
    write is retried once if the connection died anyway."""
    from unittest.mock import patch

    from sqlalchemy.exc import OperationalError

    tid = env.add_track(testing.make_audio(env.dir / "long.mp3", seconds=2))
    seen = {}
    original = jobs.transcode.transcode_hls

    def spy(*a, **kw):
        seen["in_transaction_during_conversion"] = env_session.in_transaction()
        return original(*a, **kw)

    dropped = {"n": 0}
    with env.sessions() as env_session:
        real_execute = env_session.execute

        def flaky_execute(stmt, *a, **kw):
            # the first UPDATE after the conversion (the "mark it ready" write) hits a connection Neon has closed
            if seen.get("in_transaction_during_conversion") is not None and getattr(stmt, "is_update", False) and not dropped["n"]:
                dropped["n"] += 1
                raise OperationalError("UPDATE", {}, Exception("server closed the connection unexpectedly"))
            return real_execute(stmt, *a, **kw)

        with patch.object(jobs.transcode, "transcode_hls", spy), patch.object(env_session, "execute", flaky_execute):
            assert jobs.process_track(env_session, env.s3, env.cfg, tid) == "ready"
    assert seen["in_transaction_during_conversion"] is False  # connection handed back before the minutes-long work
    assert dropped["n"] == 1  # the first final write "died" and the second succeeded
    assert env.track(tid).status == "ready"
