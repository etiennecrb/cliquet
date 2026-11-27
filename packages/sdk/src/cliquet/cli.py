"""The `cliquet` command line.

Commands that load an app run in the app's own environment, built by uv from its PEP 723
block: the command replaces itself with the app's Python and runs again.
"""

import argparse
import contextlib
import getpass
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from cliquet.context import Caller
from cliquet.errors import AppError, AppFailed
from cliquet.files import LOCK, declares
from cliquet.logs import log_to_stderr
from cliquet.manifest import (
    lock_source_problem,
    sdk_source_problem,
    uv_environment,
    without_uv_settings,
)
from cliquet.scaffold import ScaffoldError, new_app, sdk_folder, set_sdk_source
from cliquet.screenshot import THEMES, Theme
from cliquet.store import StoreError, scratch_repository, write_atomically

# Set when the command runs again inside the app's own environment.
_IN_APP_ENV = "CLIQUET_IN_APP_ENV"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="cliquet", description="Create, check and run Cliquet apps."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    new = commands.add_parser("new", help="create an app folder with its own git repository")
    new.add_argument("dir", type=Path, help="the folder to create")
    new.add_argument("--from", dest="source", type=Path, help="copy this app, such as an example")
    new.add_argument("--name", help="the app's name, by default the folder's")

    manifest = commands.add_parser("manifest", help="print an app's manifest as JSON")
    manifest.add_argument("app", type=Path, help="the app's folder")

    check = commands.add_parser("check", help="check an app's definition and current data")
    check.add_argument("app", type=Path, help="the app's folder")

    test = commands.add_parser("test", help="run an app's tests through the runtime")
    test.add_argument("app", type=Path, help="the app's folder, holding app.py and tests/")
    test.add_argument("pytest_args", nargs=argparse.REMAINDER, help="passed on to pytest")

    serve = commands.add_parser("serve", help="serve an app over MCP, for one caller")
    serve.add_argument("app", type=Path, help="the app's folder, the root of its git repository")
    serve.add_argument("--role", help="the role to act with; required if the app has several")
    serve.add_argument("--id", help="who is calling, by default your git user name")
    serve.add_argument("--http", action="store_true", help="serve streamable HTTP, not stdio")
    serve.add_argument("--host", default="127.0.0.1", help="the address for --http")
    serve.add_argument("--port", type=int, default=8765, help="the port for --http")

    preview = commands.add_parser(
        "preview", help="show an app's views in a browser, on a copy of its data"
    )
    preview.add_argument("app", type=Path, help="the app's folder")
    preview.add_argument("--role", help="the role to act with; required if the app has several")
    preview.add_argument("--id", default="preview", help="who is looking")
    preview.add_argument("--host", default="127.0.0.1", help="the address to serve on")
    preview.add_argument("--port", type=int, default=8766, help="the port to serve on")
    preview.add_argument(
        "--data",
        type=Path,
        metavar="FOLDER",
        help="show the app on a copy of the files in this folder, not on its own data",
    )
    preview.add_argument(
        "--view",
        action="append",
        metavar="NAME",
        help="only this view; may be given several times",
    )
    preview.add_argument(
        "--screenshot",
        type=Path,
        metavar="FOLDER",
        help="save a picture of each view in this folder, then stop",
    )
    preview.add_argument(
        "--script",
        type=Path,
        metavar="FILE",
        help="run the steps in this JSON file on one view, then stop",
    )
    preview.add_argument(
        "--out",
        type=Path,
        metavar="FOLDER",
        help="where --script writes screen.png and result.json",
    )
    preview.add_argument(
        "--theme",
        choices=THEMES,
        default="light",
        help="draw the views in this theme, light by default",
    )
    preview.add_argument(
        "--browser",
        metavar="PATH",
        help="the browser for --screenshot and --script, instead of the one found here",
    )
    preview.add_argument(
        "--browser-no-sandbox",
        action="store_true",
        help="run that browser without its own sandbox, as a container without one needs",
    )

    sandbox = commands.add_parser("sandbox", help="run the sandbox for the server")
    sandbox.add_argument("--root", type=Path, required=True, help="keep versions of apps here")
    sandbox.add_argument("--host", default="127.0.0.1", help="the address to listen on")
    sandbox.add_argument("--port", type=int, default=8900, help="the port to listen on")
    sandbox.add_argument(
        "--uds",
        metavar="PATH",
        help="listen on a unix socket here instead of an address: it needs a token",
    )

    args = parser.parse_args(argv)
    # Cliquet's log, such as an error's id and traceback, goes to stderr as it is written.
    log_to_stderr(("cliquet",))
    try:
        match args.command:
            case "new":
                return _new(args.dir, args.source, args.name)
            case "manifest":
                return _manifest(args.app, argv)
            case "check":
                return _check(args.app, argv)
            case "test":
                return run_tests(args.app.resolve(), args.pytest_args)
            case "serve":
                http = (args.host, args.port) if args.http else None
                return _serve(args.app, args.role, args.id, http, argv)
            case "preview":
                where = (args.host, args.port)
                shots = args.screenshot.resolve() if args.screenshot else None
                return _preview(
                    args.app,
                    args.role,
                    args.id,
                    where,
                    shots,
                    args.data,
                    args.view,
                    args.script,
                    args.out,
                    args.browser,
                    args.browser_no_sandbox,
                    args.theme,
                    argv,
                )
            case "sandbox":
                return _sandbox(args.root, args.host, args.port, args.uds)
    except (AppError, AppFailed, ScaffoldError, StoreError) as e:
        _fail(e)
        return 1
    return 2


def _fail(error: Exception) -> None:
    if isinstance(error, AppError):
        print("cliquet: the app's definition has problems:", file=sys.stderr)
        for problem in error.problems:
            print(f"  {problem}", file=sys.stderr)
    elif isinstance(error, AppFailed):
        print(f"cliquet: the app failed: {error}", file=sys.stderr)
    else:
        print(f"cliquet: {error}", file=sys.stderr)


# Commands


def _new(target: Path, source: Path | None, name: str | None) -> int:
    folder = new_app(target, source=source, name=name)
    print(f"Created {folder}, with its own git repository.")
    print(f"Next: cliquet test {target}")
    return 0


def _manifest(app_dir: Path, argv: list[str]) -> int:
    _in_app_env(app_dir, argv)
    from cliquet.manifest import build_manifest

    sys.path.insert(0, str(app_dir.resolve()))
    print(json.dumps(build_manifest(_app_py(app_dir)), indent=2))
    return 0


def _check(app_dir: Path, argv: list[str]) -> int:
    """Check the definition, then the current data: committed, and passing every invariant."""
    _in_app_env(app_dir, argv)
    from cliquet.limits import data_problem
    from cliquet.runtime import Runtime

    sys.path.insert(0, str(app_dir.resolve()))
    runtime = Runtime(_app_py(app_dir))
    try:
        dirty = runtime.store.dirty()
        if dirty:
            print(
                f"cliquet: data/ has changes not committed: {', '.join(dirty)}. "
                "Commit or discard them.",
                file=sys.stderr,
            )
            return 1
        files = runtime.store.read_declared(runtime.store.head(), runtime.manifest["files"])
        size = sum(len(content) for content in files.values())
        too_much = data_problem("The current data", 0, size)
        if too_much is not None:
            print(
                f"cliquet: {too_much} The server refuses it: keep less in data/.", file=sys.stderr
            )
            return 1
        refusal = runtime.check()
        if refusal is not None:
            print(f"cliquet: the current data breaks the app's rules:\n{refusal}", file=sys.stderr)
            return 1
        problems = runtime.check_views()
        if problems:
            print("cliquet: the app's views have problems:", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            return 1
    finally:
        runtime.close()
    print(
        f"{runtime.manifest['name']}: the definition, the current data and the views "
        "pass every check."
    )
    return 0


def _serve(
    app_dir: Path,
    role: str | None,
    caller_id: str | None,
    http: tuple[str, int] | None,
    argv: list[str],
) -> int:
    """Check the app and its data, then serve it over MCP for one caller."""
    root = _repository_root(app_dir)
    if root != app_dir.resolve():
        where = f"inside the repository at {root}" if root else "not in a git repository"
        raise StoreError(
            f"{app_dir} is {where}. cliquet serve commits to the app's own repository, so "
            f"the app folder must be its root. Make a copy with: cliquet new <dir> --from {app_dir}"
        )
    _in_app_env(app_dir, argv)
    from cliquet.runtime import EVERYONE, Runtime

    sys.path.insert(0, str(app_dir.resolve()))
    # One person serves their own copy of the app: they are its owner.
    runtime = Runtime(_app_py(app_dir), owner=EVERYONE)
    caller = Caller(caller_id or _default_id(app_dir), _serve_role(runtime.manifest, role))
    dirty = runtime.store.repair()
    if dirty:
        raise StoreError(
            f"data/ has changes not made by Cliquet: {', '.join(dirty)}. Commit or discard them."
        )
    refusal = runtime.check()
    if refusal is not None:
        print(f"cliquet: the current data breaks the app's rules:\n{refusal}", file=sys.stderr)
        return 1
    from cliquet.mcp_server import AppServer, read_instructions, serve_http, serve_stdio

    server = AppServer(runtime, caller, read_instructions(_app_py(app_dir)))
    who = f"{caller.id} ({caller.role})" if caller.role else caller.id
    where = f"http://{http[0]}:{http[1]}/mcp" if http else "stdio"
    print(f"{runtime.manifest['name']}: serving as {who} on {where}.", file=sys.stderr)
    try:
        if http:
            serve_http(server, *http)
        else:
            serve_stdio(server)
    finally:
        runtime.close()
    return 0


def _preview(
    app_dir: Path,
    role: str | None,
    caller_id: str,
    where: tuple[str, int],
    screenshots: Path | None,
    data: Path | None,
    requested: list[str] | None,
    script: Path | None,
    out: Path | None,
    browser: str | None,
    no_sandbox: bool,
    theme: Theme,
    argv: list[str],
) -> int:
    """Serve the app's views to a browser, on a copy of its data that is thrown away.

    With `screenshots`, save a picture of each view in that folder instead, and stop.
    With `data`, use the files in that folder instead of the app's own data.
    With `script`, run the steps in that JSON file on one view, and write the result.
    With `browser`, take the pictures with the browser at that path, and with `no_sandbox`,
    without its own sandbox. The views are drawn in `theme`.
    """
    _in_app_env(app_dir, argv)
    from cliquet.manifest import build_manifest
    from cliquet.mcp_server import AppServer
    from cliquet.preview import preview_server, screenshot_preview, serve_preview
    from cliquet.runtime import EVERYONE, Runtime
    from cliquet.script import check_steps, run_script

    script_data = None
    if script is not None:
        if screenshots is not None:
            raise StoreError("use --script or --screenshot, not both")
        if out is None:
            raise StoreError("--script needs --out FOLDER")
        try:
            payload = json.loads(script.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise StoreError(f"can't read the script: {error}") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("view"), str):
            raise StoreError('a script is {"view": "<name>", "steps": [...]}.')
        width = payload.get("width", 768)
        if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
            raise StoreError("a script's width is a whole number of pixels.")
        script_data = (
            payload["view"],
            check_steps(payload.get("steps")),
            width,
        )
        requested = [script_data[0]]

    sys.path.insert(0, str(app_dir.resolve()))
    if data is not None:
        data = data.resolve()
        if not data.is_dir():
            raise StoreError(f"{data} isn't a folder")
        manifest = build_manifest(_app_py(app_dir))
        found = (p.relative_to(data).as_posix() for p in data.rglob("*") if p.is_file())
        files = {
            name: (data / name).read_bytes()
            for name in sorted(found)
            if declares(manifest["files"], name)
        }
    else:
        source = Runtime(_app_py(app_dir))
        try:
            manifest = source.manifest
            files = source.store.read_declared(source.store.head(), manifest["files"])
        finally:
            source.close()
    caller = Caller(caller_id, _serve_role(manifest, role))
    with tempfile.TemporaryDirectory(prefix="cliquet-preview-") as scratch:
        # A preview shows the app to the person developing it: they are its owner.
        runtime = Runtime(
            _app_py(app_dir),
            data_dir=scratch_repository(Path(scratch), files),
            owner=EVERYONE,
        )
        try:
            server = AppServer(runtime, caller)
            available = [t.name for t in server.tools if t.meta and "ui" in t.meta]
            if requested:
                missing = [name for name in requested if name not in available]
                if missing:
                    raise StoreError(
                        f"no view {', '.join(missing)}. The views are: "
                        f"{', '.join(available) or 'none'}."
                    )
                views = requested
            else:
                views = available
            if not views:
                who = f" for the role {caller.role}" if caller.role else ""
                raise StoreError(f"{manifest['name']} has no view{who}. Add one with @app.view.")
            if script_data is not None:
                view, steps, width = script_data
                with preview_server(server, views, driver=True) as base_url:
                    result = run_script(base_url, view, steps, width, browser, no_sandbox, theme)
                assert out is not None
                folder = out.resolve()
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "screen.png").write_bytes(result.png)
                (folder / "result.json").write_text(
                    json.dumps(
                        {"calls": result.calls, "error": result.error, "height": result.height},
                        indent=2,
                    )
                )
                print(folder / "screen.png")
                print(folder / "result.json")
                return 0
            if screenshots is not None:
                taken = screenshot_preview(server, views, screenshots, browser, no_sandbox, theme)
                for path, _ in taken:
                    print(path)
                return 0
            host, port = where
            print(
                f"{manifest['name']}: previewing on a copy of the data. "
                "Changes made here are thrown away.",
                file=sys.stderr,
            )
            dark = "&theme=dark" if theme == "dark" else ""
            for view in views:
                print(f"  http://{host}:{port}/?view={view}{dark}", file=sys.stderr)
            serve_preview(server, host, port, views)
        finally:
            runtime.close()
    return 0


def clear_socket(path: str) -> str | None:
    """Remove the socket an earlier sandbox left there, or say why this path can't be bound."""
    here = Path(path)
    if here.is_socket():
        here.unlink()
        return None
    if here.exists():
        return f"{path} is not a socket: remove it, or give --uds another path"
    return None


def _sandbox(root: Path, host: str, port: int, uds: str | None) -> int:
    """Serve the sandbox, which builds versions and keeps their workers warm.

    On a unix socket it always takes a token: an address has 127.0.0.1 to fall back on.
    """
    import uvicorn

    from cliquet.sandbox import create_sandbox

    token = os.environ.get("CLIQUET_SANDBOX_TOKEN")
    if uds is not None:
        if not token:
            print(f"cliquet: set CLIQUET_SANDBOX_TOKEN to listen on {uds}", file=sys.stderr)
            return 1
        problem = clear_socket(uds)
        if problem is not None:
            print(f"cliquet: {problem}", file=sys.stderr)
            return 1
        print(
            f"cliquet sandbox: listening on the socket {uds}, keeping versions in {root}",
            file=sys.stderr,
        )
        # The socket is bound here, not by uvicorn: uvicorn opens up its permissions, and a
        # folder shared with the host, as Docker's is, can refuse that. The server's own token
        # is what keeps the sandbox to itself.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(uds)
        with contextlib.suppress(OSError):
            os.chmod(uds, 0o666)
        uvicorn.Server(uvicorn.Config(create_sandbox(root, token), log_level="warning")).run(
            sockets=[sock]
        )
        return 0
    if not token and host not in ("127.0.0.1", "localhost"):
        print(f"cliquet: set CLIQUET_SANDBOX_TOKEN to listen on {host}", file=sys.stderr)
        return 1
    print(
        f"cliquet sandbox: listening on http://{host}:{port}, keeping versions in {root}",
        file=sys.stderr,
    )
    uvicorn.run(create_sandbox(root, token), host=host, port=port, log_level="warning")
    return 0


def _serve_role(manifest: dict[str, object], role: str | None) -> str | None:
    roles = manifest["roles"]
    assert isinstance(roles, list)
    if not roles:
        if role is not None:
            raise StoreError(f"this app has no roles, so --role {role} doesn't apply")
        return None
    if role is None:
        if len(roles) > 1:
            raise StoreError(
                f"this app has several roles: choose one with --role ({', '.join(roles)})"
            )
        return roles[0]
    if role not in roles:
        raise StoreError(f"{role!r} is not one of this app's roles ({', '.join(roles)})")
    return role


def run_tests(app_dir: Path, pytest_args: list[str]) -> int:
    """Check the app's definition, then run its tests in its own environment."""
    app_py = _app_py(app_dir)
    if os.environ.get(_IN_APP_ENV):
        return _run_tests_here(app_dir, pytest_args)
    python = app_python(app_py)
    command = [
        "uv", "run", "--quiet", "--no-project", "--python", python, "--with", "pytest",
        "python", "-m", "cliquet.cli", "test", str(app_dir), *pytest_args,
    ]  # fmt: skip
    env = {**uv_environment(), _IN_APP_ENV: "1"}
    return subprocess.run(command, cwd=app_dir, env=env).returncode


def _run_tests_here(app_dir: Path, pytest_args: list[str]) -> int:
    import pytest

    from cliquet import testing
    from cliquet.manifest import build_manifest

    sys.path.insert(0, str(app_dir))
    manifest = build_manifest(app_dir / "app.py")
    # Every invariant must refuse in the tests, even if no test uses the harness.
    key = str((app_dir / "app.py").resolve())
    testing.seen[key] = (manifest["name"], [i["name"] for i in manifest["invariants"]])
    testing.refused.setdefault(key, set())
    args = [
        str(app_dir / "tests"),
        "--rootdir", str(app_dir),
        "-o", "addopts=",  # the app's environment has no xdist or repo test options
        "-p", "cliquet.pytest_plugin",
        "-p", "no:cacheprovider",
        *pytest_args,
    ]  # fmt: skip
    return int(pytest.main(args))


# Helpers


def _app_py(app_dir: Path) -> Path:
    app_py = app_dir.resolve() / "app.py"
    if not app_py.is_file():
        raise StoreError(f"{app_dir} has no app.py")
    return app_py


def _in_app_env(app_dir: Path, argv: list[str]) -> None:
    """Unless already there, replace this process with the same command in the app's env."""
    if os.environ.get(_IN_APP_ENV):
        return
    python = app_python(_app_py(app_dir))
    os.execve(python, [python, "-m", "cliquet.cli", *argv], {**os.environ, _IN_APP_ENV: "1"})


def app_python(app_py: Path) -> str:
    """The Python of the app's environment, built by uv from its PEP 723 block.

    The server keeps `app.py` with no `[tool.uv]`, so a downloaded app names no folder for
    the SDK. uv then builds from a copy that names `sdk_folder()`, as the sandbox does, and
    the app's own files stay as they are.
    """
    source = app_py.read_text()
    sdk = sdk_folder()
    filled = sdk is not None and without_uv_settings(source)
    if filled:
        source = set_sdk_source(source, sdk)
    # What uv reads is checked first: it takes the SDK from a folder, never an index.
    problem = sdk_source_problem(source, sdk)
    lock = app_py.with_name(LOCK)
    if problem is None and lock.is_file():
        problem = lock_source_problem(lock.read_text())
    if problem is not None:
        raise StoreError(problem)
    script = _copy_for_uv(app_py.resolve(), source) if filled else app_py
    env = uv_environment()
    synced = subprocess.run(
        ["uv", "sync", "--quiet", "--script", str(script)], capture_output=True, text=True, env=env
    )
    if synced.returncode != 0:
        raise StoreError(f"uv can't build the app's environment: {synced.stderr.strip()}")
    found = subprocess.run(
        ["uv", "python", "find", "--script", str(script)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return found.stdout.strip()


def _copy_for_uv(app_py: Path, source: str) -> Path:
    """Write `source`, and the app's lock if it has one, to this app's folder in the cache.

    uv keeps one environment per script path, so an app always gets the same folder. uv may
    update the copy of the lock; the app's own lock is never written.
    """
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    folder = cache / "cliquet" / "apps" / hashlib.sha256(str(app_py).encode()).hexdigest()[:16]
    write_atomically(folder / "app.py", source.encode())
    lock = app_py.with_name(LOCK)
    if lock.is_file():
        write_atomically(folder / LOCK, lock.read_bytes())
    else:
        (folder / LOCK).unlink(missing_ok=True)
    return folder / "app.py"


def _repository_root(folder: Path) -> Path | None:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=folder, capture_output=True, text=True
    )
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


def _default_id(app_dir: Path) -> str:
    result = subprocess.run(
        ["git", "config", "user.name"], cwd=app_dir, capture_output=True, text=True
    )
    return result.stdout.strip() or getpass.getuser()


if __name__ == "__main__":
    sys.exit(main())
