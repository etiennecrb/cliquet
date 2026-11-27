"""The server's database migrations: Alembic's revisions, in `versions/`, which the server
runs when it opens its database, before it serves.

All the revisions a database lacks run in one transaction, so a failed one leaves the
database as it was. A database at a revision this server doesn't know is refused, and nothing
is changed. One made before the migrations, by `create_all`, has no revision: it is taken
as the baseline.
"""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, inspect
from sqlalchemy.engine import make_url

_log = logging.getLogger(__name__)

SCRIPTS = Path(__file__).parent
# The schema of the server before it had migrations, when `create_all` made the tables. A
# database made then has no stamp, and is taken to be at this revision.
BASELINE = "5d0c3b9e1a27"


class DatabaseRefused(Exception):
    """A database the server won't serve from. Its message is one line: what is wrong with
    it, and what to do."""


def migrate(url: str, scripts: Path = SCRIPTS) -> None:
    """Bring the database at `url` to the newest revision in `scripts`, in one transaction,
    or raise `DatabaseRefused` and change nothing."""
    script = ScriptDirectory.from_config(_config(scripts))
    head = script.get_current_head()
    engine = migration_engine(url)
    try:
        with engine.begin() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
            if current == head:
                return
            if current is None:
                # Empty, or made by `create_all` before the migrations: the baseline makes
                # each table it lacks, as such a server did at each start.
                before = "no revision" if inspect(connection).get_table_names() else None
            elif current in {revision.revision for revision in script.walk_revisions()}:
                before = f"revision {current}"
            else:
                raise DatabaseRefused(
                    f"the database is at revision {current}, which this server doesn't know: "
                    "a newer server migrated it. Run that newer server, or restore the "
                    "database from the volume's snapshot taken before it ran."
                )
            try:
                command.upgrade(_config(scripts, connection), "head")
            except Exception as error:
                raise DatabaseRefused(
                    f"the database can't be migrated to revision {head}, so nothing was "
                    f"changed: {_first_line(error)}. Run the server's previous version until "
                    "a fixed one is out."
                ) from error
        if before is not None:
            _log.info("migrated the database to revision %s, from %s", head, before)
    finally:
        engine.dispose()


def migration_engine(url: str) -> Engine:
    """An engine whose transactions hold DDL too: SQLite's driver otherwise commits each
    `CREATE` and `ALTER` as it runs, so a failed revision would keep what it did."""
    if make_url(url).get_backend_name() == "sqlite":
        return create_engine(url, connect_args={"autocommit": False})
    return create_engine(url)


def _config(scripts: Path, connection: Connection | None = None) -> Config:
    """Alembic's configuration for the revisions in `scripts`, run on `connection`."""
    config = Config()
    config.set_main_option("script_location", str(scripts))
    config.attributes["connection"] = connection
    return config


def _first_line(error: Exception) -> str:
    """The error's type and the first line of its text, at most 300 characters."""
    text = str(error).strip().splitlines()
    line = f"{type(error).__name__}: {text[0]}" if text else type(error).__name__
    return line[:300]
