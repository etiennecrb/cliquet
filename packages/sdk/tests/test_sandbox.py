import base64
import gzip
import hashlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import APPS, HEADER, put_version, wheels_for
from starlette.middleware.gzip import GZipMiddleware
from starlette.testclient import TestClient
from test_screenshot import dark

from cliquet import __version__, sandbox, scaffold, screenshot
from cliquet.sandbox import WorkerPool, bundle_of, create_sandbox, version_id
from cliquet.wheels import sandbox_platform

pytestmark = [pytest.mark.sandbox, pytest.mark.slow]

REPO = Path(__file__).parents[3]
NOW = datetime(2026, 11, 10, 18, 30, tzinfo=ZoneInfo("Europe/Paris")).isoformat()
SAM = {"id": "sam", "role": "owner"}

SLOW_APP = (
    HEADER
    + """\
\"\"\"An app that takes its time.\"\"\"

import time

from cliquet import App, QueryContext

slow = App("slow", files=["log.txt"])


@slow.query()
def wait(ctx: QueryContext) -> str:
    \"\"\"Wait a while.\"\"\"
    time.sleep(5)
    return "done"
"""
)


def encoded(files: dict[str, bytes]) -> dict[str, str]:
    return {name: base64.b64encode(content).decode() for name, content in files.items()}


def stock_files() -> dict[str, bytes]:
    return bundle_of(APPS / "stock")


def stock_data() -> dict[str, bytes]:
    return {
        name: (APPS / "stock" / "data" / name).read_bytes()
        for name in ("products.csv", "movements.csv")
    }


def put(client: TestClient, files: dict[str, bytes], version: str | None = None) -> Any:
    return put_version(client, "sam", "stock", files, version)


def run(client: TestClient, version: str, body: dict[str, Any]) -> Any:
    return client.post(f"/apps/sam/stock/versions/{version}/run", json=body)


def suite(client: TestClient, version: str, body: dict[str, Any]) -> Any:
    return client.post(f"/apps/sam/stock/versions/{version}/test", json=body)


def pictures(client: TestClient, version: str, body: dict[str, Any]) -> Any:
    return client.post(f"/apps/sam/stock/versions/{version}/pictures", json=body)


def script(client: TestClient, version: str, body: dict[str, Any]) -> Any:
    return client.post(f"/apps/sam/stock/versions/{version}/script", json=body)


needs_browser = pytest.mark.skipif(
    screenshot.find_browser() is None, reason="needs a Chrome, Chromium or Edge"
)


def test_version_id_follows_the_code() -> None:
    files = stock_files()
    first = version_id(files)
    assert version_id(dict(files)) == first
    assert version_id({**files, "app.py": files["app.py"] + b"\n# more\n"}) != first
    assert version_id({**files, "instructions.md": files["instructions.md"] + b"\n"}) != first
    test = next(name for name in files if name.startswith("tests/"))
    assert version_id({**files, test: files[test] + b"\n# more\n"}) != first
    assert version_id({**files, "app.py.lock": b"other"}) == first
    assert version_id({**files, "migrate.py": b"def migrate(): pass\n"}) == first


def test_bundle_of_leaves_data_out() -> None:
    files = stock_files()
    assert {"app.py", "instructions.md", "tests/test_stock.py"} <= set(files)
    assert not any(name.startswith("data/") for name in files)


def test_health(tmp_path: Path) -> None:
    with TestClient(create_sandbox(tmp_path / "root")) as client:
        python, platform = sandbox_platform()
        assert client.get("/health").json() == {
            "ok": True,
            "sdk": __version__,
            "python": python,
            "platform": platform,
            "worker": False,
        }


def test_putting_a_version_builds_it_once(tmp_path: Path) -> None:
    root = tmp_path / "root"
    files = stock_files()
    version = version_id(files)
    with TestClient(create_sandbox(root)) as client:
        first = put(client, files)
        assert first.status_code == 200
        body = first.json()
        assert body["manifest"]["name"] == "shop"
        assert body["lock"]
        written = (root / "apps" / "sam" / "stock" / version / "app.py").read_text()
        assert "[tool.uv.sources]" in written
        again = put(client, files)
        assert again.status_code == 200
        assert again.json()["manifest"] == body["manifest"]


def test_the_sdk_s_code_hash_moves_with_its_code_only(tmp_path: Path) -> None:
    sdk = REPO / "packages" / "sdk"
    files = scaffold.sdk_files(sdk)
    hashes = {}
    for changed in (
        None,
        "src/cliquet/renderer/cliquet.css",
        "README.md",
        "src/cliquet/app.py",
        "pyproject.toml",
    ):
        copy = tmp_path / str(len(hashes))
        for name, content in files.items():
            (copy / name).parent.mkdir(parents=True, exist_ok=True)
            (copy / name).write_bytes(content + b"\n" if name == changed else content)
        hashes[changed] = scaffold.sdk_code_hash(copy)
    assert hashes[None] == scaffold.sdk_code_hash(sdk)
    assert hashes["src/cliquet/renderer/cliquet.css"] == hashes[None]
    assert hashes["README.md"] == hashes[None]
    assert hashes["src/cliquet/app.py"] != hashes[None]
    assert hashes["pyproject.toml"] not in (hashes[None], hashes["src/cliquet/app.py"])


def test_a_version_built_with_another_sdk_is_built_again(tmp_path: Path) -> None:
    root = tmp_path / "root"
    sdk = tmp_path / "sdk"
    for name, content in scaffold.sdk_files(REPO / "packages" / "sdk").items():
        (sdk / name).parent.mkdir(parents=True, exist_ok=True)
        (sdk / name).write_bytes(content)
    files = stock_files()
    version = version_id(files)
    with TestClient(create_sandbox(root, sdk_path=sdk)) as client:
        assert put(client, files).status_code == 200
        assert run(client, version, {"op": "manifest"}).status_code == 200
    # A change to the renderer's files alone keeps every version: none of them is code.
    css = sdk / "src" / "cliquet" / "renderer" / "cliquet.css"
    css.write_text(css.read_text() + "\n/* A new look. */\n")
    with TestClient(create_sandbox(root, sdk_path=sdk)) as client:
        assert run(client, version, {"op": "manifest"}).status_code == 200
    # A deploy changes the SDK in place: its dependencies, or what it reads of an app.
    init = sdk / "src" / "cliquet" / "__init__.py"
    init.write_text(init.read_text() + "\n# A new SDK.\n")
    with TestClient(create_sandbox(root, sdk_path=sdk)) as client:
        assert run(client, version, {"op": "manifest"}).json() == {"error": "unknown version"}
        assert put(client, files).status_code == 200
        assert run(client, version, {"op": "manifest"}).status_code == 200


def test_a_wrong_version_or_path_is_refused(tmp_path: Path) -> None:
    files = stock_files()
    actual = version_id(files)
    with TestClient(create_sandbox(tmp_path / "root")) as client:
        wrong = put(client, files, version="0" * 16)
        assert wrong.status_code == 422
        assert actual in wrong.json()["error"] and "0" * 16 in wrong.json()["error"]
        extra = {**files, "../evil.py": b"x"}
        bad = put(client, extra, version=actual)
        assert bad.status_code == 422
        assert "../evil.py" in bad.json()["error"]


def test_the_sandbox_reads_gzipped_json_and_gzips_its_answers(tmp_path: Path) -> None:
    files = stock_files()
    body = {"files": {name: base64.b64encode(data).decode() for name, data in files.items()}}
    gzipped = {"content-type": "application/json", "content-encoding": "gzip"}
    path = f"/apps/sam/stock/versions/{'0' * 16}"
    with TestClient(create_sandbox(tmp_path / "root")) as client:
        # The body is read: the sandbox says the version is wrong, not that the body isn't JSON.
        wrong = client.put(path, content=gzip.compress(json.dumps(body).encode()), headers=gzipped)
        assert wrong.status_code == 422
        assert version_id(files) in wrong.json()["error"]
        broken = client.put(path, content=b"not gzip", headers=gzipped)
        assert broken.json() == {"error": "not JSON"}
    # Its answers go gzipped, by the middleware the server uses too.
    sandbox = create_sandbox(tmp_path / "other")
    assert any(layer.cls is GZipMiddleware for layer in sandbox.user_middleware)


def test_the_sandbox_refuses_a_body_over_its_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "_MAX_REQUEST", 1000)
    gzipped = {"content-type": "application/json", "content-encoding": "gzip"}
    path = f"/apps/sam/stock/versions/{'0' * 16}"
    with TestClient(create_sandbox(tmp_path / "root")) as client:
        sent = client.put(path, content=b"{" + b" " * 2000 + b"}")
        assert sent.status_code == 413
        unpacked = gzip.compress(b"{" + b" " * 2000 + b"}")
        assert len(unpacked) < 1000
        assert client.put(path, content=unpacked, headers=gzipped).status_code == 413


def test_an_app_with_problems_is_not_ready(tmp_path: Path) -> None:
    source = (APPS / "stock" / "app.py").read_text()
    without = source.replace(
        '"""Stock for a small tea shop: products, deliveries and sales, kept by an agent."""\n',
        "",
    )
    assert without != source
    files = {**stock_files(), "app.py": without.encode()}
    version = version_id(files)
    with TestClient(create_sandbox(tmp_path / "root")) as client:
        answer = put(client, files)
        assert answer.status_code == 422
        assert any("docstring" in problem for problem in answer.json()["problems"])
        unknown = run(client, version, {"op": "manifest"})
        assert unknown.status_code == 404


def no_uv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Make the sandbox's calls to uv fail the test: uv could fetch PyPI's `cliquet`."""
    calls: list[list[str]] = []
    real = subprocess.run

    def run(args: Any, *rest: Any, **kwargs: Any) -> Any:
        if list(args)[:1] == ["uv"]:
            calls.append(list(args))
            raise AssertionError(f"uv was called: {args}")
        return real(args, *rest, **kwargs)

    monkeypatch.setattr(sandbox.subprocess, "run", run)
    return calls


def test_an_app_that_depends_on_cliquet_is_refused_before_uv_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = (APPS / "stock" / "app.py").read_text()
    files = {**stock_files(), "app.py": source.replace('"cliquet-sdk>=', '"cliquet>=').encode()}
    calls = no_uv(monkeypatch)
    with TestClient(create_sandbox(tmp_path / "root")) as client:
        answer = client.put(
            f"/apps/sam/stock/versions/{version_id(files)}",
            json={"files": encoded(files), "wheels": []},
        )
    assert answer.status_code == 422
    assert "isn't this SDK" in answer.json()["error"]
    assert calls == []


def test_a_sandbox_without_an_sdk_folder_builds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLIQUET_SDK_PATH", raising=False)
    monkeypatch.setattr(sandbox.scaffold, "local_sdk", lambda: None)
    calls = no_uv(monkeypatch)
    files = stock_files()
    with TestClient(create_sandbox(tmp_path / "root")) as client:
        answer = client.put(
            f"/apps/sam/stock/versions/{version_id(files)}",
            json={"files": encoded(files), "wheels": []},
        )
    assert answer.status_code == 500
    assert "CLIQUET_SDK_PATH" in answer.json()["error"]
    assert calls == []


def test_run_goes_to_a_warm_worker(tmp_path: Path) -> None:
    root = tmp_path / "root"
    files = stock_files()
    version = version_id(files)
    sandbox = create_sandbox(root)
    data = stock_data()
    body = {
        "op": "query",
        "caller": SAM,
        "now": NOW,
        "name": "stock",
        "arguments": {},
        "files": encoded(data),
    }
    with TestClient(sandbox) as client:
        assert put(client, files).status_code == 200
        first = run(client, version, body)
        assert first.status_code == 200
        assert len(first.json()["result"]) == 5
        # The build asked the pool for the manifest, and the run went to its worker.
        assert client.get("/health").json()["worker"] is True
        pool = sandbox.state.pool
        assert len(pool._workers) == 1
        pid = next(iter(pool._workers.values())).process.pid
        second = run(client, version, body)
        assert second.status_code == 200
        assert len(pool._workers) == 1
        assert next(iter(pool._workers.values())).process.pid == pid


def test_run_on_an_unknown_version(tmp_path: Path) -> None:
    version = "a" * 16
    with TestClient(create_sandbox(tmp_path / "root")) as client:
        answer = run(client, version, {"op": "manifest"})
        assert answer.status_code == 404
        assert answer.json() == {"error": "unknown version"}


def test_a_call_that_takes_too_long_is_stopped(tmp_path: Path) -> None:
    folder = tmp_path / "slow"
    folder.mkdir()
    (folder / "app.py").write_text(SLOW_APP)
    pool = WorkerPool(timeout=1)
    body = {
        "op": "query",
        "caller": SAM,
        "now": NOW,
        "name": "wait",
        "arguments": {},
        "files": {},
    }
    started = time.monotonic()
    answer = pool.request(folder, body, python=sys.executable)
    assert time.monotonic() - started < 3
    assert answer["error"]["kind"] == "failed"
    assert "more than 1" in answer["error"]["message"]
    assert len(pool._workers) == 0
    second = pool.request(folder, {"op": "manifest"}, python=sys.executable)
    assert second["result"]["name"] == "slow"
    pool.close()


def test_a_worker_on_an_old_build_finishes_its_call(tmp_path: Path) -> None:
    folder = tmp_path / "slow"
    folder.mkdir()
    (folder / "app.py").write_text(SLOW_APP)
    pool = WorkerPool(timeout=20)
    body = {"op": "query", "caller": SAM, "now": NOW, "name": "wait", "arguments": {}}
    answers: list[dict[str, Any]] = []
    call = threading.Thread(
        target=lambda: answers.append(pool.request(folder, {**body, "files": {}}, sys.executable))
    )
    call.start()
    deadline = time.monotonic() + 10
    while not (folder.resolve() in pool._workers and pool._workers[folder.resolve()].lock.locked()):
        assert time.monotonic() < deadline, "the slow call never started"
    old = pool._workers[folder.resolve()]
    # The same Python by another path stands for a version's new environment.
    other = os.path.join(os.path.dirname(sys.executable), ".", os.path.basename(sys.executable))
    built = pool.request(folder, {"op": "manifest"}, python=other)
    assert built["result"]["name"] == "slow"
    assert pool._workers[folder.resolve()].python == other
    call.join(timeout=20)
    assert [answer.get("result") for answer in answers] == ["done"], answers
    old.process.wait(timeout=10)  # Stopped once its call was done.
    pool.close()


def test_the_token_is_required(tmp_path: Path) -> None:
    with TestClient(create_sandbox(tmp_path / "root", token="secret")) as client:
        missing = client.get("/health")
        assert missing.status_code == 401
        assert missing.json() == {"error": "wrong or missing token"}
        allowed = client.get("/health", headers={"X-Sandbox-Token": "secret"})
        assert allowed.status_code == 200


def test_deleting_an_app(tmp_path: Path) -> None:
    root = tmp_path / "root"
    files = stock_files()
    version = version_id(files)
    with TestClient(create_sandbox(root)) as client:
        assert put(client, files).status_code == 200
        assert (root / "apps" / "sam" / "stock").is_dir()
        deleted = client.delete("/apps/sam/stock")
        assert deleted.status_code == 200
        assert deleted.json() == {}
        assert not (root / "apps" / "sam" / "stock").exists()
        assert run(client, version, {"op": "manifest"}).status_code == 404


def test_the_command_refuses_to_listen_wide_open(tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if key != "CLIQUET_SANDBOX_TOKEN"}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cliquet.cli",
            "sandbox",
            "--host",
            "0.0.0.0",
            "--root",
            str(tmp_path / "root"),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 1
    assert "CLIQUET_SANDBOX_TOKEN" in result.stderr


def test_the_tests_of_a_version_run(tmp_path: Path) -> None:
    files = stock_files()
    version = version_id(files)
    with TestClient(create_sandbox(tmp_path / "root")) as client:
        assert put(client, files).status_code == 200
        answer = suite(client, version, {"only": None})
    assert answer.status_code == 200
    body = answer.json()
    assert body["passed"] is True
    assert "passed" in body["output"]


def test_a_failing_test_is_reported(tmp_path: Path) -> None:
    files = {
        **stock_files(),
        "tests/test_wrong.py": b"def test_wrong() -> None:\n    assert 1 == 2\n",
    }
    version = version_id(files)
    with TestClient(create_sandbox(tmp_path / "root")) as client:
        assert put(client, files).status_code == 200
        answer = suite(client, version, {"only": None})
    assert answer.status_code == 200
    body = answer.json()
    assert body["passed"] is False
    assert "test_wrong" in body["output"]


def test_only_runs_some_tests(tmp_path: Path) -> None:
    files = {
        **stock_files(),
        "tests/test_wrong.py": b"def test_wrong() -> None:\n    assert 1 == 2\n",
    }
    version = version_id(files)
    with TestClient(create_sandbox(tmp_path / "root")) as client:
        assert put(client, files).status_code == 200
        answer = suite(client, version, {"only": "test_wrong"})
    assert answer.status_code == 200
    body = answer.json()
    assert body["passed"] is False
    assert "1 failed" in body["output"]


def test_tests_of_an_unknown_version(tmp_path: Path) -> None:
    with TestClient(create_sandbox(tmp_path / "root")) as client:
        answer = suite(client, "a" * 16, {"only": None})
    assert answer.status_code == 404
    assert answer.json() == {"error": "unknown version"}


@pytest.mark.browser
@needs_browser
def test_pictures_of_a_version(tmp_path: Path) -> None:
    root = tmp_path / "root"
    files = stock_files()
    version = version_id(files)
    data = stock_data()
    with TestClient(create_sandbox(root)) as client:
        assert put(client, files).status_code == 200
        answer = pictures(client, version, {"role": "staff", "files": encoded(data), "views": None})
    assert answer.status_code == 200
    found = answer.json()["pictures"]
    assert [picture["view"] for picture in found] == [
        "till",
        "till",
        "shelves",
        "shelves",
        "delivery",
        "delivery",
    ]
    assert [picture["width"] for picture in found] == [768, 390] * 3
    assert all(base64.b64decode(picture["png"])[:8] == b"\x89PNG\r\n\x1a\n" for picture in found)
    # How tall each screen is: the picture's own height, as none is cut.
    for picture in found:
        png = base64.b64decode(picture["png"])
        assert picture["height"] == int.from_bytes(png[20:24])
    assert list((root / "tmp").iterdir()) == []


@pytest.mark.browser
@needs_browser
def test_pictures_are_taken_with_the_sandbox_s_browser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = screenshot.find_browser()
    assert real is not None
    # A browser outside the sandbox's PATH, named only in the sandbox's own environment.
    browser, ran = tmp_path / "elsewhere" / "browser", tmp_path / "ran.txt"
    browser.parent.mkdir()
    browser.write_text(
        f'#!/bin/sh\necho ran >> {shlex.quote(str(ran))}\nexec {shlex.quote(real)} "$@"\n'
    )
    browser.chmod(0o755)
    monkeypatch.setenv(screenshot.BROWSER_VARIABLE, str(browser))
    root = tmp_path / "root"
    files = stock_files()
    version = version_id(files)
    data = stock_data()
    with TestClient(create_sandbox(root)) as client:
        assert put(client, files).status_code == 200
        answer = pictures(
            client, version, {"role": "staff", "files": encoded(data), "views": ["till"]}
        )
    assert answer.status_code == 200, answer.json()
    assert [picture["view"] for picture in answer.json()["pictures"]] == ["till", "till"]
    assert ran.read_text() == "ran\n"


def test_a_missing_browser_in_the_sandbox_s_environment_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(screenshot.BROWSER_VARIABLE, str(tmp_path / "missing"))
    root = tmp_path / "root"
    files = stock_files()
    version = version_id(files)
    with TestClient(create_sandbox(root)) as client:
        assert put(client, files).status_code == 200
        answer = pictures(client, version, {"role": "staff", "files": {}, "views": None})
    assert answer.status_code == 422
    assert "CLIQUET_CHROME is set to" in answer.json()["error"]


def test_pictures_say_why_they_failed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    files = stock_files()
    version = version_id(files)
    with TestClient(create_sandbox(root)) as client:
        assert put(client, files).status_code == 200
        answer = pictures(
            client,
            version,
            {"role": "nobody", "files": {}, "views": None},
        )
    assert answer.status_code == 422
    assert "owner" in answer.json()["error"]
    assert "staff" in answer.json()["error"]


@pytest.mark.browser
@needs_browser
def test_a_script_on_a_version(tmp_path: Path) -> None:
    root = tmp_path / "root"
    files = stock_files()
    version = version_id(files)
    data = stock_data()
    with TestClient(create_sandbox(root)) as client:
        assert put(client, files).status_code == 200
        answer = script(
            client,
            version,
            {
                "role": "staff",
                "files": encoded(data),
                "view": "till",
                "width": 768,
                "steps": [
                    {"fill": "Receipt", "with": "R-0900"},
                    {"choose": "Product", "option": "Sencha green tea 100 g"},
                    {"click": "Add to receipt"},
                    {"click": "Record receipt"},
                ],
            },
        )
    assert answer.status_code == 200
    body = answer.json()
    assert base64.b64decode(body["png"])[:8] == b"\x89PNG\r\n\x1a\n"
    # The till is laid out again when it opens, after "Add to receipt", and after the sale.
    assert [call["tool"] for call in body["calls"]] == ["till", "till", "record_receipt", "till"]
    assert body["error"] is None
    # The calls, with their arguments, are what the server makes again on its preview.
    assert body["calls"][2]["arguments"]["receipt"] == "R-0900"
    assert sorted(body) == ["calls", "error", "height", "png"]
    # How tall the till's screen is: the picture's own height, as it isn't cut.
    assert body["height"] == int.from_bytes(base64.b64decode(body["png"])[20:24])
    assert list((root / "tmp").iterdir()) == []


@pytest.mark.browser
@needs_browser
def test_pictures_and_scripts_in_the_dark_theme(tmp_path: Path) -> None:
    root = tmp_path / "root"
    files = stock_files()
    version = version_id(files)
    data = encoded(stock_data())
    with TestClient(create_sandbox(root)) as client:
        assert put(client, files).status_code == 200
        shots = pictures(
            client, version, {"role": "staff", "files": data, "views": ["till"], "theme": "dark"}
        )
        tried = script(
            client,
            version,
            {"role": "staff", "files": data, "view": "till", "steps": [], "theme": "dark"},
        )
    assert shots.status_code == 200, shots.json()
    # One theme for the call: each picture keeps its view and width, and its screen is dark.
    wide, narrow = shots.json()["pictures"]
    assert (wide["view"], wide["width"], narrow["width"]) == ("till", 768, 390)
    assert dark(base64.b64decode(wide["png"]))
    assert tried.status_code == 200, tried.json()
    assert dark(base64.b64decode(tried.json()["png"]))


def test_a_theme_is_light_or_dark(tmp_path: Path) -> None:
    root = tmp_path / "root"
    files = stock_files()
    version = version_id(files)
    with TestClient(create_sandbox(root)) as client:
        assert put(client, files).status_code == 200
        body = {"role": "staff", "files": {}, "theme": "blue"}
        shots = pictures(client, version, {**body, "views": None})
        tried = script(client, version, {**body, "view": "till", "steps": []})
    for answer in (shots, tried):
        assert answer.status_code == 422
        assert answer.json() == {"error": 'theme must be "light" or "dark"'}


def test_a_script_that_can_t_start_is_a_422(tmp_path: Path) -> None:
    root = tmp_path / "root"
    files = stock_files()
    version = version_id(files)
    with TestClient(create_sandbox(root)) as client:
        assert put(client, files).status_code == 200
        answer = script(
            client,
            version,
            {"role": "staff", "files": {}, "view": "nope", "steps": []},
        )
    assert answer.status_code == 422
    assert "shelves" in answer.json()["error"]


def test_a_version_says_which_wheels_it_lacks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    files = stock_files()
    version = version_id(files)
    with TestClient(create_sandbox(root)) as client:
        _, locked = wheels_for(client, files)
        answer = client.put(
            f"/apps/sam/stock/versions/{version}",
            json={
                "files": encoded({**files, "app.py.lock": locked.lock}),
                "wheels": [{"name": w.name, "sha256": w.sha256} for w in locked.wheels],
            },
        )
        assert answer.status_code == 409
        assert sorted(answer.json()["missing"]) == sorted(w.name for w in locked.wheels)
        assert run(client, version, {"op": "manifest"}).status_code == 404
    assert not (root / "apps" / "sam" / "stock" / version / ".ready.json").exists()


def test_a_wheel_with_a_wrong_hash_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    name = "tiny-1.0-py3-none-any.whl"
    with TestClient(create_sandbox(root)) as client:
        wrong = client.put(f"/wheels/{name}", params={"sha256": "0" * 64}, content=b"not it")
        assert wrong.status_code == 422
        assert "hash" in wrong.json()["error"]
        for bad in ("tiny.txt", ".whl", "a b.whl"):
            refused = client.put(f"/wheels/{bad}", params={"sha256": "0" * 64}, content=b"x")
            assert refused.status_code == 422, bad
        no_hash = client.put(f"/wheels/{name}", content=b"x")
        assert no_hash.status_code == 422
    assert not (root / "wheels" / name).exists()


def test_a_version_builds_without_uv_reaching_an_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = stock_files()
    version = version_id(files)
    with TestClient(create_sandbox(tmp_path / "root")) as client:
        wheels_for(client, files)  # the server's side, which may reach PyPI
        calls: list[list[str]] = []
        real = subprocess.run

        def offline_only(args: Any, *rest: Any, **kwargs: Any) -> Any:
            command = [str(part) for part in args]
            if command[:1] == ["uv"]:
                calls.append(command)
                if "--offline" not in command or command[1] in ("lock", "sync"):
                    raise AssertionError(f"uv may reach an index: {command}")
            return real(args, *rest, **kwargs)

        monkeypatch.setattr(sandbox.subprocess, "run", offline_only)
        answer = put(client, files)
        assert answer.status_code == 200, answer.text
        assert answer.json()["manifest"]["name"] == "shop"
        assert calls
        manifest = run(client, version, {"op": "manifest"})
        assert manifest.json()["result"]["name"] == "shop"


def test_a_wheel_removed_from_disk_is_asked_for_again(tmp_path: Path) -> None:
    root = tmp_path / "root"
    files = stock_files()
    other = {**files, "instructions.md": files["instructions.md"] + b"\nMore.\n"}
    with TestClient(create_sandbox(root)) as client:
        assert put(client, files).status_code == 200
        for wheel in (root / "wheels").glob("*.whl"):
            wheel.unlink()
        _, locked = wheels_for(client, other)
        answer = client.put(
            f"/apps/sam/stock/versions/{version_id(other)}",
            json={
                "files": encoded({**other, "app.py.lock": locked.lock}),
                "wheels": [{"name": w.name, "sha256": w.sha256} for w in locked.wheels],
            },
        )
    assert answer.status_code == 409
    assert sorted(answer.json()["missing"]) == sorted(w.name for w in locked.wheels)


def tiny_wheel(folder: Path) -> tuple[str, bytes]:
    """A made-up wheel with one module, `tiny`, built here: no index is reached."""
    name = "tiny-1.0-py3-none-any.whl"
    info = "tiny-1.0.dist-info"
    files = {
        "tiny.py": b"WHO = 'the second lock'\n",
        f"{info}/METADATA": b"Metadata-Version: 2.1\nName: tiny\nVersion: 1.0\n",
        f"{info}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    records = [
        f"{path},sha256={record_hash(content)},{len(content)}" for path, content in files.items()
    ]
    files[f"{info}/RECORD"] = ("\n".join([*records, f"{info}/RECORD,,"]) + "\n").encode()
    path = folder / name
    with zipfile.ZipFile(path, "w") as archive:
        for member, content in files.items():
            archive.writestr(member, content)
    return name, path.read_bytes()


def record_hash(content: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()


def counted_builds(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """The folders the sandbox makes an environment in, one per build."""
    builds: list[Path] = []
    real = sandbox._environment

    def counted(where: Path, *rest: Any) -> Any:
        builds.append(where)
        return real(where, *rest)

    monkeypatch.setattr(sandbox, "_environment", counted)
    return builds


def test_a_new_lock_builds_the_same_code_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    files = stock_files()
    version = version_id(files)
    folder = root / "apps" / "sam" / "stock" / version
    builds = counted_builds(monkeypatch)
    with TestClient(create_sandbox(root)) as client:
        assert put(client, files).status_code == 200
        assert len(builds) == 1
        assert run(client, version, {"op": "manifest"}).status_code == 200
        # The same code, locked again: here with one more library.
        _, locked = wheels_for(client, files)
        name, content = tiny_wheel(tmp_path)
        digest = hashlib.sha256(content).hexdigest()
        sent = client.put(f"/wheels/{name}", params={"sha256": digest}, content=content)
        assert sent.status_code == 200, sent.text
        relock = locked.lock + b"\n# Locked again, with tiny.\n"
        body = {
            "files": encoded({**files, "app.py.lock": relock}),
            "wheels": [
                *({"name": w.name, "sha256": w.sha256} for w in locked.wheels),
                {"name": name, "sha256": digest},
            ],
        }
        second = client.put(f"/apps/sam/stock/versions/{version}", json=body)
        assert second.status_code == 200, second.text
        assert base64.b64decode(second.json()["lock"]) == relock
        assert len(builds) == 2
        python = json.loads((folder / ".ready.json").read_text())["python"]
        found = subprocess.run(
            [python, "-c", "import tiny; print(tiny.WHO)"], capture_output=True, text=True
        )
        assert found.stdout.strip() == "the second lock", found.stderr
        assert run(client, version, {"op": "manifest"}).status_code == 200
        # The same lock again reuses the build, as does a put that lists no wheels.
        third = client.put(f"/apps/sam/stock/versions/{version}", json=body)
        assert third.status_code == 200, third.text
        unlisted = client.put(f"/apps/sam/stock/versions/{version}", json={"files": body["files"]})
        assert unlisted.status_code == 200, unlisted.text
        assert len(builds) == 2
        # Without a lock, the old one is not answered as this build's.
        body["files"] = encoded({n: c for n, c in files.items() if n != "app.py.lock"})
        fourth = client.put(f"/apps/sam/stock/versions/{version}", json=body)
        assert fourth.status_code == 200, fourth.text
        assert fourth.json()["lock"] == ""
        assert len(builds) == 3


def test_each_put_leaves_the_migration_it_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    files = stock_files()
    migration = root / "apps" / "sam" / "stock" / version_id(files) / "migrate.py"
    builds = counted_builds(monkeypatch)
    with TestClient(create_sandbox(root)) as client:
        assert put(client, files).status_code == 200
        script = b"def migrate() -> None:\n    pass\n"
        assert put(client, {**files, "migrate.py": script}).status_code == 200
        assert migration.read_bytes() == script
        assert put(client, files).status_code == 200
        assert not migration.exists()
    assert len(builds) == 1


def test_a_new_lock_that_fails_to_build_leaves_the_old_build(tmp_path: Path) -> None:
    root = tmp_path / "root"
    files = stock_files()
    version = version_id(files)
    with TestClient(create_sandbox(root)) as client:
        first = put(client, files)
        assert first.status_code == 200
        assert run(client, version, {"op": "manifest"}).status_code == 200
        # A wheel that matches its hash but can't be installed.
        name, content = "broken-1.0-py3-none-any.whl", b"not a wheel"
        digest = hashlib.sha256(content).hexdigest()
        sent = client.put(f"/wheels/{name}", params={"sha256": digest}, content=content)
        assert sent.status_code == 200, sent.text
        _, locked = wheels_for(client, files)
        body = {
            "files": encoded({**files, "app.py.lock": locked.lock + b"\n# Broken.\n"}),
            "wheels": [
                *({"name": w.name, "sha256": w.sha256} for w in locked.wheels),
                {"name": name, "sha256": digest},
            ],
        }
        failed = client.put(f"/apps/sam/stock/versions/{version}", json=body)
        assert failed.status_code == 422, failed.text
        assert run(client, version, {"op": "manifest"}).json()["result"]["name"] == "shop"
        again = put(client, files)
        assert again.status_code == 200
        assert again.json()["lock"] == first.json()["lock"]
