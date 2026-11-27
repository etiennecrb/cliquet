import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from cliquet.logs import log_to_stderr
from cliquet.scaffold import ScaffoldError, local_sdk, new_app, sdk_pin, set_sdk_source

REPO = Path(__file__).parents[3]
# The test apps, as conftest names them: a file with a `# /// script` block in a string
# is a script to ty, which then can't import conftest.
APPS = Path(__file__).parent / "apps"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Sam",
    "GIT_AUTHOR_EMAIL": "sam@localhost",
    "GIT_COMMITTER_NAME": "Sam",
    "GIT_COMMITTER_EMAIL": "sam@localhost",
}


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)


def test_cliquet_s_log_goes_to_stderr_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    logger = logging.getLogger("cliquet-test-log")
    monkeypatch.setattr(logger, "handlers", [])
    # Each command sets it up, however often: one line per record, on stderr as it is now.
    log_to_stderr(("cliquet-test-log",), "cliquet: %(message)s")
    log_to_stderr(("cliquet-test-log",), "cliquet: %(message)s")
    logger.info("hello")
    try:
        raise KeyError("x")
    except KeyError:
        logger.exception("Error e-0123456789ab: KeyError: 'x'")
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.startswith("cliquet: hello\ncliquet: Error e-0123456789ab: KeyError: 'x'\n")
    assert "Traceback" in out.err


def cliquet(*args: str, cwd: Path = REPO) -> subprocess.CompletedProcess[str]:
    """Run the CLI with this test's Python, as if already in the app's environment."""
    env = {**os.environ, **GIT_ENV, "CLIQUET_IN_APP_ENV": "1"}
    return subprocess.run(
        [sys.executable, "-m", "cliquet.cli", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    return result.stdout


BLOCK = """\
# /// script
# requires-python = ">=3.14"
# dependencies = ["cliquet-sdk>=0.0.1,<0.1", "beancount"]
#
# [tool.uv.sources]
# cliquet-sdk = { path = "../..", editable = true }
# ///
x = 1
"""


def test_set_sdk_source_points_at_a_local_sdk() -> None:
    out = set_sdk_source(BLOCK, Path("/opt/cliquet"))
    assert '# cliquet-sdk = { path = "/opt/cliquet", editable = true }' in out
    assert '"../.."' not in out
    assert out.endswith("# ///\nx = 1\n")


def test_set_sdk_source_removes_the_source_for_an_installed_sdk() -> None:
    out = set_sdk_source(BLOCK, None)
    assert "tool.uv.sources" not in out
    assert out.startswith('# /// script\n# requires-python = ">=3.14"\n# dependencies')
    assert '"beancount"]\n# ///\nx = 1\n' in out


def test_sdk_pin_allows_patch_releases_only() -> None:
    assert sdk_pin() == "cliquet-sdk>=0.0.1,<0.1"


def test_this_sdk_is_a_local_checkout() -> None:
    sdk = local_sdk()
    assert sdk is not None and (sdk / "src" / "cliquet").is_dir()


def test_new_from_the_template(tmp_path: Path) -> None:
    app = new_app(tmp_path / "Groceries List")
    assert sorted(p.name for p in app.iterdir()) == [
        ".git",
        ".gitignore",
        "app.py",
        "app.py.lock",
        "data",
        "instructions.md",
        "tests",
    ]
    assert 'groceries_list = App("groceries-list"' in (app / "app.py").read_text()
    assert git(app, "log", "--format=%s").splitlines() == ["Create the app"]
    assert git(app, "status", "--porcelain") == ""
    result = cliquet("test", str(app), "-q")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "every invariant refused at least once" in result.stdout


def test_a_new_app_s_name_is_one_an_app_may_have(tmp_path: Path) -> None:
    # A long folder name is cut to the 33 characters that leave room for the built-in tools.
    app = new_app(tmp_path / "my household budget and shopping tracker", lock=False)
    assert 'App("my-household-budget-and-shopping"' in (app / "app.py").read_text()
    result = cliquet("manifest", str(app))
    assert result.returncode == 0, result.stdout + result.stderr
    with pytest.raises(ScaffoldError, match="use at most 33 lower case letters and digits"):
        new_app(tmp_path / "long", name="n" * 34, lock=False)


def test_a_name_that_shadows_a_builtin_still_works(tmp_path: Path) -> None:
    app = new_app(tmp_path / "list", lock=False)
    assert 'app = App("list"' in (app / "app.py").read_text()
    result = cliquet("test", str(app), "-q")
    assert result.returncode == 0, result.stdout + result.stderr


def test_new_copies_an_example(tmp_path: Path) -> None:
    app = new_app(tmp_path / "shop", source=APPS / "stock", lock=False)
    assert (app / "data" / "products.csv").read_text() == (
        APPS / "stock" / "data" / "products.csv"
    ).read_text()
    assert not (app / "app.py.lock").exists()
    source = (app / "app.py").read_text()
    sdk = local_sdk()
    assert f'path = "{sdk}"' in source and '"../.."' not in source
    assert git(app, "log", "--format=%s").splitlines() == ["Create the app from stock"]


def test_new_refuses_a_folder_that_isnt_empty(tmp_path: Path) -> None:
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "notes.txt").write_text("mine")
    with pytest.raises(ScaffoldError, match="isn't empty"):
        new_app(tmp_path / "taken")
    result = cliquet("new", str(tmp_path / "taken"))
    assert result.returncode == 1 and "isn't empty" in result.stderr


def test_manifest_prints_json(tmp_path: Path) -> None:
    result = cliquet("manifest", str(APPS / "stock"))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["name"] == "shop"


def test_check_passes_on_valid_data_even_in_place() -> None:
    result = cliquet("check", str(APPS / "stock"))
    assert result.returncode == 0, result.stderr
    assert "pass every check" in result.stdout


def test_check_reports_data_that_breaks_a_rule(tmp_path: Path) -> None:
    app = new_app(tmp_path / "shop", source=APPS / "stock", lock=False)
    movements = app / "data" / "movements.csv"
    movements.write_text(
        movements.read_text() + "2026-11-08,TEA-SEN,-100,sale,R-9999/TEA-SEN,8.50\n"
    )
    result = cliquet("check", str(app))
    assert result.returncode == 1 and "changes not committed: movements.csv" in result.stderr
    git(app, "commit", "-qam", "edited by hand")
    result = cliquet("check", str(app))
    assert result.returncode == 1
    assert "Refused by: Stock never goes below zero" in result.stderr


def test_check_refuses_an_unknown_timezone(tmp_path: Path) -> None:
    app = new_app(tmp_path / "shop", source=APPS / "stock", lock=False)
    source = (app / "app.py").read_text()
    (app / "app.py").write_text(source.replace('"Europe/Paris"', '"Paris"'))
    result = cliquet("check", str(app))
    assert result.returncode == 1
    assert "timezone='Paris' is not a time zone. Write its IANA name" in result.stderr


def test_check_reports_data_the_server_would_refuse(tmp_path: Path) -> None:
    app = new_app(tmp_path / "shop", source=APPS / "stock", lock=False)
    (app / "data" / "movements.csv").write_bytes(b"x" * (22 * 1024 * 1024))
    git(app, "commit", "-qam", "a large export")

    result = cliquet("check", str(app))

    assert result.returncode == 1
    assert "more than the 21 MB an app keeps" in result.stderr
    assert "The server refuses it: keep less in data/." in result.stderr


def test_check_reports_an_invariant_that_fails(tmp_path: Path) -> None:
    app = new_app(tmp_path / "shop", source=APPS / "stock", lock=False)
    with (app / "app.py").open("a") as source:
        source.write(
            '\n\n@shop.invariant("Never checked")\n'
            "def fails(ctx: InvariantContext) -> list[str]:\n"
            '    raise RuntimeError("it went boom")\n'
        )
    git(app, "commit", "-qam", "an invariant that fails")
    result = cliquet("check", str(app))
    assert result.returncode == 1
    assert "cliquet: the app failed: RuntimeError: it went boom, in the invariant fails" in (
        result.stderr
    )
    assert "Traceback" not in result.stderr


def test_an_app_with_a_dot_file_is_refused(tmp_path: Path) -> None:
    app = new_app(tmp_path / "shop", source=APPS / "stock", lock=False)
    source = (app / "app.py").read_text()
    (app / "app.py").write_text(
        source.replace("files=[PRODUCTS, MOVEMENTS]", 'files=[".git/config"]')
    )
    result = cliquet("check", str(app))
    assert result.returncode == 1
    assert "'.git/config' can't be a data file" in result.stderr


def test_serve_needs_a_role_when_the_app_has_several(tmp_path: Path) -> None:
    app = new_app(tmp_path / "shop", source=APPS / "stock", lock=False)
    result = cliquet("serve", str(app))
    assert result.returncode == 1 and "choose one with --role (owner, staff)" in result.stderr
    result = cliquet("serve", str(app), "--role", "admin")
    assert "'admin' is not one of this app's roles" in result.stderr
    result = cliquet("serve", str(app), "--role", "staff", "--id", "till")
    assert "serving as till (staff) on stdio" in result.stderr


def test_serve_takes_the_only_role_and_the_git_user(tmp_path: Path) -> None:
    app = new_app(tmp_path / "groceries", lock=False)
    result = cliquet("serve", str(app))
    assert "groceries: serving as" in result.stderr
    result = cliquet("serve", str(app), "--role", "owner")
    assert "this app has no roles" in result.stderr


def test_the_sandbox_on_a_socket_needs_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A unix socket has no "127.0.0.1 only" rule to fall back on: it always takes a token.
    monkeypatch.delenv("CLIQUET_SANDBOX_TOKEN", raising=False)
    result = cliquet("sandbox", "--root", str(tmp_path), "--uds", str(tmp_path / "sandbox.sock"))
    assert result.returncode == 1
    assert "set CLIQUET_SANDBOX_TOKEN to listen on" in result.stderr


def test_the_sandbox_on_a_socket_refuses_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIQUET_SANDBOX_TOKEN", "the-token")
    path = tmp_path / "sandbox.sock"
    path.write_text("not a socket")
    result = cliquet("sandbox", "--root", str(tmp_path), "--uds", str(path))
    assert result.returncode == 1
    assert "is not a socket" in result.stderr


def test_a_stale_socket_is_replaced() -> None:
    from cliquet.cli import clear_socket

    # A unix socket's path is short: a test's own folder is too long for one.
    folder = Path(tempfile.mkdtemp(prefix="cliquet-socket-"))
    try:
        path = folder / "sandbox.sock"
        with socket.socket(socket.AF_UNIX) as bound:
            bound.bind(str(path))
            assert clear_socket(str(path)) is None
        assert not path.exists()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_serve_refuses_an_app_inside_another_repository() -> None:
    result = cliquet("serve", str(APPS / "stock"), "--role", "owner")
    assert result.returncode == 1
    assert "the app folder must be its root" in result.stderr
    assert "cliquet new <dir> --from" in result.stderr


def an_app_as_the_server_keeps_it(tmp_path: Path) -> tuple[Path, str]:
    """A copy of the stock example whose `app.py` has no source for the SDK, and its text."""
    app_dir = tmp_path / "shop"
    new_app(app_dir, source=APPS / "stock", lock=False)
    app_py = app_dir / "app.py"
    stored = set_sdk_source(app_py.read_text(), None)
    app_py.write_text(stored)
    return app_dir, stored


def with_a_fake_uv(tmp_path: Path) -> dict[str, str]:
    """An environment whose uv logs each call to `uv.log` and keeps the script it syncs as
    `synced.py`. It finds this test's Python as the app's, and fails any other command.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{tmp_path}/uv.log"\n'
        f'env | grep "^UV_" >> "{tmp_path}/uv.env"\n'
        'case "$1" in\n'
        f'  sync) cp "$4" "{tmp_path}/synced.py" ;;\n'
        f'  python) echo "{sys.executable}" ;;\n'
        "  *) exit 99 ;;\n"
        "esac\n"
    )
    fake_uv.chmod(0o755)
    env = {**os.environ, **GIT_ENV, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    for key in ("CLIQUET_IN_APP_ENV", "CLIQUET_SDK_PATH"):
        env.pop(key, None)
    return env


def run_cliquet(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cliquet.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def test_an_app_as_the_server_keeps_it_builds_with_this_sdk(tmp_path: Path) -> None:
    app_dir, stored = an_app_as_the_server_keeps_it(tmp_path)
    env = with_a_fake_uv(tmp_path)

    checked = run_cliquet(env, "check", str(app_dir))

    assert checked.returncode == 0, checked.stderr
    assert "pass every check" in checked.stdout
    assert (app_dir / "app.py").read_text() == stored
    sync, find = (tmp_path / "uv.log").read_text().splitlines()
    script = Path(sync.removeprefix("sync --quiet --script "))
    assert script.parent.parent == tmp_path / "cache" / "cliquet" / "apps"
    assert find == f"python find --script {script}"
    assert (tmp_path / "synced.py").read_text() == set_sdk_source(stored, local_sdk())

    other = tmp_path / "other-sdk"
    with_other = run_cliquet({**env, "CLIQUET_SDK_PATH": str(other)}, "check", str(app_dir))
    assert with_other.returncode == 0, with_other.stderr
    assert (tmp_path / "synced.py").read_text() == set_sdk_source(stored, other)
    assert (app_dir / "app.py").read_text() == stored


def test_an_app_that_could_take_the_sdk_from_an_index_is_refused_before_uv_runs(
    tmp_path: Path,
) -> None:
    app_dir, stored = an_app_as_the_server_keeps_it(tmp_path)
    app_py = app_dir / "app.py"
    # The real uv could fetch a package called `cliquet-sdk` or `cliquet` from an index.
    env = with_a_fake_uv(tmp_path)
    line = f'# cliquet-sdk = {{ path = "{local_sdk()}", editable = true }}'

    def refused(settings: str) -> subprocess.CompletedProcess[str]:
        # The block's first line is `# /// script`, so the first `# ///` line is its end.
        app_py.write_text(stored.replace("# ///\n", f"#\n{settings}# ///\n", 1))
        result = run_cliquet(env, "test", str(app_dir))
        assert result.returncode == 1
        return result

    from_an_index = refused('# [tool.uv.sources]\n# cliquet-sdk = { index = "pypi" }\n')
    assert "package index" in from_an_index.stderr and line in from_an_index.stderr
    # Any other uv setting gets no folder: `no-sources` would make uv skip it.
    assert line in refused("# [tool.uv]\n# no-sources = true\n").stderr
    app_py.write_text(stored.replace('"cliquet-sdk>=', '"cliquet>='))
    old_name = run_cliquet(env, "test", str(app_dir))
    assert old_name.returncode == 1 and "isn't this SDK" in old_name.stderr
    assert not (tmp_path / "uv.log").exists()


def test_uv_can_t_be_made_to_skip_the_sdk_s_folder(tmp_path: Path) -> None:
    app_dir, stored = an_app_as_the_server_keeps_it(tmp_path)
    app_py = app_dir / "app.py"
    sources = (
        f'# [tool.uv.sources]\n# cliquet-sdk = {{ path = "{local_sdk()}", editable = true }}\n'
    )
    env = with_a_fake_uv(tmp_path)

    def refused(settings: str) -> str:
        # The block's first line is `# /// script`, so the first `# ///` line is its end.
        app_py.write_text(stored.replace("# ///\n", f"#\n{sources}{settings}# ///\n", 1))
        result = run_cliquet(env, "check", str(app_dir))
        assert result.returncode == 1
        return result.stderr

    # With a folder for the SDK, a setting that makes uv skip it is refused all the same.
    assert "sets no-sources" in refused("# [tool.uv]\n# no-sources = true\n")
    assert "sets index-url" in refused('# [tool.uv]\n# index-url = "https://example.com/simple"\n')
    app_py.write_text(stored.replace("# ///\n", f"#\n{sources}# ///\n", 1))
    (app_dir / "app.py.lock").write_text(
        'version = 1\n\n[[package]]\nname = "cliquet-sdk"\nversion = "0.0.1"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    from_the_lock = run_cliquet(env, "check", str(app_dir))
    assert from_the_lock.returncode == 1 and "app.py.lock" in from_the_lock.stderr
    assert not (tmp_path / "uv.log").exists()

    # Nor can the person's environment, or a configuration file uv would find.
    (app_dir / "app.py.lock").unlink()
    skipping = {"UV_NO_SOURCES": "1", "UV_NO_SOURCES_PACKAGE": "cliquet-sdk"}
    config = tmp_path / "uv.toml"
    config.write_text("no-sources = true\n")
    checked = run_cliquet({**env, **skipping, "UV_CONFIG_FILE": str(config)}, "check", str(app_dir))
    assert checked.returncode == 0, checked.stderr
    seen = (tmp_path / "uv.env").read_text().splitlines()
    assert len((tmp_path / "uv.log").read_text().splitlines()) == 2
    assert seen.count("UV_NO_CONFIG=1") == 2
    assert not [line for line in seen if line.startswith(("UV_NO_SOURCES", "UV_CONFIG_FILE"))]


READS_BACK = """

from pydantic import computed_field


class Count(BaseModel):
    n: int

    @computed_field
    @property
    def twice(self) -> int:
        return 2 * self.n


@shop.query()
def counted(ctx: QueryContext) -> Count:
    \"\"\"A count.\"\"\"
    return Count(n=1)
"""


@pytest.mark.parametrize(
    ("app_py", "test", "found"),
    [
        ("\n\nimport uuid\n\nID = uuid.uuid4()\n", None, "uuid.uuid4 is random"),
        ("\n\nimport subprocess\n", None, "import subprocess isn't allowed"),
        ("\n\nimport sys\n\nsys.path.append('lib')\n", None, "changes sys.path"),
        (
            "",
            "def test_first() -> None:\n    open('data/products.csv')\n",
            "tests/test_first.py:2: 'data/products.csv' names the app's data/ folder",
        ),
        (READS_BACK, None, "Count.twice is a computed_field"),
    ],
    ids=["randomness", "subprocess", "sys.path", "tests reading data", "result"],
)
def test_check_and_test_refuse_what_the_guides_rule_out(
    tmp_path: Path, app_py: str, test: str | None, found: str
) -> None:
    app = new_app(tmp_path / "shop", source=APPS / "stock", lock=False)
    with (app / "app.py").open("a") as source:
        source.write(app_py)
    if test is not None:
        (app / "tests" / "test_first.py").write_text(test)
    for command in ("check", "test"):
        result = cliquet(command, str(app))
        assert result.returncode == 1, result.stdout
        assert "the app's definition has problems" in result.stderr
        assert found in result.stderr
