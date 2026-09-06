"""Persist factor policy versions and independent online windows.

Revision ID: 20260906_0007
Revises: 20260905_0006
Create Date: 2026-09-06
"""

import sqlalchemy as sa
from alembic import op

revision = "20260906_0007"
down_revision = "20260905_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "factor_policies" not in tables:
        op.create_table(
            "factor_policies",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("research_run_id", sa.String(length=36), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("frozen_snapshot", sa.JSON(), nullable=False),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("research_run_id", name="uq_factor_policy_research_run"),
        )
        op.create_index("ix_factor_policy_status", "factor_policies", ["status"])
    if "factor_policy_windows" not in tables:
        op.create_table(
            "factor_policy_windows",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("research_run_id", sa.String(length=36), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("result", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("matured_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "research_run_id",
                "window_start",
                name="uq_factor_policy_window_research_start",
            ),
        )
        op.create_index(
            "ix_factor_policy_windows_research_run_id",
            "factor_policy_windows",
            ["research_run_id"],
        )
        op.create_index(
            "ix_factor_policy_window_status_end",
            "factor_policy_windows",
            ["status", "window_end"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "factor_policy_windows" in tables:
        op.drop_table("factor_policy_windows")
    if "factor_policies" in tables:
        op.drop_table("factor_policies")
