"""The server's database is migrated with Alembic as it opens: in one transaction, from any
revision this server knows, and from the tables a server made before it had migrations."""

import contextlib
import re
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from cliquet_server.db import Base, Database, Right, TokenLimit
from cliquet_server.migrations import BASELINE, SCRIPTS, DatabaseRefused, migrate
from cliquet_server.settings import Settings

from .server_helpers import NOW

# The newest revision this server knows.
HEAD = ScriptDirectory(str(SCRIPTS)).get_current_head()

# A revision on top of the server's, as a later one will be: a column, and a table.
NICKNAMES = """\
import sqlalchemy as sa
from alembic import op

revision = "0000000000a2"
down_revision = "{down}"


def upgrade() -> None:
    with op.batch_alter_table("people") as batch:
        batch.add_column(sa.Column("nickname", sa.String(40), nullable=True))
    op.create_table(
        "badges",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("people.id"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("insert into badges (person_id) select id from people")
"""

# The token limits as the baseline made them: one per token.
BASELINE_LIMITS = """\
create table token_limits (
    token_id integer not null references tokens (id),
    app_id integer not null references apps (id),
    role varchar(40) not null,
    primary key (token_id)
)
"""

# A revision that changes the tables, then fails.
BROKEN = """\
import sqlalchemy as sa
from alembic import op

revision = "0000000000b3"
down_revision = "{down}"


def upgrade() -> None:
    op.add_column("people", sa.Column("pronouns", sa.String(40), nullable=True))
    op.create_table("stickers", sa.Column("id", sa.Integer(), primary_key=True))
    op.execute("delete from apps")
    op.execute("insert into nowhere values (1)")
"""


@pytest.fixture
def url(settings: Settings) -> str:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.database_url


def _path(url: str) -> str:
    return url.removeprefix("sqlite:///")


def _revisions(tmp_path: Path, *revisions: str) -> Path:
    """The server's revisions, with `revisions` on top, each on the one before."""
    scripts = tmp_path / "scripts"
    shutil.copytree(SCRIPTS, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    down = HEAD
    for number, source in enumerate(revisions):
        (scripts / "versions" / f"extra_{number}.py").write_text(source.format(down=down))
        down = re.findall(r'^revision = "(\w+)"', source, re.MULTILINE)[0]
    return scripts


def _sql(url: str, query: str) -> list[tuple]:
    with contextlib.closing(sqlite3.connect(_path(url))) as raw, raw:
        return raw.execute(query).fetchall()


def _revision(url: str) -> str | None:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def _with_an_app(url: str) -> None:
    """Open the database, and keep an app and its roles in it."""
    database = Database(url)
    try:
        database.create_app("sam", "shop", NOW, email="sam@example.com")
        database.keep_roles("sam", "shop", ["owner"])
    finally:
        database.close()


def _before_the_rights(url: str) -> None:
    """Drop what the rights' revision added, as a database migrated before it lacks it."""
    _sql(url, "alter table people drop column rights")


def _has_the_app(url: str) -> None:
    database = Database(url)
    try:
        assert [app.name for app in database.apps("sam")] == ["shop"]
        assert database.kept_roles("sam", "shop") == ["owner"]
    finally:
        database.close()


def test_the_revisions_make_exactly_the_models_tables(url: str) -> None:
    # A model changed without a revision fails here.
    Database(url).close()
    assert _revision(url) == HEAD
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    finally:
        engine.dispose()


def test_a_database_from_before_the_migrations_keeps_its_rows(url: str) -> None:
    _with_an_app(url)
    # As a server before the migrations left it, made by `create_all`, with no stamp.
    _sql(url, "drop table alembic_version")
    _before_the_rights(url)
    _has_the_app(url)
    assert _revision(url) == HEAD


def test_a_database_from_before_the_migrations_gains_the_tables_it_lacks(url: str) -> None:
    _with_an_app(url)
    _sql(url, "drop table alembic_version")
    _sql(url, "drop table app_roles")
    _before_the_rights(url)
    database = Database(url)
    try:
        assert database.kept_roles("sam", "shop") is None
        database.keep_roles("sam", "shop", ["owner"])
    finally:
        database.close()
    _has_the_app(url)
    assert _revision(url) == HEAD


def test_a_new_revision_keeps_every_row(url: str, tmp_path: Path) -> None:
    _with_an_app(url)
    migrate(url, _revisions(tmp_path, NICKNAMES))
    assert _revision(url) == "0000000000a2"
    assert _sql(url, "select login, nickname from people") == [("sam", None)]
    assert _sql(url, "select count(*) from badges") == [(1,)]
    # This server doesn't know the revision: the rows are read as they are.
    assert _sql(url, "select name, roles from apps join app_roles on app_id = apps.id") == [
        ("shop", '["owner"]')
    ]


def test_a_token_limited_at_the_baseline_keeps_its_limit(url: str, tmp_path: Path) -> None:
    _with_an_app(url)
    database = Database(url)
    try:
        sam = database.person_by_login("sam")
        assert sam is not None
        _info, token = database.create_token(sam.id, "Routine", NOW)
    finally:
        database.close()
    # Back to the baseline's table, whose limits were keyed by the token alone.
    _sql(url, "drop table token_limits")
    _sql(url, BASELINE_LIMITS)
    _sql(url, f"update alembic_version set version_num = '{BASELINE}'")
    _before_the_rights(url)
    _sql(url, "insert into token_limits select tokens.id, apps.id, 'owner' from tokens, apps")
    database = Database(url)
    try:
        found = database.token_access(token, NOW)
        assert found is not None and found[1] == (TokenLimit("sam", "shop", "owner"),)
    finally:
        database.close()
    assert _revision(url) == HEAD


def test_everyone_there_before_the_rights_keeps_building(url: str) -> None:
    _with_an_app(url)
    # Back to the revision before the rights, when everyone the tenant let in could build.
    _before_the_rights(url)
    _sql(url, "update alembic_version set version_num = '8c41e2a7d903'")
    database = Database(url)
    try:
        sam = database.person_by_login("sam")
        assert sam is not None and sam.rights == {Right.BUILD}
        # Someone new holds no right until the server grants it.
        ann = database.person_for("https://example.auth0.com", "ann", "ann@example.com", "ann", NOW)
        assert ann.rights == frozenset()
    finally:
        database.close()
    assert _revision(url) == HEAD


def test_everyone_counts_as_using_their_apps_when_last_use_is_first_kept(url: str) -> None:
    _with_an_app(url)
    # Back to before the last use was kept.
    _sql(url, "alter table people drop column apps_last_used_at")
    _sql(url, "update alembic_version set version_num = '3f7a9c21d4b6'")
    before = datetime.now(UTC)
    database = Database(url)
    try:
        after = datetime.now(UTC)
        assert database.apps_used_since(before) == ["sam"]
        assert database.apps_used_since(after + timedelta(seconds=1)) == []
    finally:
        database.close()
    assert _revision(url) == HEAD


def test_nobody_is_deleted_when_deletions_are_first_kept(url: str) -> None:
    _with_an_app(url)
    # Back to before a deleted account was kept.
    _sql(url, "alter table people drop column deleted_at")
    _sql(url, "update alembic_version set version_num = 'a745ed039065'")
    database = Database(url)
    try:
        sam = database.person_by_login("sam")
        assert sam is not None and sam.deleted_at is None
        assert database.deleted_logins() == []
    finally:
        database.close()
    _has_the_app(url)
    assert _revision(url) == HEAD


def test_a_failing_revision_changes_nothing(url: str, tmp_path: Path) -> None:
    _with_an_app(url)
    tables = _sql(url, "select name, sql from sqlite_master order by name")
    with pytest.raises(DatabaseRefused) as refused:
        migrate(url, _revisions(tmp_path, NICKNAMES, BROKEN))
    assert str(refused.value) == (
        "the database can't be migrated to revision 0000000000b3, so nothing was changed: "
        "OperationalError: (sqlite3.OperationalError) no such table: nowhere. "
        "Run the server's previous version until a fixed one is out."
    )
    assert isinstance(refused.value.__cause__, Exception)
    # Not even the revision before it, which worked: every revision runs in one transaction.
    assert _revision(url) == HEAD
    assert _sql(url, "select name, sql from sqlite_master order by name") == tables
    _has_the_app(url)


def test_a_database_newer_than_the_server_is_refused(url: str) -> None:
    _with_an_app(url)
    _sql(url, "update alembic_version set version_num = 'f00dfeedbeef'")
    with pytest.raises(DatabaseRefused) as refused:
        Database(url)
    assert str(refused.value) == (
        "the database is at revision f00dfeedbeef, which this server doesn't know: a newer "
        "server migrated it. Run that newer server, or restore the database from the volume's "
        "snapshot taken before it ran."
    )
    assert _revision(url) == "f00dfeedbeef"


# The tokens as they were before each had an id.
TOKENS_WITHOUT_IDS = """\
create table tokens_before (
    id integer not null primary key,
    person_id integer not null references people (id),
    name varchar(60) not null,
    hash varchar(64) not null unique,
    created_at datetime not null,
    last_used_at datetime,
    limited boolean not null default 0,
    unique (person_id, name)
)
"""


def test_every_token_gets_its_own_id_when_ids_are_first_kept(url: str) -> None:
    _with_an_app(url)
    database = Database(url)
    try:
        sam = database.person_by_login("sam")
        assert sam is not None
        made = [database.create_token(sam.id, name, NOW)[1] for name in ("home", "phone")]
    finally:
        database.close()
    # Back to before a token had an id.
    _sql(url, TOKENS_WITHOUT_IDS)
    _sql(
        url,
        "insert into tokens_before select id, person_id, name, hash, created_at, "
        "last_used_at, limited from tokens",
    )
    _sql(url, "drop table tokens")
    _sql(url, "alter table tokens_before rename to tokens")
    _sql(url, "update alembic_version set version_num = 'b81f4d2c6e90'")
    database = Database(url)
    try:
        found = [database.token_access(token, NOW) for token in made]
        ids = [one[2].id for one in found if one is not None]
    finally:
        database.close()
    assert len(set(ids)) == 2 and all(re.fullmatch(r"tok_[0-9a-f]{16}", one) for one in ids)
    assert _revision(url) == HEAD


# The tokens as they were before a token could name the token that made it.
TOKENS_WITHOUT_MAKERS = """\
create table tokens_before (
    id integer not null primary key,
    person_id integer not null references people (id),
    name varchar(60) not null,
    hash varchar(64) not null unique,
    created_at datetime not null,
    last_used_at datetime,
    limited boolean not null default 0,
    public_id varchar(20) not null unique,
    unique (person_id, name)
)
"""


def test_every_token_is_kept_when_makers_are_first_kept(url: str) -> None:
    _with_an_app(url)
    database = Database(url)
    try:
        sam = database.person_by_login("sam")
        assert sam is not None
        made = [database.create_token(sam.id, name, NOW) for name in ("home", "phone")]
    finally:
        database.close()
    # Back to before a token named its maker.
    _sql(url, TOKENS_WITHOUT_MAKERS)
    _sql(
        url,
        "insert into tokens_before select id, person_id, name, hash, created_at, "
        "last_used_at, limited, public_id from tokens",
    )
    _sql(url, "drop table tokens")
    _sql(url, "alter table tokens_before rename to tokens")
    _sql(url, "update alembic_version set version_num = 'ec8e858e1f10'")
    database = Database(url)
    try:
        # Each works as before, and was made on the console or with a sign-in: by no token.
        for info, token in made:
            found = database.token_access(token, NOW)
            assert found is not None and found[2].id == info.public_id
        assert [one.made_by for one in database.tokens(sam.id)] == [None, None]
    finally:
        database.close()
    assert _sql(url, "select count(*) from tokens where made_by is null") == [(2,)]
    assert _revision(url) == HEAD


# The shares as they were before each kept the email it was shared with.
SHARES_WITHOUT_EMAILS = """\
create table shares_before (
    id integer not null primary key,
    person_id integer not null references people (id),
    app_id integer not null references apps (id),
    role varchar(40) not null,
    created_at datetime not null,
    unique (person_id, app_id, role)
)
"""


def test_every_share_takes_its_person_s_email_when_emails_are_first_kept(url: str) -> None:
    _with_an_app(url)
    database = Database(url)
    try:
        database.person_for("https://example.auth0.com", "pat", "pat@example.com", "pat", NOW)
        database.share("sam", "shop", "pat@example.com", "owner", NOW)
        # An owner imported with no email yet, whom no share could reach.
        database.create_app("ann", "notes", NOW)
    finally:
        database.close()
    # Back to before a share kept its email.
    _sql(url, SHARES_WITHOUT_EMAILS)
    _sql(
        url,
        "insert into shares_before select id, person_id, app_id, role, created_at from shares",
    )
    _sql(
        url,
        "insert into shares_before (person_id, app_id, role, created_at) "
        "select people.id, apps.id, 'owner', apps.created_at from people, apps "
        "where people.login = 'ann' and apps.name = 'shop'",
    )
    _sql(url, "drop table shares")
    _sql(url, "alter table shares_before rename to shares")
    _sql(url, "update alembic_version set version_num = '5b7877c729d0'")
    assert _sql(url, "select email from people where login = 'ann'") == [(None,)]
    assert _sql(url, "select count(*) from shares") == [(2,)]
    database = Database(url)
    try:
        shares = database.waiting_shares("sam", "shop")
        assert [(one.email, one.person.login, one.role) for one in shares] == [
            ("pat@example.com", "pat", "owner")
        ]
    finally:
        database.close()
    # The share whose person had no email, which nobody could list or cancel, is gone.
    assert _sql(url, "select email from shares") == [("pat@example.com",)]
    assert _revision(url) == HEAD


def test_every_token_never_expires_when_expiry_is_first_kept(url: str) -> None:
    _with_an_app(url)
    database = Database(url)
    try:
        sam = database.person_by_login("sam")
        assert sam is not None
        made = [database.create_token(sam.id, name, NOW) for name in ("home", "phone")]
    finally:
        database.close()
    # Back to before a token expired.
    _sql(url, "alter table tokens drop column expires_at")
    _sql(url, "update alembic_version set version_num = '635a7476112b'")
    database = Database(url)
    try:
        # Each still works, years later: it was made to work for good.
        later = NOW + timedelta(days=3650)
        for info, token in made:
            found = database.token_access(token, later)
            assert found is not None and found[2].id == info.public_id
        assert [one.expires_at for one in database.tokens(sam.id)] == [None, None]
    finally:
        database.close()
    assert _revision(url) == HEAD
