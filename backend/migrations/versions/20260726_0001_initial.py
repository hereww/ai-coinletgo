"""Initial trading system schema.

Revision ID: 20260726_0001
Revises:
Create Date: 2026-07-26
"""

from collections.abc import Callable

import sqlalchemy as sa
from alembic import op

revision = "20260726_0001"
down_revision = None
branch_labels = None
depends_on = None


def _create_if_missing(
    existing: set[str], table_name: str, create: Callable[[], None]
) -> None:
    if table_name not in existing:
        create()


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())

    _create_if_missing(
        existing,
        "system_state",
        lambda: op.create_table(
            "system_state",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("mode", sa.String(length=40), nullable=False),
            sa.Column("entries_enabled", sa.Boolean(), nullable=False),
            sa.Column("halt_reason", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        ),
    )

    def create_audit_events() -> None:
        op.create_table(
            "audit_events",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("actor", sa.String(length=120), nullable=False),
            sa.Column("action", sa.String(length=120), nullable=False),
            sa.Column("resource", sa.String(length=120), nullable=False),
            sa.Column("outcome", sa.String(length=40), nullable=False),
            sa.Column("detail", sa.JSON(), nullable=False),
            sa.Column("ip_address", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_audit_created_at", "audit_events", ["created_at"])

    _create_if_missing(existing, "audit_events", create_audit_events)

    def create_signals() -> None:
        op.create_table(
            "signals",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("symbol", sa.String(length=30), nullable=False),
            sa.Column("action", sa.String(length=40), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("prompt_version", sa.String(length=80), nullable=False),
            sa.Column("model_name", sa.String(length=120), nullable=False),
            sa.Column("input_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_signal_created_at", "signals", ["created_at"])
        op.create_index("ix_signals_input_hash", "signals", ["input_hash"])
        op.create_index("ix_signals_symbol", "signals", ["symbol"])

    _create_if_missing(existing, "signals", create_signals)

    def create_risk_decisions() -> None:
        op.create_table(
            "risk_decisions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("signal_id", sa.String(length=36), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("reasons", sa.JSON(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_risk_decisions_signal_id", "risk_decisions", ["signal_id"]
        )

    _create_if_missing(existing, "risk_decisions", create_risk_decisions)

    def create_orders() -> None:
        op.create_table(
            "orders",
            sa.Column("client_order_id", sa.String(length=64), nullable=False),
            sa.Column("exchange_order_id", sa.String(length=80), nullable=True),
            sa.Column("symbol", sa.String(length=30), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("client_order_id"),
        )
        op.create_index(
            "ix_orders_exchange_order_id", "orders", ["exchange_order_id"]
        )
        op.create_index("ix_orders_symbol", "orders", ["symbol"])

    _create_if_missing(existing, "orders", create_orders)

    def create_income_ledger() -> None:
        op.create_table(
            "income_ledger",
            sa.Column("id", sa.String(length=100), nullable=False),
            sa.Column("symbol", sa.String(length=30), nullable=False),
            sa.Column("income_type", sa.String(length=40), nullable=False),
            sa.Column("income", sa.Numeric(precision=30, scale=10), nullable=False),
            sa.Column("asset", sa.String(length=20), nullable=False),
            sa.Column("trade_id", sa.String(length=80), nullable=True),
            sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_income_event_time", "income_ledger", ["event_time"])
        op.create_index(
            "ix_income_symbol_type", "income_ledger", ["symbol", "income_type"]
        )

    _create_if_missing(existing, "income_ledger", create_income_ledger)

    def create_positions() -> None:
        op.create_table(
            "positions",
            sa.Column("id", sa.String(length=80), nullable=False),
            sa.Column("symbol", sa.String(length=30), nullable=False),
            sa.Column("side", sa.String(length=20), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_positions_symbol", "positions", ["symbol"])

    _create_if_missing(existing, "positions", create_positions)

    def create_market_features() -> None:
        op.create_table(
            "market_features",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("symbol", sa.String(length=30), nullable=False),
            sa.Column("interval", sa.String(length=10), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_market_symbol_timestamp",
            "market_features",
            ["symbol", "timestamp"],
        )

    _create_if_missing(existing, "market_features", create_market_features)

    _create_if_missing(
        existing,
        "replay_runs",
        lambda: op.create_table(
            "replay_runs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("parameters", sa.JSON(), nullable=False),
            sa.Column("metrics", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        ),
    )

    _create_if_missing(
        existing,
        "equity_checkpoints",
        lambda: op.create_table(
            "equity_checkpoints",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("trading_day", sa.Date(), nullable=False),
            sa.Column(
                "day_start_equity", sa.Numeric(precision=30, scale=10), nullable=False
            ),
            sa.Column(
                "high_water_mark", sa.Numeric(precision=30, scale=10), nullable=False
            ),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        ),
    )

    def create_equity_history() -> None:
        op.create_table(
            "equity_history",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("equity", sa.Numeric(precision=30, scale=10), nullable=False),
            sa.Column("drawdown", sa.Numeric(precision=20, scale=10), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_equity_history_created_at", "equity_history", ["created_at"]
        )

    _create_if_missing(existing, "equity_history", create_equity_history)

    _create_if_missing(
        existing,
        "runtime_config",
        lambda: op.create_table(
            "runtime_config",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("values", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        ),
    )

    _create_if_missing(
        existing,
        "model_replay_cache",
        lambda: op.create_table(
            "model_replay_cache",
            sa.Column("input_hash", sa.String(length=64), nullable=False),
            sa.Column("prompt_version", sa.String(length=80), nullable=False),
            sa.Column("model_name", sa.String(length=120), nullable=False),
            sa.Column("output", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("input_hash"),
        ),
    )


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table_name in (
        "model_replay_cache",
        "runtime_config",
        "equity_history",
        "equity_checkpoints",
        "replay_runs",
        "market_features",
        "positions",
        "income_ledger",
        "orders",
        "risk_decisions",
        "signals",
        "audit_events",
        "system_state",
    ):
        if table_name in existing:
            op.drop_table(table_name)
