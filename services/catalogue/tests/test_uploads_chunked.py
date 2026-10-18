"""The resumable upload: pause/resume, repeats, cancel, restarts, and who can touch a session."""
import hashlib
import uuid

from listens_common import testing
from listens_common.config import get_settings
from listens_common.models import Track
from listens_common.storage import get_s3

from app import uploads

BASE = {"license": "Personal library", "rights_confirmed": True}
PART = uploads.PART_SIZE


def audio_bytes(env, name="song.mp3", padding=0, **tags) -> bytes:
    """A real MP3, optionally followed by zero padding to make it span several parts (players ignore trailing zeros)."""
    return testing.make_audio(env.dir / name, seconds=3, **tags).read_bytes() + b"\0" * padding


def start(env, filename, size, headers=None, **extra):
    return env.client.post("/uploads/sessions", json={"filename": filename, "size": size, **BASE, **extra}, headers=headers or env.headers)


def send(env, sid, n, data, headers=None):
    return env.client.put(f"/uploads/sessions/{sid}/parts/{n}", content=data[(n - 1) * PART : n * PART], headers=headers or env.headers)


def object_bytes(key) -> bytes:
    s = get_settings()
    return get_s3().get_object(Bucket=s.s3_bucket_audio, Key=key)["Body"].read()


def open_uploads() -> list:
    return get_s3().list_multipart_uploads(Bucket=get_settings().s3_bucket_audio).get("Uploads", [])


def test_pause_and_resume_across_parts_assembles_the_exact_file_and_creates_the_track(env):
    data = audio_bytes(env, title="Chunked Song", artist="The Parts", padding=11 * 1024 * 1024)  # 3 parts
    r = start(env, "song.mp3", len(data))
    assert r.status_code == 201, r.text
    session = r.json()
    sid = session["id"]
    assert (session["part_size"], session["total_parts"], session["received"]) == (PART, 3, [])

    assert send(env, sid, 1, data).json() == {"part": 1, "received": 1}
    assert send(env, sid, 2, data).status_code == 200
    # --- the user pauses here; later (maybe after a reload) the app asks what the server already has
    assert env.client.get(f"/uploads/sessions/{sid}", headers=env.headers).json()["received"] == [1, 2]
    # an interrupted part is simply sent again, and sending a part twice is harmless
    assert send(env, sid, 2, data).json()["received"] == 2
    assert send(env, sid, 3, data).json()["received"] == 3

    done = env.client.post(f"/uploads/sessions/{sid}/complete", headers=env.headers)
    assert done.status_code == 201, done.text
    body = done.json()
    assert body["duplicate"] is False
    assert (body["track"]["title"], body["track"]["artist"]["name"], body["track"]["status"]) == ("Chunked Song", "The Parts", "uploaded")

    track_id = body["track"]["id"]
    with env.sessions() as db:
        track = db.get(Track, uuid.UUID(track_id))
        assert track.content_sha256 == hashlib.sha256(data).hexdigest() and track.duration_ms
    assert object_bytes(f"originals/{track_id}/source.mp3") == data  # byte-for-byte what the phone sent
    jobs = env.redis.xrange("jobs:transcode")
    assert len(jobs) == 1 and jobs[0][1]["track_id"] == track_id  # queued for transcoding like any upload
    assert open_uploads() == []
    assert env.client.get(f"/uploads/sessions/{sid}", headers=env.headers).status_code == 404


def test_a_lost_answer_to_complete_is_answered_again_instead_of_failing(env):
    data = audio_bytes(env, title="Retry Me", artist="Flaky Net")
    sid = start(env, "retry.mp3", len(data)).json()["id"]
    send(env, sid, 1, data)
    first = env.client.post(f"/uploads/sessions/{sid}/complete", headers=env.headers)
    again = env.client.post(f"/uploads/sessions/{sid}/complete", headers=env.headers)  # the phone never heard the first answer
    assert (first.status_code, again.status_code) == (201, 201)
    assert first.json()["track"]["id"] == again.json()["track"]["id"]
    with env.sessions() as db:
        assert db.query(Track).count() == 1


def test_bad_parts_and_an_unfinished_upload_are_refused(env):
    data = audio_bytes(env, padding=6 * 1024 * 1024)  # 2 parts
    sid = start(env, "x.mp3", len(data)).json()["id"]
    assert env.client.put(f"/uploads/sessions/{sid}/parts/1", content=b"too short", headers=env.headers).status_code == 400
    assert env.client.put(f"/uploads/sessions/{sid}/parts/9", content=data[:PART], headers=env.headers).status_code == 400
    assert env.client.put(f"/uploads/sessions/{sid}/parts/0", content=data[:PART], headers=env.headers).status_code == 400
    send(env, sid, 1, data)
    early = env.client.post(f"/uploads/sessions/{sid}/complete", headers=env.headers)
    assert early.status_code == 409 and "1 of 2" in early.json()["detail"]
    with env.sessions() as db:
        assert db.query(Track).count() == 0  # nothing was created


def test_starting_validates_type_size_and_rights(env):
    assert start(env, "notes.txt", 100).status_code == 415
    assert start(env, "big.mp3", get_settings().max_upload_mb * 1024 * 1024 + 1).status_code == 413
    assert start(env, "a.mp3", 100, rights_confirmed=False).status_code == 400
    assert env.client.post("/uploads/sessions", json={"filename": "a.mp3", "size": 0, **BASE}, headers=env.headers).status_code == 422
    assert open_uploads() == []  # a refused start leaves nothing behind in storage


def test_a_repeat_is_discarded_at_the_end_and_never_becomes_an_object(env):
    data = audio_bytes(env, title="Once Only", artist="Solo")
    first = start(env, "a.mp3", len(data)).json()["id"]
    send(env, first, 1, data)
    assert env.client.post(f"/uploads/sessions/{first}/complete", headers=env.headers).status_code == 201

    second = start(env, "copy.mp3", len(data)).json()["id"]
    send(env, second, 1, data)
    r = env.client.post(f"/uploads/sessions/{second}/complete", headers=env.headers)
    assert r.status_code == 200 and r.json()["duplicate"] is True and "already in your library" in r.json()["reason"]
    s = get_settings()
    keys = [o["Key"] for o in get_s3().list_objects_v2(Bucket=s.s3_bucket_audio).get("Contents", [])]
    assert len(keys) == 1  # only the first song's original exists
    assert open_uploads() == []

    # "force" accepts a deliberate second version (different file, same song) ...
    other = data + b"\0"
    forced = start(env, "again.mp3", len(other), force=True).json()["id"]
    send(env, forced, 1, other)
    assert env.client.post(f"/uploads/sessions/{forced}/complete", headers=env.headers).status_code == 201
    # ... but never the very same file twice
    same = start(env, "same-bytes.mp3", len(data), force=True).json()["id"]
    send(env, same, 1, data)
    again = env.client.post(f"/uploads/sessions/{same}/complete", headers=env.headers)
    assert again.status_code == 200 and again.json()["duplicate"] is True


def test_cancel_throws_everything_away_and_can_be_repeated(env):
    data = audio_bytes(env, padding=6 * 1024 * 1024)
    sid = start(env, "x.mp3", len(data)).json()["id"]
    send(env, sid, 1, data)
    assert len(open_uploads()) == 1
    assert env.client.delete(f"/uploads/sessions/{sid}", headers=env.headers).status_code == 204
    assert open_uploads() == []
    assert env.client.get(f"/uploads/sessions/{sid}", headers=env.headers).status_code == 404
    assert env.client.delete(f"/uploads/sessions/{sid}", headers=env.headers).status_code == 204  # cancelling again is fine
    assert not (uploads.TMP_ROOT / sid).exists()


def test_nobody_else_can_see_send_to_complete_or_cancel_a_session(env):
    data = audio_bytes(env)
    sid = start(env, "mine.mp3", len(data)).json()["id"]
    other = env.other_headers
    assert env.client.get(f"/uploads/sessions/{sid}", headers=other).status_code == 404
    assert send(env, sid, 1, data, headers=other).status_code == 404
    assert env.client.post(f"/uploads/sessions/{sid}/complete", headers=other).status_code == 404
    env.client.delete(f"/uploads/sessions/{sid}", headers=other)  # looks cancelled to them, but must not touch mine
    assert env.client.get(f"/uploads/sessions/{sid}", headers=env.headers).status_code == 200
    assert env.client.get(f"/uploads/sessions/{sid}").status_code in (401, 403)


def test_it_still_completes_after_a_server_restart_lost_the_local_copy(env):
    data = audio_bytes(env, title="After Restart", artist="Survivor", padding=6 * 1024 * 1024)
    sid = start(env, "r.mp3", len(data)).json()["id"]
    send(env, sid, 1, data)
    send(env, sid, 2, data)
    (uploads.TMP_ROOT / sid).unlink()  # what a sleeping free-tier server does to its disk
    r = env.client.post(f"/uploads/sessions/{sid}/complete", headers=env.headers)
    assert r.status_code == 201, r.text
    assert object_bytes(f"originals/{r.json()['track']['id']}/source.mp3") == data
    assert r.json()["track"]["title"] == "After Restart"
