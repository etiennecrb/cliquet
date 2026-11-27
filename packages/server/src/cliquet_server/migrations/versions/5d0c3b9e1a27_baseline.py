"""The tables the server had before it had migrations.

Revision ID: 5d0c3b9e1a27
Revises:

Written out in full, never from the models, so it stays what it was when they change. A
database made before the migrations has some or all of these tables already: each table
it lacks is made, as `create_all` did at each start then.
"""

import sqlalchemy as sa
from alembic import op

revision = "5d0c3b9e1a27"
down_revision = None


def upgrade() -> None:
    """Make every table the server had before it had migrations."""
    op.create_table(
        "people",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("issuer", sa.String(500), nullable=True),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("login", sa.String(39), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer", "subject"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("login"),
        if_not_exists=True,
    )
    op.create_table(
        "apps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "name"),
        sa.ForeignKeyConstraint(["owner_id"], ["people.id"]),
        if_not_exists=True,
    )
    op.create_table(
        "sandboxes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("token", sa.String(500), nullable=False),
        sa.Column("sdk", sa.String(64), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id"),
        sa.ForeignKeyConstraint(["owner_id"], ["people.id"]),
        if_not_exists=True,
    )
    op.create_table(
        "tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("person_id", "name"),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"]),
        sa.UniqueConstraint("hash"),
        if_not_exists=True,
    )
    op.create_table(
        "app_errors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("app_id", sa.Integer(), nullable=False),
        sa.Column("error_id", sa.String(40), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("caller", sa.String(39), nullable=True),
        sa.Column("role", sa.String(40), nullable=True),
        sa.Column("tool", sa.String(100), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_id", "error_id"),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"]),
        if_not_exists=True,
    )
    op.create_table(
        "app_looks",
        sa.Column("app_id", sa.Integer(), nullable=False),
        sa.Column("accent", sa.String(7), nullable=True),
        sa.Column("icon", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("app_id"),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"]),
        if_not_exists=True,
    )
    op.create_table(
        "app_roles",
        sa.Column("app_id", sa.Integer(), nullable=False),
        sa.Column("roles", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("app_id"),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"]),
        if_not_exists=True,
    )
    op.create_table(
        "app_titles",
        sa.Column("app_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("app_id"),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"]),
        if_not_exists=True,
    )
    op.create_table(
        "invitations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("app_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("role", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_id", "email", "role"),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"]),
        if_not_exists=True,
    )
    for table in ("memberships", "shares"):
        op.create_table(
            table,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("person_id", sa.Integer(), nullable=False),
            sa.Column("app_id", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("person_id", "app_id", "role"),
            sa.ForeignKeyConstraint(["person_id"], ["people.id"]),
            sa.ForeignKeyConstraint(["app_id"], ["apps.id"]),
            if_not_exists=True,
        )
    op.create_table(
        "token_limits",
        sa.Column("token_id", sa.Integer(), nullable=False),
        sa.Column("app_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("token_id"),
        sa.ForeignKeyConstraint(["token_id"], ["tokens.id"]),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"]),
        if_not_exists=True,
    )
