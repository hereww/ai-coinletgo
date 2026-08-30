"""Add optional Portfolio-v1 lineage columns to order audit records.

Revision ID: 20260827_0004
Revises: 20260827_0003
Create Date: 2026-08-27
"""

import sqlalchemy as sa
from alembic import op

revision = "20260827_0004"
down_revision = "20260827_0003"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if not _has_table("orders"):
        return
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("orders")}
    additions = (
        ("portfolio_decision_id", sa.String(length=36)),
        ("portfolio_allocation_id", sa.String(length=36)),
        ("action_sequence", sa.Integer()),
    )
    missing = [(name, column_type) for name, column_type in additions if name not in columns]
    if not missing:
        return
    with op.batch_alter_table("orders") as batch_op:
        for name, column_type in missing:
            batch_op.add_column(sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    if not _has_table("orders"):
        return
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("orders")}
    present = ["action_sequence", "portfolio_allocation_id", "portfolio_decision_id"]
    if not any(name in columns for name in present):
        return
    with op.batch_alter_table("orders") as batch_op:
        for name in present:
            if name in columns:
                batch_op.drop_column(name)
