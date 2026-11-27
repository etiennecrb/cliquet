"""Keep the rights Cliquet grants each person, and let everyone who could build go on.

Revision ID: 3f7a9c21d4b6
Revises: 8c41e2a7d903

Every person the tenant signed in could build: there was no list of builders. Each person now
holds a list of rights, whose only one is build. Everyone already here keeps building; a person
made later holds none until the server grants it.
"""

import sqlalchemy as sa
from alembic import op

revision = "3f7a9c21d4b6"
down_revision = "8c41e2a7d903"


def upgrade() -> None:
    """Give every person a list of rights, and grant each one there is build."""
    with op.batch_alter_table("people") as batch:
        batch.add_column(sa.Column("rights", sa.Text(), nullable=False, server_default="[]"))
    op.execute("""update people set rights = '["build"]'""")
