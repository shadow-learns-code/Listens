"""Relational data model, shared by every service and by Alembic.

Design notes
- UUID primary keys for public entities (no enumerable IDs); BIGINT identity for
  play_events because it is an append-only, high-volume log.
- Status/type columns are VARCHAR + CHECK rather than native PG enums: adding a value
  later is a one-line migration instead of ALTER TYPE gymnastics.
- `follows` is polymorphic (a user follows a user OR an artist), so target_id has no
  FK; the (target_type, target_id) pair is validated in the application layer.
- Only portable column types are used so the same models run on SQLite in unit tests.
"""
import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# BIGINT on Postgres, INTEGER on SQLite (SQLite only autoincrements INTEGER PKs).
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    bio: Mapped[str | None] = mapped_column(Text)
    avatar_key: Mapped[str | None] = mapped_column(String(255))  # object key in MinIO
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Artist(Base):
    __tablename__ = "artists"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    bio: Mapped[str | None] = mapped_column(Text)
    image_key: Mapped[str | None] = mapped_column(String(255))
    # Set when a user-uploader owns this artist profile (uploads must be by the rights holder).
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (Index("ix_artists_name", "name"),)


class Album(Base):
    __tablename__ = "albums"

    id: Mapped[uuid.UUID] = _uuid_pk()
    artist_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("artists.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    cover_key: Mapped[str | None] = mapped_column(String(255))
    release_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = _created_at()

    artist: Mapped[Artist] = relationship(lazy="joined")

    __table_args__ = (Index("ix_albums_artist_id", "artist_id"),)


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    artist_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("artists.id", ondelete="CASCADE"), nullable=False
    )
    album_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("albums.id", ondelete="SET NULL")
    )
    uploader_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    genre: Mapped[str | None] = mapped_column(String(60))
    license: Mapped[str | None] = mapped_column(String(100))  # e.g. "CC BY 4.0"
    # Upload pipeline state machine (Phase 2): uploaded -> processing -> ready | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="uploaded", server_default="uploaded")
    source_key: Mapped[str | None] = mapped_column(String(255))  # original upload in MinIO
    hls_prefix: Mapped[str | None] = mapped_column(String(255))  # folder holding HLS renditions
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    source_bitrate_kbps: Mapped[int | None] = mapped_column(Integer)
    cover_key: Mapped[str | None] = mapped_column(String(255))  # object key in the covers bucket
    content_sha256: Mapped[str | None] = mapped_column(String(64))  # dedupe re-uploads per user
    error: Mapped[str | None] = mapped_column(Text)  # why transcoding failed
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lyrics: Mapped[str | None] = mapped_column(Text)  # plain unsynced lyrics (ID3 USLT / Vorbis LYRICS)
    lyrics_synced: Mapped[str | None] = mapped_column(Text)  # LRC text "[mm:ss.xx]line", from ID3 SYLT if present
    # Cold-start audio features (librosa, extracted by the worker). Used to suggest "similar
    # tracks" for a track with no listening history yet, i.e. no ALS vector (see Phase 5).
    tempo_bpm: Mapped[float | None] = mapped_column(Float)
    energy: Mapped[float | None] = mapped_column(Float)  # mean RMS amplitude, roughly "loudness/intensity"
    # Timbre/harmony fingerprint (MFCC + chroma + spectral contrast means, ~34 floats) from the same
    # librosa pass. Raw values; the recommender standardises per dimension across the library.
    audio_vector: Mapped[list[float] | None] = mapped_column(JSON)
    # Mood (listens_common.emotion): the CLAP fingerprint (512 numbers, deferred: only the scoring script reads it) and
    # {"ratings": {emotion: predicted rating}} from it. NULL until scripts/score_moods.py has run for the song.
    mood_vector: Mapped[list[float] | None] = mapped_column(JSON, deferred=True)
    moods: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _created_at()

    # `artist` is the PRIMARY (first credited) artist; `artists` is everyone credited, in credit order, read through
    # the track_artists link rows (write those via listens_common.artists.set_track_artists).
    artist: Mapped[Artist] = relationship(lazy="joined")
    artists: Mapped[list[Artist]] = relationship(
        secondary="track_artists", order_by="TrackArtist.position", lazy="selectin", viewonly=True
    )
    album: Mapped["Album | None"] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint("status IN ('uploaded','processing','ready','failed')", name="ck_tracks_status"),
        Index("ix_tracks_artist_id", "artist_id"),
        Index("ix_tracks_album_id", "album_id"),
        Index("ix_tracks_status_created", "status", "created_at"),
        Index("uq_tracks_uploader_sha", "uploader_id", "content_sha256", unique=True),
    )


class TrackArtist(Base):
    """Who is credited on a track. One row per person, `position` 0 = the primary (first-listed) artist."""

    __tablename__ = "track_artists"

    track_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True)
    artist_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("artists.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (Index("ix_track_artists_artist_id", "artist_id"),)


class Playlist(Base):
    __tablename__ = "playlists"

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    cover_key: Mapped[str | None] = mapped_column(String(255))
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    is_collaborative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_playlists_owner_id", "owner_id"),)


class PlaylistTrack(Base):
    """A surrogate key (not (playlist, track)) so the same track can appear twice."""

    __tablename__ = "playlist_tracks"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    playlist_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("playlists.id", ondelete="CASCADE"), nullable=False
    )
    track_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    track: Mapped[Track] = relationship(lazy="joined")

    # Deliberately not UNIQUE(playlist_id, position): reordering would need deferred
    # constraints. Ordering is (position, id) and positions are rewritten on reorder.
    __table_args__ = (Index("ix_playlist_tracks_playlist_pos", "playlist_id", "position"),)


class Like(Base):
    __tablename__ = "likes"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    track_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (Index("ix_likes_track_id", "track_id"),)


class Follow(Base):
    __tablename__ = "follows"

    follower_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    target_type: Mapped[str] = mapped_column(String(8), primary_key=True)
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("target_type IN ('user','artist')", name="ck_follows_target_type"),
        Index("ix_follows_target", "target_type", "target_id"),
    )


class PlayEvent(Base):
    """Append-only listening log (written by the Phase 4 stream consumer)."""

    __tablename__ = "play_events"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    # No FK on user/track: high-volume log, and we want to keep history if either is deleted.
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    track_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    ms_played: Mapped[int | None] = mapped_column(Integer)
    position_ms: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(String(40))  # skip reason, e.g. "next_button"
    session_id: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('play_start','play_end','skip','like','add_to_playlist')",
            name="ck_play_events_type",
        ),
        Index("ix_play_events_user_time", "user_id", "occurred_at"),
        Index("ix_play_events_track_time", "track_id", "occurred_at"),
    )


class UserVector(Base):
    """A user's latent-factor vector from the nightly ALS training run (see services/recommender).
    One row per user; retrained in place. Vectors are stored as JSON (a plain list[float]), not a
    Postgres-specific ARRAY or a vector-DB extension, so the same schema runs on SQLite in tests
    and nothing beyond a normal Postgres image is required to deploy this."""

    __tablename__ = "user_vectors"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    vector: Mapped[list[float]] = mapped_column(JSON, nullable=False)
    trained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TrackVector(Base):
    """A track's latent-factor vector from the same ALS run. Dot product with a UserVector gives
    a recommendation score; cosine similarity between two TrackVectors gives "similar tracks"."""

    __tablename__ = "track_vectors"

    track_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True)
    vector: Mapped[list[float]] = mapped_column(JSON, nullable=False)
    trained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ArtistMeta(Base):
    """Open-data context for an artist, fetched by scripts/enrich_library.py: the MusicBrainz id and
    tags, and ListenBrainz's "similar artists" (collaborative filtering over real listening sessions).
    `similar` is a list of {"mbid", "name", "score"}. One row per artist; absent until enriched."""

    __tablename__ = "artist_meta"

    artist_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("artists.id", ondelete="CASCADE"), primary_key=True)
    mbid: Mapped[str | None] = mapped_column(String(36))
    tags: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    similar: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
