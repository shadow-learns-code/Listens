"""track moods: a per-song audio fingerprint and the emotions predicted from it

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-18

Additive only (two nullable JSON columns, no data change). `mood_vector` is the song's fingerprint from a pretrained audio
model (LAION CLAP, 512 numbers); `moods` is {"ratings": {emotion: predicted rating}} for the 28 emotions in
listens_common.emotion. Both are filled by scripts/score_moods.py on a computer that has the model; until then they are
NULL and the song's mood comes from the lighter signals the worker already stores (key, tempo, loudness).

Deploy order matters: auth applies migrations on startup, so push this migration (with the model columns) before anything
that reads them is live, and never apply it to production ahead of the push.
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tracks") as batch:
        batch.add_column(sa.Column("mood_vector", sa.JSON()))
        batch.add_column(sa.Column("moods", sa.JSON()))


def downgrade() -> None:
    with op.batch_alter_table("tracks") as batch:
        batch.drop_column("moods")
        batch.drop_column("mood_vector")
