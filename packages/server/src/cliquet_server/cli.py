"""The `cliquet-server` command line."""

import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn

from cliquet.logs import log_to_stderr
from cliquet_server import __version__
from cliquet_server.accounts import Accounts
from cliquet_server.app import create_app
from cliquet_server.app_auth import app_address, me_address
from cliquet_server.apps import Apps, PublishRefused
from cliquet_server.builder import Builder, builder_enabled
from cliquet_server.db import AppExists, Forgotten, Right, open_database
from cliquet_server.executor import BuildProblems, SandboxError, make_executor
from cliquet_server.migrations import DatabaseRefused
from cliquet_server.settings import Settings, SettingsError

_log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="cliquet-server", description="Run the Cliquet server.")
    parser.add_argument("--version", action="version", version=f"cliquet-server {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run the server")
    run.add_argument("--host", default="127.0.0.1", help="the address to listen on")
    run.add_argument("--port", type=int, default=8800, help="the port to listen on")

    import_app = commands.add_parser("import", help="import an app folder as a new app")
    import_app.add_argument("folder", type=Path, help="the app's folder")
    import_app.add_argument("--owner", required=True, help="the login that will own it")
    import_app.add_argument("--name", help="the app's name, by default the folder's")
    import_app.add_argument(
        "--email",
        help="the email the owner signs in with; needed when the login has no person yet",
    )

    for command, verb in (("grant", "grant a person"), ("withdraw", "withdraw from a person")):
        change = commands.add_parser(command, help=f"{verb} a right across the server")
        change.add_argument("login", help="the person's login")
        change.add_argument("right", help=f"the right: {', '.join(Right)}")

    forget = commands.add_parser("forget-sandbox", help="forget an owner's sandbox")
    forget.add_argument("login", help="the owner whose sandbox should be set up again")

    delete = commands.add_parser(
        "delete-account", help="delete a person's account, with everything it owns"
    )
    delete.add_argument("login", help="the person's login")
    delete.add_argument("--confirm", required=True, help="the same login again, to confirm")

    args = parser.parse_args(argv)
    # The server's own lines and the SDK's, such as how long each app took to start, or an
    # error's id and traceback: uvicorn sets up only its own loggers. One prefix for both, so
    # `cliquet-server: unexpected:` finds every failure of Cliquet's own.
    log_to_stderr(("cliquet_server", "cliquet"), "cliquet-server: %(message)s")
    try:
        match args.command:
            case "run":
                return _run(args.host, args.port)
            case "import":
                return _import(args.folder, args.owner, args.name, args.email)
            case "grant" | "withdraw":
                return _change_right(args.command, args.login, args.right)
            case "forget-sandbox":
                return _forget_sandbox(args.login)
            case "delete-account":
                return _delete_account(args.login, args.confirm)
    except SettingsError as e:
        print(f"cliquet-server: {e}", file=sys.stderr)
        return 1
    except DatabaseRefused as e:
        if e.__cause__ is None:
            _log.error("%s", e)
        else:
            # A revision that failed is a fault of Cliquet's own.
            _log.error("unexpected: %s", e, exc_info=e.__cause__)
        return 1
    return 2


def _import(folder: Path, owner: str, name: str | None, email: str | None) -> int:
    """Import an app folder, publish it, and say where it is served."""
    settings = Settings.from_env()
    _warn_about_root(settings)
    database = open_database(settings.data_dir, settings.database_url)
    if email is None and database.person_by_login(owner) is None:
        database.close()
        print(
            f"cliquet-server: {owner} has no person yet: pass --email with the email they "
            "sign in with, so that only that email can claim the app",
            file=sys.stderr,
        )
        return 1
    executor = make_executor(settings, database)
    apps = Apps(settings, database, executor)
    try:
        live = apps.import_folder(folder, owner, name, email=email)
    except (BuildProblems, PublishRefused, AppExists, ValueError, SandboxError) as error:
        _fail_import(error)
        return 1
    finally:
        apps.close()
        database.close()
        executor.close()
    _print_imported(settings, live)
    return 0


def _change_right(command: str, login: str, name: str) -> int:
    """Grant or withdraw a right, and say whether the person holds it now."""
    if name not in Right:
        rights = ", ".join(Right)
        print(
            f"cliquet-server: there is no right called {name!r}: the rights are {rights}.",
            file=sys.stderr,
        )
        return 1
    settings = Settings.from_env()
    database = open_database(settings.data_dir, settings.database_url)
    change = database.grant if command == "grant" else database.withdraw
    try:
        change(login, Right(name))
    except ValueError as error:
        print(f"cliquet-server: {error}", file=sys.stderr)
        return 1
    finally:
        database.close()
    print(f"{login} {'now has' if command == 'grant' else 'no longer has'} the right {name}.")
    if name == Right.PLUS:
        print("A running server applies it within a minute.")
    return 0


def _forget_sandbox(login: str) -> int:
    """Remove the owner's sandbox row, so the next call sets it up again."""
    settings = Settings.from_env()
    database = open_database(settings.data_dir, settings.database_url)
    try:
        database.delete_sandbox(login)
    finally:
        database.close()
    print(f"Forgot the sandbox for {login}. Restart the server: until then, it keeps using it.")
    return 0


def _delete_account(login: str, confirm: str) -> int:
    """Delete the account with this login and everything it owns, or finish a deletion that
    stopped, and say what to delete at the sign-in provider."""
    if confirm != login:
        print(
            f"cliquet-server: to delete the account {login}, pass --confirm {login}: "
            "nothing was deleted.",
            file=sys.stderr,
        )
        return 1
    settings = Settings.from_env()
    database = open_database(settings.data_dir, settings.database_url)
    account = database.account(login)
    if account is None:
        database.close()
        print(f"cliquet-server: nobody has or had the login {login!r} here.", file=sys.stderr)
        return 1
    if account.deleted_at is not None:
        print(f"The account {login} was deleted already: finishing what is left of it.")
    executor = make_executor(settings, database)
    apps = Apps(settings, database, executor)
    builder = Builder(apps, settings) if builder_enabled(settings) else None
    try:
        Accounts(apps, builder).delete(login, forgotten=_print_forgotten)
    except SandboxError as error:
        said = str(error).rstrip(".")
        print(
            f"cliquet-server: the deletion stopped: {said}. The account is closed: run the "
            "same command again to finish it.",
            file=sys.stderr,
        )
        return 1
    finally:
        apps.close()
        database.close()
        executor.close()
    print(f"Deleted the account {login} and everything it owned. Its login stays reserved.")
    return 0


def _print_forgotten(forgotten: Forgotten) -> None:
    """Say what was forgotten of the person's identity, for the operator to delete them at
    the sign-in provider, as soon as it is: nothing else keeps it."""
    if forgotten.issuer is None and forgotten.subject is None and forgotten.email is None:
        print("It has no identity to forget: it had none, or an earlier run forgot it.")
        return
    print("Forgot its identity. Delete the person at the sign-in provider too:")
    print(f"  Issuer: {forgotten.issuer or 'none'}")
    print(f"  Subject: {forgotten.subject or 'none'}")
    print(f"  Email: {forgotten.email or 'none'}")


def _print_imported(settings: Settings, live) -> None:
    login, name = live.info.login, live.info.name
    print(f"Imported {login}/{name}, version {live.version}.")
    print(f"  Page: {settings.base_url}/apps/{login}/{name}")
    print(f"  MCP, for an assistant: {me_address(settings.base_url)}")
    print(f"  MCP, with a token limited to the app: {app_address(settings.base_url, login, name)}")


def _fail_import(error: Exception) -> None:
    if isinstance(error, BuildProblems):
        print("cliquet-server: the version can't be built:", file=sys.stderr)
        for problem in error.problems:
            print(f"  {problem}", file=sys.stderr)
    elif isinstance(error, PublishRefused):
        print("cliquet-server: the version can't go live:", file=sys.stderr)
        for reason in error.reasons:
            print(f"  {reason}", file=sys.stderr)
    else:
        print(f"cliquet-server: {error}", file=sys.stderr)


def _warn_about_root(settings: Settings) -> None:
    """Say that the local executor runs app code as root, where the read-only modes hold nothing."""
    if settings.executor == "local" and os.geteuid() == 0:
        _log.warning(
            "the server runs as root: app code under the local executor can write anywhere, "
            "and a query or a view that writes is not refused. Run the server as a user of its own."
        )


def _run(host: str, port: int) -> int:
    """Read the settings, open the database, say where the server listens, and serve it
    until stopped."""
    settings = Settings.from_env()
    app = create_app(settings)
    # After the app is made: a server that refuses to start says only why.
    _warn_about_root(settings)
    _log.info("listening on http://%s:%s, data in %s", host, port, settings.data_dir)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
