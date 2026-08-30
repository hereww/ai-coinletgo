"""Add Portfolio-v1 decision, allocation, and execution audit records.

Revision ID: 20260827_0003
Revises: 20260811_0002
Create Date: 2026-08-27
"""

import sqlalchemy as sa
from alembic import op

revision = "20260827_0003"
down_revision = "20260811_0002"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if not _has_table("portfolio_decisions"):
        op.create_table(
            "portfolio_decisions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("market_regime", sa.String(length=30), nullable=False),
            sa.Column("risk_budget_fraction", sa.Numeric(precision=20, scale=10), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("prompt_version", sa.String(length=80), nullable=False),
            sa.Column("model_name", sa.String(length=120), nullable=False),
            sa.Column("input_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_portfolio_decision_created_at", "portfolio_decisions", ["created_at"])
        op.create_index("ix_portfolio_decisions_input_hash", "portfolio_decisions", ["input_hash"])

    if not _has_table("portfolio_allocations"):
        op.create_table(
            "portfolio_allocations",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("decision_id", sa.String(length=36), nullable=False),
            sa.Column("symbol", sa.String(length=30), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_portfolio_allocation_decision",
            "portfolio_allocations",
            ["decision_id"],
        )
        op.create_index("ix_portfolio_allocation_symbol", "portfolio_allocations", ["symbol"])

    if not _has_table("portfolio_executions"):
        op.create_table(
            "portfolio_executions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("decision_id", sa.String(length=36), nullable=False),
            sa.Column("allocation_id", sa.String(length=36), nullable=True),
            sa.Column("action", sa.String(length=40), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("order_ids", sa.JSON(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_portfolio_execution_decision", "portfolio_executions", ["decision_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for name in ("portfolio_executions", "portfolio_allocations", "portfolio_decisions"):
        if name in tables:
            op.drop_table(name)
