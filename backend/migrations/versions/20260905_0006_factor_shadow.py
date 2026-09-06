"""Persist point-in-time factor shadow rankings.

Revision ID: 20260905_0006
Revises: 20260905_0005
Create Date: 2026-09-05
"""

import sqlalchemy as sa
from alembic import op

revision = "20260905_0006"
down_revision = "20260905_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "factor_shadow_rankings" in set(inspector.get_table_names()):
        return
    op.create_table(
        "factor_shadow_rankings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("research_run_id", sa.String(length=36), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_factor_shadow_rankings_research_run_id",
        "factor_shadow_rankings",
        ["research_run_id"],
    )
    op.create_index(
        "ix_factor_shadow_timestamp",
        "factor_shadow_rankings",
        ["timestamp"],
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "factor_shadow_rankings" not in set(inspector.get_table_names()):
        return
    op.drop_table("factor_shadow_rankings")
