import numpy as np

from listens_common import mood

C_MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
C_MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def vec(chroma, centroid=2000.0, zcr=0.08):
    """A 34-dim fingerprint with the layout the worker writes: 13 MFCC, 12 chroma, 7 contrast, centroid, zcr."""
    return [0.0] * 13 + list(chroma) + [0.0] * 7 + [centroid, zcr]


def test_major_and_minor_keys_are_told_apart_in_any_key():
    for shift in (0, 3, 7):
        assert mood.mode_score(np.roll(C_MAJOR_PROFILE, shift)) > 0.3
        assert mood.mode_score(np.roll(C_MINOR_PROFILE, shift)) < -0.3
    assert mood.mode_score([1.0] * 12) == 0.0  # flat chroma has no key
    assert mood.mode_score([0.5, 0.2]) == 0.0  # malformed input is neutral, not an error


def test_slow_dark_minor_is_sad_and_fast_bright_major_is_upbeat():
    raw = {}
    for k in range(6):  # a spread of filler songs so z-scores have something to compare against
        raw[f"mid{k}"] = (vec(np.roll(C_MAJOR_PROFILE if k % 2 else C_MINOR_PROFILE, k), 2000 + 100 * k, 0.08), 100 + 4 * k, 0.05 + 0.002 * k)
    raw["sad"] = (vec(C_MINOR_PROFILE, 1200, 0.04), 60, 0.02)
    raw["happy"] = (vec(C_MAJOR_PROFILE, 3200, 0.14), 150, 0.12)
    m = mood.compute_moods(raw)
    assert m["sad"].valence < -0.8 and m["sad"].arousal < -0.8 and m["sad"].label == "melancholic"
    assert m["happy"].valence > 0.8 and m["happy"].arousal > 0.8 and m["happy"].label == "upbeat"
    assert mood.match(m["sad"], m["sad"]) == 1.0
    assert mood.match(m["sad"], m["happy"]) < 0.05


def test_too_few_songs_or_missing_fingerprints_give_no_mood():
    assert mood.compute_moods({"a": (vec(C_MAJOR_PROFILE), 100, 0.05)}) == {}
    assert mood.compute_moods({str(i): (None, 100, 0.05) for i in range(8)}) == {}


def test_missing_tempo_or_energy_still_gives_a_mood():
    raw = {str(i): (vec(np.roll(C_MAJOR_PROFILE, i), 2000 + 150 * i), None if i % 2 else 100 + i, None if i % 3 == 0 else 0.05 + 0.01 * i) for i in range(8)}
    assert len(mood.compute_moods(raw)) == 8


# ---- named moods (Love, Break-up, Sad ...) and the learned model --------------------------------------------------------
import numpy as np  # noqa: E402

from listens_common import emotion  # noqa: E402


def test_every_song_gets_named_mood_scores_from_the_light_signals_alone():
    raw = {f"t{i}": (np.random.default_rng(i).normal(size=34).tolist(), 70 + 10 * i, 0.02 * (i + 1)) for i in range(8)}
    moods = mood.compute_moods(raw)
    assert len(moods) == 8
    for m in moods.values():
        assert m.scores is not None and len(m.scores) == len(emotion.MOOD_NAMES)
    slow, fast = moods["t0"], moods["t7"]  # slowest/quietest vs fastest/loudest
    assert mood.score_for(slow, "Sad") > mood.score_for(fast, "Sad")
    assert mood.score_for(fast, "Party") > mood.score_for(slow, "Party")


def test_resolve_mood_name_accepts_the_names_and_everyday_words():
    assert mood.resolve_mood_name("Break-up") == "Break-up"
    assert mood.resolve_mood_name("  HEARTBREAK ") == "Break-up"
    assert mood.resolve_mood_name("chill") == "Pleasant"
    assert mood.resolve_mood_name("mass") == "Mass"
    assert mood.resolve_mood_name("bhakti") == "Devotional"
    assert mood.resolve_mood_name("rock") is None


def test_named_mood_similarity_puts_a_breakup_song_closer_to_sad_than_to_party():
    n = len(emotion.MOOD_NAMES)

    def peak(**kw):
        s = [0.0] * n
        for name, v in kw.items():
            s[emotion.MOOD_NAMES.index(name.replace("_", "-"))] = v
        return mood.Mood(0.0, 0.0, tuple(s))

    breakup, sad, party = peak(Break_up=2.5, Sad=1.5), peak(Sad=2.5, Break_up=1.0), peak(Party=2.5, Happy=1.5)
    assert mood.match(breakup, sad) > mood.match(breakup, party) + 0.2
    assert mood.match(breakup, breakup) == 1.0


def test_tags_name_only_clear_strengths():
    row = np.zeros(len(emotion.MOOD_NAMES))
    row[emotion.MOOD_NAMES.index("Love")], row[emotion.MOOD_NAMES.index("Pleasant")], row[emotion.MOOD_NAMES.index("Sad")] = 2.0, 1.0, 0.3
    assert emotion.tags(row) == ["Love", "Pleasant"]  # Sad at 0.3 is not clear enough
    assert emotion.tags(np.zeros(len(emotion.MOOD_NAMES))) == []


def test_the_shipped_model_loads_and_predicts_plausible_ratings():
    """The model file is part of the repo: it must load, have the 28 emotions and give finite numbers for any fingerprint."""
    dims = emotion.dimensions()
    assert len(dims) == 28 and "sad/depressing" in dims and "romantic/loving" in dims
    fp = np.random.default_rng(0).normal(size=(3, 512))
    fp /= np.linalg.norm(fp, axis=1, keepdims=True)
    r = emotion.ratings(fp)
    assert r.shape == (3, 28) and np.isfinite(r).all()
    assert emotion.ratings(fp[0]).shape == (28,)
    # every mood recipe names emotions the model really has
    for name, recipe in emotion.MOODS.items():
        assert set(recipe) <= set(dims), name


def test_scored_songs_blend_the_learned_ratings_with_the_light_signals():
    dims = emotion.dimensions()
    rng = np.random.default_rng(1)
    ids = [f"t{i}" for i in range(8)]
    raw = {i: (rng.normal(size=34).tolist(), 80 + 5 * k, 0.03) for k, i in enumerate(ids)}
    ratings = {i: {d: float(rng.normal()) for d in dims} for i in ids}
    ratings["t0"]["sad/depressing"] = 9.0  # a clear outlier on the "sad" rating
    plain, blended = mood.compute_moods(raw), mood.compute_moods(raw, ratings)
    assert blended["t0"].scores != plain["t0"].scores  # ratings change the answer
    assert mood.score_for(blended["t0"], "Sad") > mood.score_for(plain["t0"], "Sad")
    # fewer than 5 scored songs: nothing to compare against, so the light signals stand alone
    assert mood.compute_moods(raw, {"t0": ratings["t0"]})["t0"].scores == plain["t0"].scores


def test_payload_is_what_gets_stored_on_a_track():
    import json

    fp = np.random.default_rng(3).normal(size=512)
    vector, moods = emotion.payload(fp)
    assert len(vector) == 512 and abs(float(np.linalg.norm(vector)) - 1.0) < 1e-3  # stored as a unit fingerprint
    assert set(moods["ratings"]) == set(emotion.dimensions()) and moods["model"] == emotion.MODEL_VERSION
    again = json.loads(json.dumps(moods))  # must survive the JSON column
    assert again["ratings"]["sad/depressing"] == moods["ratings"]["sad/depressing"]


def test_the_learned_share_can_be_turned_down_to_nothing_by_an_environment_variable():
    import importlib
    import os
    from unittest import mock

    with mock.patch.dict(os.environ, {"MOOD_LEARNED_WEIGHT": "0"}):
        reloaded = importlib.reload(emotion)
        assert reloaded.LEARNED_WEIGHT == 0.0
    with mock.patch.dict(os.environ, {"MOOD_LEARNED_WEIGHT": "7"}):
        assert importlib.reload(emotion).LEARNED_WEIGHT == 1.0  # clamped to a sane range
    importlib.reload(emotion)  # back to the default for the other tests
    assert emotion.LEARNED_WEIGHT == 0.5
