"""ALS-based "made for you" picks and "similar tracks" (Phase 5).

See services/recommender for the nightly ALS training job that produces the UserVector/
TrackVector rows this module scores against - the maths (confidence-weighted implicit
factorization) is explained there, not duplicated here. Both entry points degrade gracefully
when there's no ALS vector yet (a brand-new install, or a track uploaded since the last
training run): made-for-you falls back to library.py's artist-based heuristic, and
similar-tracks falls back to tempo/energy proximity (services/worker/app/audio_features.py),
then same-artist/same-genre.
"""
import uuid

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import taste
from listens_common import mood, tamil_seed
from listens_common.models import (
    Album, Artist, ArtistMeta, Like, PlayEvent, PlaylistTrack, Track, TrackVector, UserVector,
)

FULL_LISTEN_FRACTION = 0.9
EARLY_SKIP_MS = 30_000
ALS_BLEND = 0.35  # ALS share when content signals exist too: on a small library it learns little

MAX_PER_ARTIST = 2  # a Discover Weekly dominated by one artist defeats the point of the section


def cap_per_artist(tracks: list[Track], max_per_artist: int = MAX_PER_ARTIST) -> list[Track]:
    """Keeps only the first `max_per_artist` tracks per artist, preserving order. Exposed (not
    underscore-private) because made-for-you applies it once more, after heuristic/new-release
    fill-in, to keep the cap from Discover Weekly's original run."""
    counts: dict[uuid.UUID, int] = {}
    out = []
    for t in tracks:
        if counts.get(t.artist_id, 0) >= max_per_artist:
            continue
        counts[t.artist_id] = counts.get(t.artist_id, 0) + 1
        out.append(t)
    return out


def track_moods(db: Session) -> dict[uuid.UUID, mood.Mood]:
    """Mood of every ready track that has an audio fingerprint or has been scored (one query; empty for a library too small to
    compare). Scored songs (scripts/score_moods.py) blend the learned emotion model with the lighter signals."""
    rows = db.execute(
        select(Track.id, Track.audio_vector, Track.tempo_bpm, Track.energy, Track.moods)
        .where(Track.status == "ready", (Track.audio_vector.is_not(None)) | (Track.moods.is_not(None)))
    ).all()
    return mood.compute_moods({r[0]: (r[1], r[2], r[3]) for r in rows}, ratings=_ratings(rows, 4))


def _ratings(rows, index: int) -> dict:
    """{track id: {emotion: predicted rating}} for the rows whose moods column holds ratings."""
    return {r[0]: r[index]["ratings"] for r in rows if isinstance(r[index], dict) and r[index].get("ratings")}


def load_library(db: Session, include: set[uuid.UUID] | None = None) -> taste.Library:
    """Every ready track (plus any `include`d ones, e.g. a seed not yet ready) as comparable
    items, with the open-data artist context attached. One query for tracks, one for artist
    context; lyrics are only sampled (first 300 chars) for language detection."""
    rows = db.execute(
        select(
            Track.id, Track.artist_id, Track.album_id, Track.genre, Track.tempo_bpm, Track.energy, Track.audio_vector,
            Track.title, Artist.name, Album.title, Album.release_date, func.substr(Track.lyrics, 1, 300), Track.moods,
        )
        .join(Artist, Artist.id == Track.artist_id)
        .outerjoin(Album, Album.id == Track.album_id)
        .where((Track.status == "ready") | (Track.id.in_(include or set())))
    ).all()
    standardised = taste.Library.standardise({r[0]: r[6] for r in rows if r[6]})
    moods = mood.compute_moods({r[0]: (r[6], r[4], r[5]) for r in rows if r[6]}, ratings=_ratings(rows, 12))
    seeds = {r[1]: tamil_seed.lookup(r[8]) for r in rows}  # artist_id -> built-in Tamil entry, if any
    items = []
    for r in rows:
        seed = seeds[r[1]]
        items.append(taste.Item(
            id=r[0], artist_id=r[1], album_key=(r[9] or "").casefold().strip() or None, genre=r[3], tempo=r[4], energy=r[5],
            # the album's own date wins; otherwise the artist's peak years stand in for it
            year=r[10].year if r[10] else (seed.year if seed else None),
            language=taste.detect_language(r[7], r[8], r[9], r[11]) or ("ta" if seed and seed.tamil_core else None),
            vec=standardised.get(r[0]), mood=moods.get(r[0]),
        ))
    artists: dict[uuid.UUID, taste.ArtistInfo] = {
        artist_id: taste.ArtistInfo(circle=seed.circle) for artist_id, seed in seeds.items() if seed
    }
    for m in db.scalars(select(ArtistMeta)):
        related = [s for s in m.similar if not s.get("self")]
        top = max((s["score"] for s in related), default=0)
        artists[m.artist_id] = taste.ArtistInfo(
            mbid=m.mbid, tags=dict(m.tags or {}),
            # a joint credit stores each named person's id as a {"self": true} entry
            extra_mbids=tuple(s["mbid"] for s in m.similar if s.get("self")),
            neighbours={s["mbid"]: s["score"] / top for s in related if top and s.get("mbid")},
            circle=artists[m.artist_id].circle if m.artist_id in artists else None,
        )
    return taste.Library(items, artists)


def listener_weights(db: Session, user_id: uuid.UUID) -> dict[uuid.UUID, float]:
    """The listener's taste as signed track weights: a like or playlist add +3, a finished listen
    +1 (+2 more for each repeat), an early skip -2. Same scheme the ALS trainer uses, so both
    models read the listener the same way (services/recommender/app/matrix.py)."""
    w: dict[uuid.UUID, float] = {}

    def add(track_id, amount):
        w[track_id] = w.get(track_id, 0.0) + amount

    for (t,) in db.execute(select(Like.track_id).where(Like.user_id == user_id)):
        add(t, 3)
    for (t,) in db.execute(select(PlaylistTrack.track_id).where(PlaylistTrack.added_by == user_id)):
        add(t, 3)
    for t, n in db.execute(
        select(PlayEvent.track_id, func.count()).join(Track, Track.id == PlayEvent.track_id)
        .where(PlayEvent.user_id == user_id, PlayEvent.event_type == "play_end", Track.duration_ms.is_not(None),
               PlayEvent.ms_played >= Track.duration_ms * FULL_LISTEN_FRACTION)
        .group_by(PlayEvent.track_id)
    ):
        add(t, n + 2 * max(0, n - 1))
    for t, n in db.execute(
        select(PlayEvent.track_id, func.count())
        .where(PlayEvent.user_id == user_id, PlayEvent.event_type == "skip", PlayEvent.position_ms < EARLY_SKIP_MS)
        .group_by(PlayEvent.track_id)
    ):
        add(t, -2 * n)
    return w


def _in_order(db: Session, ids: list[uuid.UUID]) -> list[Track]:
    by_id = {t.id: t for t in db.scalars(select(Track).where(Track.id.in_(ids))).unique()}
    return [by_id[i] for i in ids if i in by_id]


def hybrid_made_for_you(db: Session, user_id: uuid.UUID, limit: int) -> list[Track] | None:
    """Content + taste + (when trained) ALS, diversified with MMR. None when there is no content
    signal at all, so the caller keeps the older ALS/heuristic behaviour unchanged."""
    weights = listener_weights(db, user_id)
    if not any(v > 0 for v in weights.values()):
        return None
    lib = load_library(db)
    played = set(db.scalars(select(PlayEvent.track_id).where(PlayEvent.user_id == user_id)))
    candidates = [i for i in lib.items.values() if i.id not in played]
    content = {c.id: s for c in candidates if (s := taste.taste_score(lib, c, weights)) is not None}
    if not content or max(content.values()) <= 0:
        return None

    scores = taste.rank_normalise(content)
    user_vector = db.get(UserVector, user_id)
    if user_vector is not None:
        u = np.array(user_vector.vector)
        als = {
            tv.track_id: float(u @ np.array(tv.vector))
            for tv in db.scalars(select(TrackVector).where(TrackVector.track_id.in_(list(content))))
        }
        if als:
            als_n = taste.rank_normalise(als)
            scores = {t: (1 - ALS_BLEND) * s + ALS_BLEND * als_n.get(t, 0.5) for t, s in scores.items()}
    return _in_order(db, taste.mmr(lib, scores, limit))


def als_made_for_you(db: Session, user_id: uuid.UUID, limit: int) -> list[Track] | None:
    """Score every not-yet-played, ready track by dot product with the user's ALS vector
    (see services/recommender/app/train.py for why that dot product is the right score).
    Returns None - not [] - when there's no trained vector for this user yet, so the caller
    knows to fall back to the heuristic rather than showing a thin/empty section."""
    user_vector = db.get(UserVector, user_id)
    if user_vector is None:
        return None

    # Plain .all(), not .unique(): Track's joined relationships (artist, album) are many-to-one,
    # so this never fans out duplicate rows - and .unique() can't hash a row containing the
    # JSON vector column anyway.
    played = select(PlayEvent.track_id).where(PlayEvent.user_id == user_id)
    candidates = db.execute(
        select(Track, TrackVector.vector)
        .join(TrackVector, TrackVector.track_id == Track.id)
        .where(Track.status == "ready", Track.id.not_in(played))
    ).all()
    if not candidates:
        return []

    u = np.array(user_vector.vector)
    scored = sorted(candidates, key=lambda row: float(u @ np.array(row[1])), reverse=True)
    ranked_tracks = [row[0] for row in scored]
    return cap_per_artist(ranked_tracks)[:limit]


def _zscore(values: list[float]) -> np.ndarray:
    arr = np.array(values, dtype=float)
    std = arr.std()
    return (arr - arr.mean()) / std if std > 1e-9 else np.zeros_like(arr)


def _tempo_energy_neighbors(db: Session, track: Track, limit: int) -> list[Track]:
    candidates = list(db.scalars(
        select(Track).where(
            Track.status == "ready", Track.id != track.id,
            Track.tempo_bpm.is_not(None), Track.energy.is_not(None),
        )
    ).unique())
    if not candidates:
        return []
    # z-score both axes together with the query track, so "fast and loud" vs "slow and quiet"
    # is judged on a comparable scale rather than raw BPM dwarfing a 0-1 energy value.
    tempos = _zscore([track.tempo_bpm, *(c.tempo_bpm for c in candidates)])
    energies = _zscore([track.energy, *(c.energy for c in candidates)])
    target = np.array([tempos[0], energies[0]])
    ranked = sorted(
        range(len(candidates)),
        key=lambda i: float(np.linalg.norm(target - np.array([tempos[i + 1], energies[i + 1]]))),
    )
    return [candidates[i] for i in ranked[:limit]]


def _same_artist_then_genre(db: Session, track: Track, limit: int) -> list[Track]:
    same_artist = list(db.scalars(
        select(Track).where(Track.status == "ready", Track.id != track.id, Track.artist_id == track.artist_id)
        .order_by(Track.created_at.desc()).limit(limit)
    ).unique())
    if len(same_artist) >= limit or not track.genre:
        return same_artist[:limit]
    have = {t.id for t in same_artist} | {track.id}
    same_genre = list(db.scalars(
        select(Track).where(Track.status == "ready", Track.genre == track.genre, Track.id.not_in(have))
        .order_by(Track.created_at.desc()).limit(limit - len(same_artist))
    ).unique())
    return [*same_artist, *same_genre]


def hybrid_similar(db: Session, track: Track, limit: int, user_id: uuid.UUID | None) -> list[Track]:
    """Tracks like `track`: content similarity (audio, artist graph, film, era, language, tempo),
    blended with ALS co-listening when both tracks have vectors, nudged toward this listener's
    taste, then diversified. Empty when nothing is comparable (the caller falls back)."""
    lib = load_library(db, include={track.id})
    seed = lib.items.get(track.id)
    if seed is None:
        return []
    others = [i for i in lib.items.values() if i.id != track.id]
    content = {o.id: lib.similarity(seed, o) for o in others}
    if not content or max(content.values()) <= 0:
        return []

    scores = taste.rank_normalise(content)
    own = db.get(TrackVector, track.id)
    if own is not None:
        vecs = {tv.track_id: tv.vector for tv in db.scalars(select(TrackVector).where(TrackVector.track_id.in_(list(content))))}
        if vecs:
            ids = list(vecs)
            cos = cosine_similarity([own.vector], [vecs[i] for i in ids])[0]
            als_n = taste.rank_normalise(dict(zip(ids, map(float, cos))))
            als_weight = 0.25 if seed.mood is not None else 0.6  # co-listening knows nothing of mood: let mood lead
            scores = {t: als_weight * als_n.get(t, 0.5) + (1 - als_weight) * s for t, s in scores.items()}
    # whatever blended in (co-listening above all), a song of the wrong mood stays down: the seed's mood decides
    scores = {t: s * taste.mood_gate_for(seed, lib.items[t]) for t, s in scores.items()}
    if user_id is not None:
        weights = listener_weights(db, user_id)
        mine = {o.id: v for o in others if (v := taste.taste_score(lib, o, weights)) is not None}
        if mine:
            mine_n = taste.rank_normalise(mine)
            scores = {t: 0.85 * s + 0.15 * mine_n.get(t, 0.0) for t, s in scores.items()}
    # diversity re-ranking rewards songs unlike the ones already picked, which for a mood-led list would pull in the wrong
    # mood; with a mood estimate it only breaks near-ties
    lam = 0.95 if seed.mood is not None else 0.8
    return _in_order(db, taste.mmr(lib, scores, limit, lam=lam, per_artist=3))


def similar_tracks(db: Session, track: Track, limit: int, user_id: uuid.UUID | None = None) -> list[Track]:
    picks = hybrid_similar(db, track, limit, user_id)
    if len(picks) >= limit:
        return picks
    have = {t.id for t in picks} | {track.id}
    return [*picks, *[t for t in _legacy_similar(db, track, limit) if t.id not in have]][:limit]


def _legacy_similar(db: Session, track: Track, limit: int) -> list[Track]:
    own_vector = db.get(TrackVector, track.id)
    if own_vector is not None:
        rows = db.execute(
            select(Track, TrackVector.vector).join(TrackVector, TrackVector.track_id == Track.id)
            .where(Track.status == "ready", Track.id != track.id)
        ).all()
        if rows:
            vectors = np.array([row[1] for row in rows])
            sims = cosine_similarity([own_vector.vector], vectors)[0]
            order = np.argsort(sims)[::-1][:limit]
            return [rows[i][0] for i in order]

    picks = _tempo_energy_neighbors(db, track, limit)
    if len(picks) >= limit:
        return picks
    have = {t.id for t in picks} | {track.id}
    fallback = [t for t in _same_artist_then_genre(db, track, limit - len(picks)) if t.id not in have]
    return [*picks, *fallback]
