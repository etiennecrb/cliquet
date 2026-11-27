"""Give each API token an id the apps it reaches see.

Revision ID: ec8e858e1f10
Revises: b81f4d2c6e90

An app's code tells one agent of a person from another by the id of the token behind a call.
It is random, never the token or its row's number. Each token made before gets its own, so
the apps it reaches see it from now on.
"""

import secrets

import sqlalchemy as sa
from alembic import op

revision = "ec8e858e1f10"
down_revision = "b81f4d2c6e90"


def upgrade() -> None:
    """Add each token's id, a new random one for every token there is."""
    # A database made by `create_all` and taken as the baseline may have the column already.
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("tokens")}
    if "public_id" in columns:
        return
    with op.batch_alter_table("tokens") as batch:
        batch.add_column(sa.Column("public_id", sa.String(20), nullable=True))
    tokens = sa.table("tokens", sa.column("id", sa.Integer()), sa.column("public_id"))
    for (token,) in op.get_bind().execute(sa.select(tokens.c.id)).all():
        # Written out, never from the server's code, so this revision stays what it was.
        made = f"tok_{secrets.token_hex(8)}"
        op.execute(tokens.update().where(tokens.c.id == token).values(public_id=made))
    with op.batch_alter_table("tokens") as batch:
        batch.alter_column("public_id", existing_type=sa.String(20), nullable=False)
        # Named as a table made from the models names it.
        batch.create_unique_constraint("tokens_public_id_key", ["public_id"])
