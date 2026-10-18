"""Search over tracks, artists, albums and playlists.

Postgres: full-text search with the 'simple' config and prefix matching ("dai" finds
"Daidream"), ranked by ts_rank. The WHERE expressions are written to be *identical* to the
GIN expression indexes in migration 0002 so the planner can use them.
SQLite (unit tests only): plain LIKE fallback, same response shape.
"""
import re
import uuid

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from listens_common import emotion, mood as moodlib
from listens_common.models import Album, Artist, Playlist, Track, TrackArtist


def build_tsquery(q: str) -> str | None:
    """'daft pu' -> 'daft:* & pu:*'. Only \\w tokens survive, so no tsquery syntax injection."""
    tokens = re.findall(r"\w+", q.lower())[:8]
    return " & ".join(f"{t}:*" for t in tokens) or None


TRACK_FTS = "to_tsvector('simple', t.title || ' ' || coalesce(t.genre, ''))"
ARTIST_FTS = "to_tsvector('simple', a.name)"
ALBUM_FTS = "to_tsvector('simple', al.title)"
PLAYLIST_FTS = "to_tsvector('simple', p.name || ' ' || coalesce(p.description, ''))"

# Two branches (title/genre hit, artist-name hit) instead of one `OR`: an OR against a subquery
# stops the planner from using the GIN index. Each branch is index-friendly; GROUP BY merges
# duplicates keeping the best rank. Artist-name hits rank below direct title hits.
PG_TRACKS = f"""
SELECT id FROM (
  SELECT t.id, ts_rank({TRACK_FTS}, q) AS rank, t.created_at
    FROM tracks t, to_tsquery('simple', :q) q
   WHERE t.status = 'ready' AND {TRACK_FTS} @@ q
  UNION ALL
  SELECT t.id, 0.01 AS rank, t.created_at
    FROM tracks t JOIN artists a ON a.id = t.artist_id, to_tsquery('simple', :q) q
   WHERE t.status = 'ready' AND {ARTIST_FTS} @@ q
  UNION ALL
  SELECT t.id, 0.01 AS rank, t.created_at
    FROM tracks t JOIN track_artists ta ON ta.track_id = t.id JOIN artists a ON a.id = ta.artist_id, to_tsquery('simple', :q) q
   WHERE t.status = 'ready' AND {ARTIST_FTS} @@ q
) hits GROUP BY id ORDER BY max(rank) DESC, max(created_at) DESC LIMIT :n"""

PG_ARTISTS = f"""
SELECT a.id FROM artists a, to_tsquery('simple', :q) q
WHERE {ARTIST_FTS} @@ q ORDER BY ts_rank({ARTIST_FTS}, q) DESC, a.name LIMIT :n"""

PG_ALBUMS = f"""
SELECT al.id FROM albums al, to_tsquery('simple', :q) q
WHERE {ALBUM_FTS} @@ q ORDER BY ts_rank({ALBUM_FTS}, q) DESC, al.title LIMIT :n"""

PG_PLAYLISTS = f"""
SELECT p.id FROM playlists p, to_tsquery('simple', :q) q
WHERE {PLAYLIST_FTS} @@ q AND (p.is_public OR p.owner_id = :uid)
ORDER BY ts_rank({PLAYLIST_FTS}, q) DESC, p.name LIMIT :n"""


def _like(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return f"%{escaped.lower()}%"


def _load(db: Session, model, ids: list[uuid.UUID]) -> list:
    """Fetch rows preserving the ranked id order."""
    if not ids:
        return []
    rows = {r.id: r for r in db.scalars(select(model).where(model.id.in_(ids))).unique()}
    return [rows[i] for i in ids if i in rows]


def search(db: Session, q: str, user_id: uuid.UUID, limit: int = 10) -> dict[str, list]:
    if db.get_bind().dialect.name == "postgresql":
        tsq = build_tsquery(q)
        if tsq is None:
            return {"tracks": [], "artists": [], "albums": [], "playlists": []}
        run = lambda sql, **kw: list(db.scalars(text(sql), {"q": tsq, "n": limit, **kw}))  # noqa: E731
        return {
            "tracks": _load(db, Track, run(PG_TRACKS)),
            "artists": _load(db, Artist, run(PG_ARTISTS)),
            "albums": _load(db, Album, run(PG_ALBUMS)),
            "playlists": _load(db, Playlist, run(PG_PLAYLISTS, uid=user_id)),
        }

    pat = _like(q)
    esc = "\\"
    credited_match = (
        select(TrackArtist.track_id).join(Artist, Artist.id == TrackArtist.artist_id)
        .where(func.lower(Artist.name).like(pat, escape=esc))
    )
    tracks = db.scalars(
        select(Track).join(Artist, Track.artist_id == Artist.id)
        .where(Track.status == "ready")
        .where(or_(func.lower(Track.title).like(pat, escape=esc), func.lower(Artist.name).like(pat, escape=esc),
                   Track.id.in_(credited_match),
                   func.lower(func.coalesce(Track.genre, "")).like(pat, escape=esc)))
        .order_by(Track.created_at.desc()).limit(limit)
    ).unique().all()
    artists = db.scalars(select(Artist).where(func.lower(Artist.name).like(pat, escape=esc)).limit(limit)).all()
    albums = db.scalars(select(Album).where(func.lower(Album.title).like(pat, escape=esc)).limit(limit)).unique().all()
    playlists = db.scalars(
        select(Playlist).where(func.lower(Playlist.name).like(pat, escape=esc))
        .where(or_(Playlist.is_public.is_(True), Playlist.owner_id == user_id)).limit(limit)
    ).all()
    return {"tracks": tracks, "artists": artists, "albums": albums, "playlists": playlists}


# ------------------------------------------------------------- filtered search

FILTERS = ("all", "artist", "album", "genre", "mood")
_EMPTY = {"tracks": [], "artists": [], "albums": [], "playlists": []}


def _ready_tracks(db: Session, *where, limit: int) -> list[Track]:
    return db.scalars(select(Track).where(Track.status == "ready", *where).order_by(Track.created_at.desc()).limit(limit)).unique().all()


def search_by(db: Session, q: str, by: str, limit: int = 10) -> dict[str, list]:
    """Search ONE field: artist (every song they are credited on), album (its songs), genre, or mood ("sad", "chill",
    "party"). Plain substring match, case-insensitive: these are short tag values, not prose, so full-text ranking adds
    nothing here. `by="all"` is the ordinary search."""
    pat, esc = _like(q), "\\"
    if by == "artist":
        artists = db.scalars(select(Artist).where(func.lower(Artist.name).like(pat, escape=esc)).order_by(Artist.name).limit(limit)).all()
        ids = [a.id for a in artists]
        credited = select(TrackArtist.track_id).where(TrackArtist.artist_id.in_(ids))
        tracks = _ready_tracks(db, or_(Track.artist_id.in_(ids), Track.id.in_(credited)), limit=limit * 3) if ids else []
        return {**_EMPTY, "tracks": tracks, "artists": artists}
    if by == "album":
        albums = db.scalars(select(Album).where(func.lower(Album.title).like(pat, escape=esc)).order_by(Album.title).limit(limit)).unique().all()
        ids = [a.id for a in albums]
        return {**_EMPTY, "tracks": _ready_tracks(db, Track.album_id.in_(ids), limit=limit * 3) if ids else [], "albums": albums}
    if by == "genre":
        return {**_EMPTY, "tracks": _ready_tracks(db, func.lower(func.coalesce(Track.genre, "")).like(pat, escape=esc), limit=limit * 3)}
    if by == "mood":
        name = moodlib.resolve_mood_name(q)  # Love, Break-up, Sad, Pleasant, Happy, Party, Mass, Vibe, Nostalgic, Devotional, Motivational
        if name is None:
            return dict(_EMPTY)
        from .recommendations import track_moods  # lazy: recommendations imports this package's taste module

        moods = {t: m for t, m in track_moods(db).items() if name in m.tags}
        ranked = sorted(moods, key=lambda t: -moodlib.score_for(moods[t], name))[: limit * 3]  # the clearest examples first
        by_id = {t.id: t for t in db.scalars(select(Track).where(Track.id.in_(ranked))).unique()}
        return {**_EMPTY, "tracks": [by_id[i] for i in ranked if i in by_id]}
    return search(db, q, uuid.UUID(int=0), limit)  # not reached through the API (it routes "all" itself)


def facets(db: Session) -> dict[str, list[dict]]:
    """What can be filtered on: the genres in the library and the moods with how many songs sit in each."""
    genres = db.execute(
        select(func.lower(Track.genre), func.min(Track.genre), func.count()).where(Track.status == "ready", Track.genre.is_not(None), Track.genre != "")
        .group_by(func.lower(Track.genre)).order_by(func.count().desc()).limit(30)
    ).all()
    from .recommendations import track_moods

    counts: dict[str, int] = {}
    for m in track_moods(db).values():
        for tag in m.tags:
            counts[tag] = counts.get(tag, 0) + 1
    return {
        "genres": [{"name": g[1], "count": g[2]} for g in genres],
        "moods": [{"id": name, "name": name, "query": name.lower(), "count": counts[name]}
                  for name in emotion.MOOD_NAMES if counts.get(name)],
    }
