"""Keep when a person's account was deleted.

Revision ID: b81f4d2c6e90
Revises: a745ed039065

A deleted account keeps its row, with its identity, email and rights cleared: its login stays
reserved for ever, and its id is never given to someone new, since a session names a person by
id. Nobody here was deleted yet, so the column starts empty.
"""

import sqlalchemy as sa
from alembic import op

revision = "b81f4d2c6e90"
down_revision = "a745ed039065"


def upgrade() -> None:
    """Add when each person's account was deleted, empty for everyone."""
    # A database made by `create_all` and taken as the baseline may have the column already.
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("people")}
    if "deleted_at" not in columns:
        with op.batch_alter_table("people") as batch:
            batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
