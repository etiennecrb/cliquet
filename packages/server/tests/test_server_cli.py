import contextlib
import logging
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from cliquet_server import cli, db
from cliquet_server.cli import main
from cliquet_server.executor import LocalExecutor, SandboxAway
from cliquet_server.migrations import DatabaseRefused
from cliquet_server.settings import Settings

from .server_helpers import APPS, NOW


@pytest.fixture(autouse=True)
def cliquet_log() -> Iterator[None]:
    """A command sets up Cliquet's log: it is put back as it was after each test."""
    loggers = [logging.getLogger(name) for name in ("cliquet", "cliquet_server")]
    saved = [(logger.handlers[:], logger.level) for logger in loggers]
    yield
    for logger, (handlers, level) in zip(loggers, saved, strict=True):
        logger.handlers[:] = handlers
        logger.setLevel(level)


def test_the_server_logs_its_lines_and_the_sdk_s_with_one_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLIQUET_DATA_DIR", str(tmp_path / "data"))

    def run(app: object, **options: object) -> None:
        try:
            raise RuntimeError("a fault")
        except RuntimeError:
            logging.getLogger("cliquet_server.apps").exception("unexpected: it failed")
        logging.getLogger("cliquet.errors").warning("Error e-0123456789ab: the app's")

    monkeypatch.setattr(cli, "create_app", lambda settings: None)
    monkeypatch.setattr(cli.uvicorn, "run", run)
    assert main(["run"]) == 0
    error = capsys.readouterr().err
    assert "cliquet-server: listening on http://127.0.0.1:8800" in error
    # One search finds the server's failures, with their traceback, whichever logger.
    assert "cliquet-server: unexpected: it failed\nTraceback" in error
    assert "RuntimeError: a fault" in error
    assert "cliquet-server: Error e-0123456789ab: the app's" in error


@pytest.mark.parametrize(
    ("env", "euid", "said"),
    [
        ({}, 0, True),
        ({}, 1000, False),
        ({"CLIQUET_EXECUTOR": "sprites", "CLIQUET_SPRITES_TOKEN": "token"}, 0, False),
    ],
)
def test_only_a_root_local_executor_warns_about_the_read_only_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    env: dict[str, str],
    euid: int,
    said: bool,
) -> None:
    monkeypatch.setenv("CLIQUET_DATA_DIR", str(tmp_path / "data"))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(os, "geteuid", lambda: euid)
    monkeypatch.setattr(cli, "create_app", lambda settings: None)
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **options: None)
    assert main(["run"]) == 0
    assert ("the server runs as root" in capsys.readouterr().err) is said


def test_the_command_imports_an_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLIQUET_DATA_DIR", str(tmp_path / "data"))
    assert (
        main(
            [
                "import",
                str(APPS / "stock"),
                "--owner",
                "etienne",
                "--name",
                "shop",
                "--email",
                "etienne@example.com",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "/apps/etienne/shop" in out
    assert "MCP, for an assistant: http://127.0.0.1:8800/me/mcp" in out
    assert (
        "MCP, with a token limited to the app: http://127.0.0.1:8800/apps/etienne/shop/mcp" in out
    )
    # The owner it made builds, and one it didn't make keeps their rights.
    assert _rights("etienne") == {db.Right.BUILD}
    assert main(["withdraw", "etienne", "build"]) == 0
    assert main(["import", str(APPS / "stock"), "--owner", "etienne", "--name", "cafe"]) == 0
    assert _rights("etienne") == frozenset()


def _rights(login: str) -> frozenset[db.Right]:
    """The rights of the person with this login, in the database the settings name."""
    settings = Settings.from_env()
    database = db.open_database(settings.data_dir, settings.database_url)
    try:
        person = database.person_by_login(login)
        assert person is not None
        return person.rights
    finally:
        database.close()


def test_the_command_grants_and_withdraws_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLIQUET_DATA_DIR", str(tmp_path / "data"))
    settings = Settings.from_env()
    database = db.open_database(settings.data_dir, settings.database_url)
    database.person_for("https://example.auth0.com", "ann", "ann@example.com", "ann", NOW)
    database.close()
    assert main(["grant", "ann", "build"]) == 0
    assert capsys.readouterr().out == "ann now has the right build.\n"
    assert _rights("ann") == {db.Right.BUILD}
    assert main(["withdraw", "ann", "build"]) == 0
    assert capsys.readouterr().out == "ann no longer has the right build.\n"
    assert _rights("ann") == frozenset()
    # Plus is read by the sandbox meter once a minute, not at each request.
    assert main(["grant", "ann", "plus"]) == 0
    assert capsys.readouterr().out == (
        "ann now has the right plus.\nA running server applies it within a minute.\n"
    )


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (
            ["grant", "ann", "admin"],
            "cliquet-server: there is no right called 'admin': the rights are build, plus.",
        ),
        (
            ["withdraw", "nobody", "build"],
            "cliquet-server: Nobody has the login 'nobody' on this server: check it, or wait "
            "until they have signed in and chosen it.",
        ),
    ],
)
def test_the_command_refuses_an_unknown_right_or_login(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    said: str,
) -> None:
    monkeypatch.setenv("CLIQUET_DATA_DIR", str(tmp_path / "data"))
    assert main(args) == 1
    assert capsys.readouterr().err.splitlines() == [said]


def test_the_command_says_why_it_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    monkeypatch.setenv("CLIQUET_DATA_DIR", str(tmp_path / "data"))
    assert (
        main(["import", str(folder), "--owner", "etienne", "--email", "etienne@example.com"]) == 1
    )
    error = capsys.readouterr().err
    assert any(
        line.startswith("cliquet-server: ") and "has no app.py" in line
        for line in error.splitlines()
    )


def _an_account_to_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """ann, with an app never published, its draft, and her builds in the local sandbox."""
    monkeypatch.setenv("CLIQUET_DATA_DIR", str(tmp_path / "data"))
    settings = Settings.from_env()
    database = db.open_database(settings.data_dir, settings.database_url)
    database.person_for("https://example.auth0.com/", "auth0|ann", "ann@example.com", "ann", NOW)
    database.create_app("ann", "notes", NOW)
    database.close()
    for folder in ("drafts/ann/notes", "sandbox/apps/ann/notes"):
        (settings.data_dir / folder).mkdir(parents=True)
    return settings


def test_the_command_deletes_an_account_and_says_what_it_forgot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _an_account_to_delete(tmp_path, monkeypatch)
    assert main(["delete-account", "ann", "--confirm", "ann"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Forgot its identity. Delete the person at the sign-in provider too:",
        "  Issuer: https://example.auth0.com/",
        "  Subject: auth0|ann",
        "  Email: ann@example.com",
        "Deleted the account ann and everything it owned. Its login stays reserved.",
    ]
    for folder in ("drafts/ann", "sandbox/apps/ann"):
        assert not (settings.data_dir / folder).exists()
    database = db.open_database(settings.data_dir, settings.database_url)
    try:
        assert database.apps("ann") == [] and database.person_by_login("ann") is None
        account = database.account("ann")
        assert account is not None and account.deleted_at is not None
    finally:
        database.close()
    # Run again, as after a stop, it finishes what is left, and forgets nothing more.
    (settings.data_dir / "apps" / "ann" / "notes").mkdir(parents=True)
    assert main(["delete-account", "ann", "--confirm", "ann"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "The account ann was deleted already: finishing what is left of it.",
        "It has no identity to forget: it had none, or an earlier run forgot it.",
        "Deleted the account ann and everything it owned. Its login stays reserved.",
    ]
    assert not (settings.data_dir / "apps" / "ann").exists()


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (
            ["delete-account", "ann", "--confirm", "an"],
            "cliquet-server: to delete the account ann, pass --confirm ann: nothing was deleted.",
        ),
        (
            ["delete-account", "nobody", "--confirm", "nobody"],
            "cliquet-server: nobody has or had the login 'nobody' here.",
        ),
    ],
)
def test_the_command_deletes_nothing_without_the_login_twice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    said: str,
) -> None:
    settings = _an_account_to_delete(tmp_path, monkeypatch)
    assert main(args) == 1
    assert capsys.readouterr().err.splitlines() == [said]
    assert (settings.data_dir / "drafts" / "ann" / "notes").is_dir()
    database = db.open_database(settings.data_dir, settings.database_url)
    try:
        assert database.person_by_login("ann") is not None
    finally:
        database.close()


def test_a_deletion_the_sandbox_stops_says_to_run_it_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _an_account_to_delete(tmp_path, monkeypatch)

    def away(executor: object, owner: str) -> None:
        raise SandboxAway("can't reach Sprites. Error e-0123456789ab has the details.")

    monkeypatch.setattr(LocalExecutor, "destroy", away)
    assert main(["delete-account", "ann", "--confirm", "ann"]) == 1
    captured = capsys.readouterr()
    assert "  Email: ann@example.com" in captured.out
    assert captured.err.splitlines()[-1] == (
        "cliquet-server: the deletion stopped: can't reach Sprites. Error e-0123456789ab has "
        "the details. The account is closed: run the same command again to finish it."
    )
    monkeypatch.undo()
    monkeypatch.setenv("CLIQUET_DATA_DIR", str(tmp_path / "data"))
    assert main(["delete-account", "ann", "--confirm", "ann"]) == 0


def test_import_needs_an_email_for_a_new_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLIQUET_DATA_DIR", str(tmp_path / "data"))
    assert main(["import", str(APPS / "stock"), "--owner", "new", "--name", "shop"]) == 1
    error = capsys.readouterr().err
    assert error.startswith("cliquet-server:")
    assert "--email" in error


def test_the_server_refuses_to_serve_a_database_it_doesn_t_know(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv("CLIQUET_DATA_DIR", str(data))
    assert main(["forget-sandbox", "sam"]) == 0
    with contextlib.closing(sqlite3.connect(data / "server.db")) as raw, raw:
        raw.execute("update alembic_version set version_num = 'f00dfeedbeef'")
    capsys.readouterr()
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **options: pytest.fail("it served"))
    assert main(["run"]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "cliquet-server: the database is at revision f00dfeedbeef, which this server doesn't "
        "know: a newer server migrated it. Run that newer server, or restore the database "
        "from the volume's snapshot taken before it ran."
    ]


def test_a_failed_migration_is_logged_as_unexpected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(url: str) -> None:
        try:
            raise RuntimeError("the revision broke")
        except RuntimeError as error:
            raise DatabaseRefused("the database can't be migrated") from error

    monkeypatch.setenv("CLIQUET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(db, "migrate", fail)
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **options: pytest.fail("it served"))
    assert main(["run"]) == 1
    error = capsys.readouterr().err
    # One search finds it with the server's other failures, with the revision's traceback.
    assert error.startswith("cliquet-server: unexpected: the database can't be migrated\n")
    assert "Traceback" in error and "RuntimeError: the revision broke" in error
