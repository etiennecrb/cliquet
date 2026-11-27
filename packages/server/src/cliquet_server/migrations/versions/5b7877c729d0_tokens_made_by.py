"""Let a token name the token that made it.

Revision ID: 5b7877c729d0
Revises: ec8e858e1f10

An agent with a token for everywhere may make a token for a worker. Each such token names the
one that made it, so the Tokens page says so, and goes when that one is revoked. Every token
made before was made on the console or with a sign-in: it names none.
"""

import sqlalchemy as sa
from alembic import op

revision = "5b7877c729d0"
down_revision = "ec8e858e1f10"


def upgrade() -> None:
    """Add the token each token was made by, none for every token there is."""
    # A database made by `create_all` and taken as the baseline may have the column already.
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("tokens")}
    if "made_by" in columns:
        return
    with op.batch_alter_table("tokens") as batch:
        batch.add_column(sa.Column("made_by", sa.Integer(), nullable=True))
        # Named as the models name it.
        batch.create_foreign_key("tokens_made_by_fkey", "tokens", ["made_by"], ["id"])
