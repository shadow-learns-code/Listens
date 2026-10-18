"""Song processing from the app: counts, pause/resume, and "start now"."""
from unittest.mock import patch

from listens_common import processing

from .test_catalogue import seed


def state(env):
    return env.client.get("/processing", headers=env.headers).json()


def test_it_counts_only_my_songs_by_stage_and_reports_the_switch(env):
    seed(env, "a", status="uploaded")
    seed(env, "b", status="uploaded")
    seed(env, "c", status="processing")
    seed(env, "d", status="ready")
    seed(env, "e", status="failed")
    seed(env, "theirs", status="uploaded", uploader=env.other_id)
    assert state(env) == {"paused": False, "waiting": 2, "processing": 1, "ready": 1, "failed": 1}


def test_pause_and_resume_flip_the_shared_switch(env):
    r = env.client.put("/processing", json={"paused": True}, headers=env.headers)
    assert r.status_code == 200 and r.json()["paused"] is True
    assert processing.is_paused(env.redis) is True  # this is what every worker looks at
    assert env.client.put("/processing", json={"paused": False}, headers=env.headers).json()["paused"] is False
    assert processing.is_paused(env.redis) is False


def test_start_now_resumes_and_asks_the_worker_to_drain_the_queue(env):
    seed(env, "waiting", status="uploaded")
    processing.set_paused(env.redis, True)

    class Reply:
        ok = True
        calls = 0

        def json(self):
            Reply.calls += 1
            return {"handled": 1 if Reply.calls == 1 else 0}  # one song, then the queue is empty

    with patch("app.processing_api.requests.post", return_value=Reply()) as post:
        r = env.client.post("/processing/start", headers=env.headers)
    assert r.status_code == 200 and r.json()["paused"] is False and r.json()["waiting"] == 1
    assert processing.is_paused(env.redis) is False
    assert post.call_count == 2 and post.call_args.args[0].endswith("/tick")  # second call found nothing left and stopped


def test_start_now_with_nothing_waiting_does_not_wake_the_worker(env):
    seed(env, "done", status="ready")
    with patch("app.processing_api.requests.post") as post:
        assert env.client.post("/processing/start", headers=env.headers).status_code == 200
    post.assert_not_called()


def test_an_unreachable_worker_does_not_break_start(env):
    seed(env, "waiting", status="uploaded")
    with patch("app.processing_api.requests.post", side_effect=OSError("asleep")):
        assert env.client.post("/processing/start", headers=env.headers).status_code == 200


def test_pausing_while_a_run_is_going_stops_further_rounds(env):
    from app import processing_api

    processing.set_paused(env.redis, True)
    with patch("app.processing_api.requests.post") as post:
        processing_api.run_worker("http://worker", env.redis)
    post.assert_not_called()


def test_it_needs_a_login(env):
    assert env.client.get("/processing").status_code in (401, 403)
    assert env.client.put("/processing", json={"paused": True}).status_code in (401, 403)
    assert env.client.post("/processing/start").status_code in (401, 403)
