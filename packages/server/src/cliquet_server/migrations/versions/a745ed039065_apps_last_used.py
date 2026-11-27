"""Keep when each owner's apps were last used.

Revision ID: a745ed039065
Revises: 3f7a9c21d4b6

At boot, the server warms only the sandboxes of owners whose apps were used lately. Nobody's
last use is known yet, so every person gets the migration's time: each is warmed until the
cutoff passes, then only a real use counts.
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "a745ed039065"
down_revision = "3f7a9c21d4b6"


def upgrade() -> None:
    """Add each person's last use of their apps, and take it as now for everyone."""
    # A database made by `create_all` and taken as the baseline may have the column already.
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("people")}
    if "apps_last_used_at" not in columns:
        with op.batch_alter_table("people") as batch:
            batch.add_column(
                sa.Column("apps_last_used_at", sa.DateTime(timezone=True), nullable=True)
            )
    people = sa.table("people", sa.column("apps_last_used_at", sa.DateTime(timezone=True)))
    op.execute(people.update().values(apps_last_used_at=datetime.now(UTC)))
