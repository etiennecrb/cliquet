"""Let a token be limited to several apps, one role in each.

Revision ID: 8c41e2a7d903
Revises: 5d0c3b9e1a27

A token's limits were keyed by the token alone, so it could hold only one. They are now keyed
by the token and the app: a token holds one role in each app it works for. Each limit is kept.
A token now says itself whether it is limited, so one that loses its last limit works nowhere,
never everywhere.
"""

import sqlalchemy as sa
from alembic import op

revision = "8c41e2a7d903"
down_revision = "5d0c3b9e1a27"


def upgrade() -> None:
    """Key the token limits by the token and the app, keeping every limit, and mark each
    token that has one as limited."""
    op.create_table(
        "token_limits_new",
        sa.Column("token_id", sa.Integer(), nullable=False),
        sa.Column("app_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(40), nullable=False),
        # Named as a table made from the models names them, since a rename keeps the names.
        sa.PrimaryKeyConstraint("token_id", "app_id", name="token_limits_pkey"),
        sa.ForeignKeyConstraint(["token_id"], ["tokens.id"], name="token_limits_token_id_fkey"),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"], name="token_limits_app_id_fkey"),
    )
    op.execute(
        "insert into token_limits_new (token_id, app_id, role) "
        "select token_id, app_id, role from token_limits"
    )
    op.drop_table("token_limits")
    op.rename_table("token_limits_new", "token_limits")
    # A database made by `create_all` and taken as the baseline may have the column already.
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("tokens")}
    if "limited" not in columns:
        with op.batch_alter_table("tokens") as batch:
            batch.add_column(
                sa.Column("limited", sa.Boolean(), nullable=False, server_default=sa.false())
            )
    op.execute("update tokens set limited = true where id in (select token_id from token_limits)")
