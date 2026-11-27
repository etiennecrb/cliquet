"""Keep how long each owner's sandbox was busy each month.

Revision ID: d4a1c7e93b20
Revises: 92f6b8e89a0a

A server with a monthly budget of sandbox time refuses calls once it is used, so the count
must survive a restart. One row per person and calendar month, in whole seconds, written at
most once a minute per owner while a budget is set.
"""

import sqlalchemy as sa
from alembic import op

revision = "d4a1c7e93b20"
down_revision = "92f6b8e89a0a"


def upgrade() -> None:
    """Make the table of each owner's sandbox time per month."""
    op.create_table(
        "sandbox_time",
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("seconds", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("person_id", "month", name="sandbox_time_pkey"),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], name="sandbox_time_person_id_fkey"),
        # A database made by `create_all` and taken as the baseline may have it already.
        if_not_exists=True,
    )
