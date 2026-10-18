from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All config comes from environment variables (12-factor); see .env.example."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: str = "sqlite+pysqlite:///:memory:"
    redis_url: str = "redis://localhost:6379/0"
    # Play events are written straight to Postgres by the catalogue so history is current the moment
    # a song starts (the Render free tier cannot run the always-on consumer; its daily tick left
    # "Recently played" up to a day behind). True restores the Redis-stream-only path, which the
    # docker-compose stack's events consumer reads.
    events_via_queue: bool = False

    jwt_secret: str = "dev-only-change-me-dev-only-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7

    cors_origins: str = "http://localhost:3000"

    # Object storage: Backblaze B2 (S3-compatible API). "auto" addressing picks virtual-hosted
    # style for a real DNS endpoint (what B2 expects) and falls back to path-style automatically
    # when the endpoint is an IP:port (the in-process moto server the test suite uses).
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "test"
    s3_secret_key: str = "test"
    s3_region: str = "us-east-1"
    s3_addressing_style: str = "auto"
    s3_bucket_audio: str = "listens-audio"
    s3_bucket_covers: str = "listens-covers"

    # Streaming: URLs are HMAC-signed and expire. Separate secret from the JWT one so a leak
    # of one doesn't compromise the other.
    stream_signing_secret: str = "dev-only-stream-secret-dev-only-stream-secret"
    stream_public_prefix: str = "/api/stream"  # where the gateway mounts the stream service
    cover_url_ttl_seconds: int = 24 * 3600
    max_upload_mb: int = 500
    # The HTTP-triggered transcode worker: "Start processing now" in the app asks it to drain the queue. Defaults to the
    # production worker on Render; docker-compose runs the always-on loop instead (no HTTP), so there it just does nothing.
    worker_url: str = "https://shadow-listens-worker.onrender.com"

    # Codec of the HLS renditions the worker produces. AAC plays everywhere (Safari/iOS need it);
    # "mp3" exists for environments whose browser build lacks AAC (e.g. open-source Chromium in CI).
    hls_audio_codec: str = "aac"

    # Recommendations (Phase 5): how often services/recommender retrains the ALS model.
    recommender_train_interval_hours: float = 24

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
