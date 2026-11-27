"""Keep whether the sign-in provider verified each person's email.

Revision ID: 92f6b8e89a0a
Revises: 159deba0fc18

A person whose email isn't verified yet gets in with no email, and claims nothing by one. Once
the provider verifies it, they keep no email when another account has it, so having no email
can't say whether it was verified. Everyone already here signed in with a verified email, or
was made by the operator, who vouches for them: each is verified.
"""

import sqlalchemy as sa
from alembic import op

revision = "92f6b8e89a0a"
down_revision = "159deba0fc18"


def upgrade() -> None:
    """Add whether each person's email is verified, yes for everyone there is."""
    # A database made by `create_all` and taken as the baseline may have the column already.
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("people")}
    if "email_verified" in columns:
        return
    with op.batch_alter_table("people") as batch:
        batch.add_column(
            sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade() -> None:
    """Drop whether each person's email is verified."""
    with op.batch_alter_table("people") as batch:
        batch.drop_column("email_verified")
