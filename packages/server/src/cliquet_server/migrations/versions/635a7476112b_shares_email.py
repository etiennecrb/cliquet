"""Keep on each share the email its owner shared it with.

Revision ID: 635a7476112b
Revises: 5b7877c729d0

Until a share is accepted, its owner knows only the email they typed, and the share is listed
and cancelled by it. The person's own email changes when they sign in with a new one, so the
share keeps the one typed. A share made before kept none: it takes its person's email, the
best there is. A person loses their email only when their account is closed, which drops the
shares waiting for them, so a share whose person has none can't be: if one were, nobody could
list or cancel it, and it goes.
"""

import sqlalchemy as sa
from alembic import op

revision = "635a7476112b"
down_revision = "5b7877c729d0"


def upgrade() -> None:
    """Add the email each share was made with, its person's email for every share there is."""
    # A database made by `create_all` and taken as the baseline may have the column already.
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("shares")}
    if "email" in columns:
        return
    with op.batch_alter_table("shares") as batch:
        batch.add_column(sa.Column("email", sa.String(320), nullable=True))
    shares = sa.table(
        "shares", sa.column("person_id", sa.Integer()), sa.column("email", sa.String(320))
    )
    people = sa.table("people", sa.column("id", sa.Integer()), sa.column("email", sa.String(320)))
    theirs = sa.select(people.c.email).where(people.c.id == shares.c.person_id).scalar_subquery()
    op.execute(shares.update().values(email=theirs))
    op.execute(shares.delete().where(shares.c.email.is_(None)))
    with op.batch_alter_table("shares") as batch:
        batch.alter_column("email", existing_type=sa.String(320), nullable=False)


def downgrade() -> None:
    """Drop the email each share was made with: a share is listed by its person's again."""
    with op.batch_alter_table("shares") as batch:
        batch.drop_column("email")
