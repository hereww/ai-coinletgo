"""Add persisted factor research runs.

Revision ID: 20260905_0005
Revises: 20260827_0004
Create Date: 2026-09-05
"""

import sqlalchemy as sa
from alembic import op

revision = "20260905_0005"
down_revision = "20260827_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "factor_research_runs" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "factor_research_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_factor_research_created_at",
        "factor_research_runs",
        ["created_at"],
    )


def downgrade() -> None:
    if "factor_research_runs" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("factor_research_runs")
