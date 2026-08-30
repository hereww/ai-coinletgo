"""Add the exchange environment to persisted system state.

Revision ID: 20260811_0002
Revises: 20260726_0001
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op

revision = "20260811_0002"
down_revision = "20260726_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("system_state")}
    if "environment" in columns:
        return
    with op.batch_alter_table("system_state") as batch_op:
        batch_op.add_column(
            sa.Column(
                "environment",
                sa.String(length=20),
                nullable=False,
                server_default="testnet",
            )
        )
    with op.batch_alter_table("system_state") as batch_op:
        batch_op.alter_column("environment", server_default=None)


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("system_state")}
    if "environment" not in columns:
        return
    with op.batch_alter_table("system_state") as batch_op:
        batch_op.drop_column("environment")
