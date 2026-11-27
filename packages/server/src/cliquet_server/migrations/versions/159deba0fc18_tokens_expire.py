"""Let a token expire.

Revision ID: 159deba0fc18
Revises: 635a7476112b

A new token works for a number of days, 90 by default, or for good when its person chooses.
Every token made before was made to work for good: it keeps no expiry, and the Tokens page
says it never expires.
"""

import sqlalchemy as sa
from alembic import op

revision = "159deba0fc18"
down_revision = "635a7476112b"


def upgrade() -> None:
    """Add when each token expires, never for every token there is."""
    # A database made by `create_all` and taken as the baseline may have the column already.
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("tokens")}
    if "expires_at" in columns:
        return
    with op.batch_alter_table("tokens") as batch:
        batch.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Drop when each token expires: every token works for good again."""
    with op.batch_alter_table("tokens") as batch:
        batch.drop_column("expires_at")
