import uuid

from listens_common import testing
from listens_common.config import get_settings
from listens_common.models import Album, Artist, Playlist, Track
from listens_common.storage import get_s3

from .test_upload import post


def seed(env, title="Song", artist="Band", album=None, status="ready", uploader=None, genre=None, cover=False):
    with env.sessions() as db:
        a = db.query(Artist).filter_by(name=artist).first() or Artist(name=artist)
        db.add(a)
        db.flush()
        al = None
        if album:
            al = db.query(Album).filter_by(title=album, artist_id=a.id).first() or Album(title=album, artist_id=a.id)
            db.add(al)
            db.flush()
        t = Track(artist_id=a.id, album_id=al.id if al else None, title=title, status=status, genre=genre,
                  uploader_id=uploader or env.user_id, duration_ms=1000,
                  cover_key="tracks/x.jpg" if cover else None)
        db.add(t)
        db.commit()
        return str(t.id), str(a.id), str(al.id) if al else None


def test_list_only_ready_and_mine_shows_all(env):
    seed(env, "ready one")
    seed(env, "still processing", status="processing")
    seed(env, "broken", status="failed")
    seed(env, "theirs", status="processing", uploader=env.other_id)
    titles = [t["title"] for t in env.client.get("/tracks", headers=env.headers).json()]
    assert titles == ["ready one"]
    mine = {t["title"]: t["status"] for t in env.client.get("/tracks/mine", headers=env.headers).json()}
    assert mine == {"ready one": "ready", "still processing": "processing", "broken": "failed"}


def test_get_track_visibility_and_cover_url(env):
    tid, *_ = seed(env, "p", status="processing")
    assert env.client.get(f"/tracks/{tid}", headers=env.headers).status_code == 200  # uploader sees it
    assert env.client.get(f"/tracks/{tid}", headers=env.other_headers).status_code == 404
    assert env.client.get(f"/tracks/{uuid.uuid4()}", headers=env.headers).status_code == 404
    rid, *_ = seed(env, "r", cover=True)
    t = env.client.get(f"/tracks/{rid}", headers=env.other_headers).json()
    assert t["cover_url"].startswith("/api/stream/cover/")


def test_cover_url_is_stable_across_requests_so_the_browser_can_cache_it(env):
    # Same track, same user, two separate requests a moment apart: the signed cover token must
    # be byte-for-byte identical, or the browser can never cache the (unchanging) image - it'd
    # see a "new" URL on every single list/page that shows this track and re-download every time.
    rid, *_ = seed(env, "r", cover=True)
    first = env.client.get(f"/tracks/{rid}", headers=env.headers).json()["cover_url"]
    second = env.client.get(f"/tracks/{rid}", headers=env.headers).json()["cover_url"]
    assert first == second


def test_artist_and_album_pages(env):
    t1, art, alb = seed(env, "One", "Band", "LP")
    seed(env, "Two", "Band", "LP")
    seed(env, "Hidden", "Band", "LP", status="processing")
    a = env.client.get(f"/artists/{art}", headers=env.headers).json()
    assert a["name"] == "Band" and {t["title"] for t in a["tracks"]} == {"One", "Two"}
    b = env.client.get(f"/albums/{alb}", headers=env.headers).json()
    assert b["title"] == "LP" and b["artist"]["name"] == "Band" and len(b["tracks"]) == 2
    assert env.client.get(f"/artists/{uuid.uuid4()}", headers=env.headers).status_code == 404


def test_list_albums_groups_tracks_and_skips_unready_only_albums(env):
    seed(env, "One", "Band", "LP")
    seed(env, "Two", "Band", "LP")
    seed(env, "Solo", "Band")  # no album: must not appear
    seed(env, "Unreleased", "Band", "Lost Tapes", status="processing")  # no ready tracks: must not appear

    out = env.client.get("/albums", headers=env.headers).json()
    titles = {a["title"] for a in out}
    assert titles == {"LP"}
    lp = next(a for a in out if a["title"] == "LP")
    assert lp["artist"]["name"] == "Band" and lp["track_count"] == 2


def test_delete_track_removes_objects_and_enforces_owner(env):
    f = testing.make_audio(env.dir / "del.mp3", title="Del")
    tid = post(env, f).json()["track"]["id"]
    assert env.client.delete(f"/tracks/{tid}", headers=env.other_headers).status_code == 403
    assert env.client.delete(f"/tracks/{tid}", headers=env.headers).status_code == 204
    assert env.client.get(f"/tracks/{tid}", headers=env.headers).status_code == 404
    s = get_settings()
    assert "Contents" not in get_s3().list_objects_v2(Bucket=s.s3_bucket_audio)


def test_search_fallback_matches_title_artist_genre_and_hides_unready(env):
    seed(env, "Midnight Drive", "Neon Owls", genre="synthwave")
    seed(env, "Sunrise", "Neon Owls")
    seed(env, "Midnight Secret", "Other", status="processing")
    with env.sessions() as db:
        db.add(Playlist(owner_id=env.user_id, name="Midnight mix", is_public=False))
        db.add(Playlist(owner_id=env.other_id, name="Midnight public", is_public=True))
        db.add(Playlist(owner_id=env.other_id, name="Midnight private", is_public=False))
        db.commit()

    def q(text):
        return env.client.get("/search", params={"q": text}, headers=env.headers).json()

    assert [t["title"] for t in q("midnight")["tracks"]] == ["Midnight Drive"]
    assert {t["title"] for t in q("neon")["tracks"]} == {"Midnight Drive", "Sunrise"}  # via artist name
    assert [a["name"] for a in q("owl")["artists"]] == ["Neon Owls"]
    assert [t["title"] for t in q("SYNTH")["tracks"]] == ["Midnight Drive"]
    assert {p["name"] for p in q("midnight")["playlists"]} == {"Midnight mix", "Midnight public"}
    assert q("100%")["tracks"] == []  # LIKE wildcards are escaped


# ------------------------------------------------------- search filters

def test_search_can_be_limited_to_artist_album_or_genre(env):
    seed(env, "Alpha", artist="Rahman", album="Guru", genre="Soundtrack")
    seed(env, "Beta", artist="Rahman", album="Roja", genre="Folk")
    seed(env, "Guru Song", artist="Other", album="Else", genre="Pop")  # "guru" only in a TITLE, and "Rahman" nowhere
    s = lambda q, by: env.client.get("/search", params={"q": q, "by": by}, headers=env.headers).json()  # noqa: E731

    by_artist = s("rahman", "artist")
    assert sorted(t["title"] for t in by_artist["tracks"]) == ["Alpha", "Beta"]
    assert [a["name"] for a in by_artist["artists"]] == ["Rahman"] and by_artist["albums"] == []

    by_album = s("guru", "album")  # the album, not the song that merely has "Guru" in its title
    assert [t["title"] for t in by_album["tracks"]] == ["Alpha"]
    assert [a["title"] for a in by_album["albums"]] == ["Guru"]

    assert [t["title"] for t in s("folk", "genre")["tracks"]] == ["Beta"]
    assert s("nothing-here", "genre")["tracks"] == []
    assert env.client.get("/search", params={"q": "x", "by": "bogus"}, headers=env.headers).status_code == 422


def test_search_by_mood_finds_songs_of_that_mood_and_facets_list_what_exists(env):
    import numpy as np

    major = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
    minor = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
    ids = {}
    for k in range(6):  # filler songs so the library has a spread to compare against
        ids[f"mid{k}"] = seed(env, f"mid{k}", artist="Filler", genre="Pop")[0]
    ids["sad"] = seed(env, "Slow Minor", artist="Sad Singer", genre="Soundtrack")[0]
    ids["happy"] = seed(env, "Fast Major", artist="Happy Singer", genre="Soundtrack")[0]

    def vec(chroma, centroid, zcr):
        return [0.0] * 13 + list(map(float, chroma)) + [0.0] * 7 + [centroid, zcr]

    spec = {f"mid{k}": (vec(np.roll(major if k % 2 else minor, k), 2000 + 100 * k, 0.08), 100 + 4 * k, 0.05 + 0.002 * k) for k in range(6)}
    spec["sad"] = (vec(minor, 1200, 0.04), 60, 0.02)
    spec["happy"] = (vec(major, 3200, 0.14), 150, 0.12)
    with env.sessions() as db:
        for key, (v, tempo, energy) in spec.items():
            db.query(Track).filter_by(id=uuid.UUID(ids[key])).update({"audio_vector": v, "tempo_bpm": tempo, "energy": energy})
        db.commit()

    s = lambda q: env.client.get("/search", params={"q": q, "by": "mood"}, headers=env.headers).json()["tracks"]  # noqa: E731
    assert s("sad")[0]["title"] == "Slow Minor"  # the clearest example of the mood comes first
    assert s("party")[0]["title"] == "Fast Major"  # everyday words map to a mood
    assert s("banana") == []
    f = env.client.get("/search/facets", headers=env.headers).json()
    assert {m["name"] for m in f["moods"]} >= {"Sad", "Happy"}
    assert {"name": "Soundtrack", "count": 2} in f["genres"]


def test_scored_songs_are_found_by_everyday_words_and_the_chips_list_the_named_moods(env):
    """Songs scored by the emotion model (scripts/score_moods.py) carry predicted ratings; mood search and the filter chips
    use them: "heartbreak" finds the song rated sad, bittersweet and longing, and Break-up appears as a chip."""
    import numpy as np

    from listens_common import emotion

    dims = emotion.dimensions()
    rng = np.random.default_rng(7)
    ids = {}
    for k in range(8):
        ids[k] = seed(env, f"Filler {k}", artist="Filler")[0]
    ids["hurt"] = seed(env, "Broken Heart", artist="Sad Singer")[0]
    with env.sessions() as db:
        for key, tid in ids.items():
            ratings = {d: float(rng.normal(5, 0.3)) for d in dims}
            if key == "hurt":
                ratings.update({"sad/depressing": 8.5, "bittersweet": 8.5, "tender/longing": 8.0})
            db.query(Track).filter_by(id=uuid.UUID(tid)).update({"moods": {"ratings": ratings}})
        db.commit()

    found = env.client.get("/search", params={"q": "heartbreak", "by": "mood"}, headers=env.headers).json()["tracks"]
    assert found and found[0]["title"] == "Broken Heart"
    assert env.client.get("/search", params={"q": "break-up", "by": "mood"}, headers=env.headers).json()["tracks"][0]["title"] == "Broken Heart"
    chips = env.client.get("/search/facets", headers=env.headers).json()["moods"]
    assert "Break-up" in {m["name"] for m in chips}
    assert all({"id", "name", "query", "count"} <= set(m) for m in chips)
