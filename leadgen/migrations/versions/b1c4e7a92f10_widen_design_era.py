"""widen website_audits.design_era

The column held a human-readable sentence in 40 characters. The parser builds
that sentence by appending clauses, and "pre-2012 (not responsive), copyright
2011" is 41 — one character over. Postgres rejected the INSERT, which aborted
the whole audit batch, which failed the run before any report was written.

Revision ID: b1c4e7a92f10
Revises: 8936d3228de3
Created: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "b1c4e7a92f10"
down_revision: str | None = "8936d3228de3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "website_audits",
        "design_era",
        existing_type=sa.String(length=40),
        type_=sa.String(length=120),
        existing_nullable=True,
    )


def downgrade() -> None:
    # Values written since the upgrade can exceed 40 characters, so trim before
    # narrowing or Postgres refuses the ALTER.
    op.execute("UPDATE website_audits SET design_era = left(design_era, 40)")
    op.alter_column(
        "website_audits",
        "design_era",
        existing_type=sa.String(length=120),
        type_=sa.String(length=40),
        existing_nullable=True,
    )
