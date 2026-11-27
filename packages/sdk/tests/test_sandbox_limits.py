"""The limits the sandbox puts on each process that runs app code."""

import base64
import os
import resource
import shutil
import subprocess
import sys
import threading
import time
import tracemalloc
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import APPS, HEADER, put_version
from starlette.testclient import TestClient

from cliquet import sandbox, screenshot
from cliquet.limits import MAX_DATA
from cliquet.sandbox import WorkerPool, bundle_of, create_sandbox, version_id

pytestmark = [pytest.mark.sandbox, pytest.mark.slow]

needs_memory_limit = pytest.mark.skipif(
    not sandbox.CAN_LIMIT_MEMORY, reason="this platform can't lower a process's memory"
)
"""macOS refuses every memory limit: `setrlimit` raises ValueError for each of them."""

needs_prlimit = pytest.mark.skipif(
    not hasattr(resource, "prlimit"), reason="needs prlimit, which macOS has not"
)
"""A CPU limit is set at the worker's start, and raised before each call, through prlimit."""

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=ZoneInfo("Europe/Paris")).isoformat()
SAM = {"id": "sam", "role": None}

LIMITS_APP = (
    HEADER
    + '''\
"""An app that tries every limit of the sandbox."""

import importlib
import os
import sys
from pathlib import Path

from cliquet import ActionContext, App, QueryContext

# Checking the app refuses `import subprocess`, but hostile code hides it: the sandbox's
# limits hold all the same.
subprocess = importlib.import_module("subprocess")

limits = App("limits", files=["notes.txt"])


@limits.query()
def environment(ctx: QueryContext) -> list[str]:
    """The names in the worker's environment."""
    return sorted(os.environ)


@limits.query()
def eat(ctx: QueryContext, megabytes: int) -> int:
    """Hold this much memory."""
    held = bytearray(megabytes * 1024 * 1024)
    return len(held)


@limits.query()
def spin(ctx: QueryContext) -> int:
    """Loop on the CPU, for ever."""
    count = 0
    while True:
        count += 1


@limits.query()
def child(ctx: QueryContext) -> int:
    """Start a child process that outlives the call, and return its id."""
    return subprocess.Popen(["sleep", "300"]).pid


@limits.query()
def large(ctx: QueryContext, megabytes: int) -> str:
    """Answer with this many megabytes."""
    return "x" * (megabytes * 1024 * 1024)


@limits.query()
def shout(ctx: QueryContext, megabytes: int) -> str:
    """Print this many megabytes on standard error."""
    line = "y" * 1023 + "\\n"
    for _ in range(megabytes * 1024):
        sys.stderr.write(line)
    sys.stderr.flush()
    return "done"


@limits.query()
def cpu_limit(ctx: QueryContext) -> int:
    """The worker's soft limit on CPU seconds, -1 for none."""
    import resource

    return resource.getrlimit(resource.RLIMIT_CPU)[0]


@limits.query()
def crash(ctx: QueryContext, path: str) -> int:
    """Note the call in a file, then crash the way C code does when memory runs out."""
    with open(path, "a") as notes:
        notes.write("called\\n")
    os.abort()
    return 0


@limits.action()
def write_big(ctx: ActionContext, megabytes: int) -> None:
    """Write a large file."""
    Path("notes.txt").write_bytes(b"z" * (megabytes * 1024 * 1024))
'''
)


def query(name: str, **arguments: Any) -> dict[str, Any]:
    return {
        "op": "query",
        "caller": SAM,
        "now": NOW,
        "name": name,
        "arguments": arguments,
        "files": {},
    }


@pytest.fixture
def app_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "limits"
    folder.mkdir()
    (folder / "app.py").write_text(LIMITS_APP)
    return folder


def held_while(call: Any) -> tuple[Any, int]:
    """What `call()` answers, and the most memory Python allocated meanwhile, in bytes.

    Measured from now, so an earlier test that used more can't hide what this one holds.
    """
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        start = tracemalloc.get_traced_memory()[0]
        answer = call()
        return answer, tracemalloc.get_traced_memory()[1] - start
    finally:
        tracemalloc.stop()


def state_of(pid: int) -> str | None:
    """The state `ps` gives the process, `Z` for a zombie; None when it is gone."""
    if Path("/proc/self").exists():
        try:
            return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
        except FileNotFoundError:
            return None
    # macOS keeps no /proc, and `ps` is on both.
    found = subprocess.run(["ps", "-o", "state=", "-p", str(pid)], capture_output=True, text=True)
    return found.stdout.strip() or None


def is_gone(pid: int) -> bool:
    """Whether the process is gone: stopped, or a zombie, which only its parent can reap."""
    return state_of(pid) in (None, "Z", "X")


def test_a_worker_has_an_empty_environment(
    app_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIQUET_SANDBOX_TOKEN", "the-sandbox-s-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "a-cloud-key")
    pool = WorkerPool()
    try:
        answer = pool.request(app_folder, query("environment"), python=sys.executable)
    finally:
        pool.close()
    # macOS's CoreFoundation adds one of its own to every process it starts.
    names = [name for name in answer["result"] if name != "__CF_USER_TEXT_ENCODING"]
    assert names == ["HOME", "LANG", "PATH", "TMPDIR"]


@needs_memory_limit
def test_a_worker_that_eats_memory_is_stopped(app_folder: Path) -> None:
    pool = WorkerPool()
    try:
        answer = pool.request(app_folder, query("eat", megabytes=1500), python=sys.executable)
        assert answer["error"]["kind"] == "failed"
        assert "memory" in answer["error"]["message"]
        assert len(pool._workers) == 0
        again = pool.request(app_folder, query("eat", megabytes=10), python=sys.executable)
        assert again["result"] == 10 * 1024 * 1024
    finally:
        pool.close()


@needs_prlimit
def test_a_worker_that_spins_is_stopped(tmp_path: Path) -> None:
    files = {"app.py": LIMITS_APP.encode()}
    version = version_id(files)
    pool = WorkerPool(timeout=20, cpu_seconds=2)
    sandbox = create_sandbox(tmp_path / "root", pool=pool)
    answers: list[Any] = []
    with TestClient(sandbox) as client:
        assert put_version(client, "sam", "limits", files).status_code == 200
        started = time.monotonic()
        thread = threading.Thread(
            target=lambda: answers.append(
                client.post(f"/apps/sam/limits/versions/{version}/run", json=query("spin"))
            )
        )
        thread.start()
        healths = 0
        while thread.is_alive():
            assert client.get("/health").status_code == 200
            healths += 1
            thread.join(timeout=0.2)
        elapsed = time.monotonic() - started
    assert healths > 1
    assert elapsed < 10
    error = answers[0].json()["error"]
    assert error["kind"] == "failed"
    assert "CPU" in error["message"]


def test_a_worker_s_children_die_with_it(app_folder: Path) -> None:
    pool = WorkerPool()
    try:
        pid = pool.request(app_folder, query("child"), python=sys.executable)["result"]
        assert not is_gone(pid)
        pool.stop(app_folder)
        deadline = time.monotonic() + 10
        while not is_gone(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert is_gone(pid)
    finally:
        pool.close()


def test_a_large_answer_is_refused(app_folder: Path) -> None:
    pool = WorkerPool()
    try:
        assert pool.request(app_folder, query("large", megabytes=1), python=sys.executable)
        answer, grown = held_while(
            lambda: pool.request(app_folder, query("large", megabytes=64), python=sys.executable)
        )
        assert answer["error"]["kind"] == "failed"
        assert "too large" in answer["error"]["message"]
        assert grown < 60 * 1024 * 1024
        assert len(pool._workers) == 0
    finally:
        pool.close()


def test_a_worker_s_prints_are_bounded(app_folder: Path, capfd: pytest.CaptureFixture[str]) -> None:
    pool = WorkerPool()
    try:
        assert pool.request(app_folder, query("shout", megabytes=1), python=sys.executable)
        answer, grown = held_while(
            lambda: pool.request(app_folder, query("shout", megabytes=100), python=sys.executable)
        )
        assert answer["result"] == "done"
        assert grown < 32 * 1024 * 1024
        warm = next(iter(pool._workers.values()))
        assert 0 < len(warm.prints.kept()) <= 64 * 1024
    finally:
        pool.close()
    assert len(capfd.readouterr().err) < 64 * 1024


def test_a_large_file_is_refused(app_folder: Path) -> None:
    pool = WorkerPool()
    body = {
        "op": "call",
        "caller": SAM,
        "now": NOW,
        "name": "write_big",
        "arguments": {"megabytes": 100},
        "files": {"notes.txt": ""},
    }
    try:
        answer = pool.request(app_folder, body, python=sys.executable)
    finally:
        pool.close()
    assert answer["error"]["kind"] == "failed"
    assert "64 MB" in answer["error"]["message"]


def test_a_call_may_rewrite_as_much_data_as_a_draft_holds(app_folder: Path) -> None:
    # So a migration can rewrite all the data of an app just published: the change fits one
    # answer, and the worker's memory holds the data, its base64 and the answer at once.
    megabytes = MAX_DATA >> 20
    assert megabytes << 20 == MAX_DATA
    pool = WorkerPool()
    body = {
        "op": "call",
        "caller": SAM,
        "now": NOW,
        "name": "write_big",
        "arguments": {"megabytes": megabytes},
        "files": {"notes.txt": base64.b64encode(b"y" * MAX_DATA).decode()},
    }
    try:
        answer = pool.request(app_folder, body, python=sys.executable)
    finally:
        pool.close()
    assert "error" not in answer, answer["error"]
    assert base64.b64decode(answer["changes"]["notes.txt"]) == b"z" * MAX_DATA


def test_a_ready_version_starts_without_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = bundle_of(APPS / "stock")
    version = version_id(files)
    sandbox = create_sandbox(tmp_path / "root")
    data = {
        name: base64.b64encode((APPS / "stock" / "data" / name).read_bytes()).decode()
        for name in ("products.csv", "movements.csv")
    }
    body = {**query("stock"), "caller": {"id": "sam", "role": "owner"}, "files": data}
    with TestClient(sandbox) as client:
        put = put_version(client, "sam", "stock", files)
        assert put.status_code == 200, put.text
        sandbox.state.pool.close()
        uv = shutil.which("uv")
        assert uv is not None
        without = os.pathsep.join(
            part
            for part in os.environ["PATH"].split(os.pathsep)
            if part and not (Path(part) / "uv").exists()
        )
        monkeypatch.setenv("PATH", without)
        assert shutil.which("uv") is None
        answer = client.post(f"/apps/sam/stock/versions/{version}/run", json=body)
    assert answer.status_code == 200
    assert len(answer.json()["result"]) == 5


@pytest.mark.browser
@pytest.mark.skipif(screenshot.find_browser() is None, reason="needs a Chrome or Chromium")
def test_tests_and_pictures_still_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIQUET_SANDBOX_TOKEN", "the-sandbox-s-secret")
    files = bundle_of(APPS / "stock")
    version = version_id(files)
    data = {
        name: base64.b64encode((APPS / "stock" / "data" / name).read_bytes()).decode()
        for name in ("products.csv", "movements.csv")
    }
    root = tmp_path / "root"
    with TestClient(create_sandbox(root)) as client:
        put = put_version(client, "sam", "stock", files)
        assert put.status_code == 200, put.text
        tested = client.post(f"/apps/sam/stock/versions/{version}/test", json={"only": None})
        pictures = client.post(
            f"/apps/sam/stock/versions/{version}/pictures",
            json={"role": "staff", "files": data, "views": ["till"]},
        )
    assert tested.status_code == 200
    assert tested.json()["passed"] is True, tested.json()["output"]
    assert pictures.status_code == 200, pictures.text
    assert [picture["view"] for picture in pictures.json()["pictures"]] == ["till", "till"]
    assert list((root / "tmp").iterdir()) == []


@needs_prlimit
def test_a_worker_has_a_cpu_limit(app_folder: Path) -> None:
    pool = WorkerPool()
    try:
        limited = pool.request(app_folder, query("cpu_limit"), python=sys.executable)
    finally:
        pool.close()
    assert limited["result"] > 0


def test_a_worker_without_prlimit_has_no_cpu_limit(
    app_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without prlimit, the limit couldn't be raised before each call: the worker gets none.
    monkeypatch.delattr(sandbox.resource, "prlimit", raising=False)
    pool = WorkerPool()
    try:
        free = pool.request(app_folder, query("cpu_limit"), python=sys.executable)
    finally:
        pool.close()
    assert free["result"] == resource.RLIM_INFINITY


def test_a_worker_that_crashes_is_not_asked_again(app_folder: Path, tmp_path: Path) -> None:
    notes = tmp_path / "notes.txt"
    pool = WorkerPool()
    try:
        answer = pool.request(app_folder, query("crash", path=str(notes)), python=sys.executable)
    finally:
        pool.close()
    assert answer["error"]["kind"] == "failed"
    assert "memory" in answer["error"]["message"]
    assert notes.read_text() == "called\n"


def test_a_process_is_reaped_only_after_its_group_is_killed() -> None:
    process = subprocess.Popen(["true"], start_new_session=True)
    assert sandbox._wait_unreaped(process, 10) == 0
    # Still a zombie: its id, and so its group's, can't be given to another process yet.
    assert state_of(process.pid) == "Z"
    sandbox._terminate(process)
    assert process.returncode == 0
    assert is_gone(process.pid)


def test_a_migration_too_large_to_send_back_says_what_to_do(app_folder: Path) -> None:
    (app_folder / "migrate.py").write_text(
        "from pathlib import Path\n\n\n"
        "def migrate() -> None:\n"
        '    """Write more than one answer carries back."""\n'
        '    Path("notes.txt").write_bytes(b"z" * (30 * 1024 * 1024))\n'
    )
    pool = WorkerPool()
    try:
        answer = pool.request(
            app_folder, {"op": "migrate", "caller": SAM, "files": {}}, python=sys.executable
        )
    finally:
        pool.close()

    assert answer["error"]["kind"] == "failed"
    assert answer["error"]["message"] == (
        "the migration's answer was too large: one answer carries back at most 21 MB of "
        "changed data, and an app keeps no more. If the app holds more, migrate part of the "
        "files in this version, and the rest in the next one"
    )


def test_a_change_too_large_to_send_back_says_what_to_do(app_folder: Path) -> None:
    pool = WorkerPool()
    try:
        answer = pool.request(
            app_folder,
            {
                "op": "call",
                "caller": SAM,
                "now": NOW,
                "name": "write_big",
                "arguments": {"megabytes": 30},
                "files": {},
            },
            python=sys.executable,
        )
    finally:
        pool.close()

    assert answer["error"]["kind"] == "failed"
    assert answer["error"]["message"] == (
        "the answer was too large: more than 32 MB. Return less or split the call, and if a "
        "data file is the cause, bring it under 21 MB in one change or remove it."
    )
