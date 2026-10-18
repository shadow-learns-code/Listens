from fastapi.testclient import TestClient

from app import jobs
from app.http import app, get_redis, get_sessions
from listens_common import testing


def client_for(env):
    app.dependency_overrides[get_redis] = lambda: env.redis
    app.dependency_overrides[get_sessions] = lambda: env.sessions
    return TestClient(app)


def test_health():
    assert TestClient(app).get("/health").json() == {"status": "ok"}


def test_tick_drains_the_queue_and_returns_how_many_it_handled(env):
    c = client_for(env)
    try:
        src = testing.make_audio(env.dir / "s.mp3", seconds=2)
        tid = env.add_track(src)
        jobs.ensure_group(env.redis)
        env.redis.xadd(jobs.STREAM, {"track_id": str(tid), "attempt": "0"})

        r = c.post("/tick", params={"max_seconds": 10, "block_ms": 50})
        assert r.status_code == 200
        assert r.json() == {"handled": 1}
        assert env.track(tid).status == "ready"
    finally:
        app.dependency_overrides.clear()


def test_tick_on_an_empty_queue_returns_zero_quickly(env):
    c = client_for(env)
    try:
        r = c.post("/tick", params={"max_seconds": 10, "block_ms": 50})
        assert r.status_code == 200
        assert r.json() == {"handled": 0}
    finally:
        app.dependency_overrides.clear()


def test_paused_from_the_app_stops_the_render_worker_but_not_a_laptop_worker(env):
    from listens_common import processing

    c = client_for(env)
    try:
        tid = env.add_track(testing.make_audio(env.dir / "p.mp3", seconds=2))
        jobs.ensure_group(env.redis)
        env.redis.xadd(jobs.STREAM, {"track_id": str(tid), "attempt": "0"})
        processing.set_paused(env.redis, True)

        # the Render worker (this HTTP entry point) does nothing while paused
        assert c.post("/tick", params={"max_seconds": 10, "block_ms": 50}).json() == {"handled": 0, "paused": True}
        assert env.track(tid).status == "uploaded"

        # a laptop worker (jobs.poll_once, as run by run_local_worker.sh) ignores the switch and converts the song
        assert jobs.poll_once(env.redis, env.sessions, env.s3, env.cfg, "laptop", block_ms=50) == 1
        assert env.track(tid).status == "ready"

        # resumed: the Render worker works again
        processing.set_paused(env.redis, False)
        tid2 = env.add_track(testing.make_audio(env.dir / "q.mp3", seconds=2))
        env.redis.xadd(jobs.STREAM, {"track_id": str(tid2), "attempt": "0"})
        assert c.post("/tick", params={"max_seconds": 10, "block_ms": 50}).json() == {"handled": 1}
    finally:
        app.dependency_overrides.clear()
