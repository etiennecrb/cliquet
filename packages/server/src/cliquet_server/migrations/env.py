"""Alembic's environment for the server's database.

The server passes its own connection, in a transaction it commits only once every revision
has run. Run by hand from the repository's root, it opens the database the server's settings
name, in `cliquet-data/` by default: `uv run alembic upgrade head`, then
`uv run alembic revision --autogenerate -m "..."` writes a revision for a change to the
models, to check and complete.
"""

from alembic import context
from sqlalchemy import Connection

from cliquet_server.db import Base
from cliquet_server.migrations import migration_engine
from cliquet_server.settings import Settings


def _run(connection: Connection) -> None:
    """Run the revisions on `connection`, all in its one transaction."""
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        # SQLite alters a table by making it again: a revision does so with
        # `op.batch_alter_table`, as autogenerate writes it.
        render_as_batch=True,
        transactional_ddl=True,
        transaction_per_migration=False,
    )
    with context.begin_transaction():
        context.run_migrations()


given = context.config.attributes.get("connection")
if given is not None:
    _run(given)
else:
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = migration_engine(settings.database_url)
    try:
        with engine.begin() as connection:
            _run(connection)
    finally:
        engine.dispose()
