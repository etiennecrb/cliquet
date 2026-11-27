import base64
import itertools
import json
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import HEADER
from test_runtime import LOG_APP, TALLY

from cliquet.worker import Worker, handle

_names = itertools.count()
NOW = datetime(2026, 11, 10, 18, 30, tzinfo=ZoneInfo("Europe/Paris"))
SAM = {"id": "sam", "role": "owner"}


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def files(**texts: str) -> dict[str, str]:
    return {name: b64(text) for name, text in texts.items()}


def request(op: str, **fields: Any) -> dict[str, Any]:
    return {"op": op, "caller": SAM, "now": NOW.isoformat(), **fields}


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    app.mkdir()
    (app / "app.py").write_text(LOG_APP)
    return app


@pytest.fixture
def worker(folder: Path) -> Iterator[Worker]:
    made = Worker(folder / "app.py", module_name=f"cliquet_process_{next(_names)}")
    yield made
    made.close()


def test_manifest(worker: Worker, folder: Path) -> None:
    answer = handle(worker, folder, {"op": "manifest"})
    assert answer["result"]["name"] == "log"


def test_call_returns_changes_in_base64(worker: Worker, folder: Path) -> None:
    answer = handle(
        worker,
        folder,
        request(
            "call",
            name="append",
            arguments={"line": "hello"},
            files=files(**{"log.txt": "start\n"}),
        ),
    )
    assert answer == {"changes": {"log.txt": b64("start\nhello\n")}, "result": None}


def test_call_returns_a_refusal(worker: Worker, folder: Path) -> None:
    answer = handle(
        worker,
        folder,
        request(
            "call",
            name="append",
            arguments={"line": "forbidden"},
            files=files(**{"log.txt": "start\n"}),
        ),
    )
    refusal = answer["refusal"]
    assert refusal["message"] is None
    assert refusal["invariants"] == {"no_forbidden": ["line 2"]}
    assert refusal["descriptions"]["no_forbidden"] == "No line says forbidden"


def test_apply(worker: Worker, folder: Path) -> None:
    before = files(**{"log.txt": "a\nb\n"})
    shrunk = handle(
        worker,
        folder,
        request("apply", label="revert", files=before, result=files(**{"log.txt": "a\n"})),
    )
    assert "append_only" in shrunk["refusal"]["invariants"]
    grown = handle(
        worker,
        folder,
        request("apply", label="revert", files=before, result=files(**{"log.txt": "a\nb\nc\n"})),
    )
    assert grown == {"changes": {"log.txt": b64("a\nb\nc\n")}, "result": None}
    same = handle(worker, folder, request("apply", label="revert", files=before, result=before))
    assert same == {"changes": {}, "result": None}


def test_query_and_view(worker: Worker, folder: Path) -> None:
    answer = handle(
        worker,
        folder,
        request("query", name="lines", arguments={}, files=files(**{"log.txt": "start\n"})),
    )
    assert answer == {"result": ["start"]}


def test_a_view_is_laid_out(worker: Worker, folder: Path) -> None:
    page = (
        HEADER
        + """\
\"\"\"One screen.\"\"\"

from pathlib import Path

from cliquet import App, QueryContext, Screen

page = App("page", files=["log.txt"], roles=["owner", "reader"])


@page.view(roles=["owner", "reader"])
def overview(ctx: QueryContext, last: int = 10) -> Screen:
    \"\"\"The log.\"\"\"
    lines = Path("log.txt").read_text().splitlines()[-last:]
    return Screen("<p>{{ lines | join(', ') }}</p>", args={"last": last}, lines=lines)
"""
    )
    (folder / "app.py").write_text(page)
    made = Worker(folder / "app.py", module_name=f"cliquet_process_{next(_names)}")
    log = files(**{"log.txt": "start\nmore\n"})
    try:
        answer = handle(
            made, folder, request("view", name="overview", arguments={"last": 1}, files=log)
        )
        plain = handle(made, folder, request("view", name="overview", arguments={}, files=log))
        wrong = handle(
            made, folder, request("view", name="overview", arguments={"last": "x"}, files=log)
        )
    finally:
        made.close()
    # The worker answers the arguments that draw it again, and the tree: the runtime checks it.
    assert answer == {"result": {"arguments": {"last": 1}, "body": [["p", {}, "more"]]}}
    assert plain == {"result": {"arguments": {}, "body": [["p", {}, "start, more"]]}}
    assert wrong["error"]["kind"] == "invalid_arguments"


def test_check(worker: Worker, folder: Path) -> None:
    answer = handle(worker, folder, request("check", files=files(**{"log.txt": "start\n"})))
    assert answer == {"result": {"refusal": None, "views": []}}


def test_errors_have_a_kind(worker: Worker, folder: Path) -> None:
    wrong = handle(
        worker,
        folder,
        request(
            "call",
            name="append",
            arguments={"line": 3, "extra": 1},
            files=files(**{"log.txt": "start\n"}),
        ),
    )
    assert wrong["error"]["kind"] == "invalid_arguments"
    far = handle(
        worker,
        folder,
        request(
            "query", name="line", arguments={"number": 99}, files=files(**{"log.txt": "start\n"})
        ),
    )
    assert far["error"]["kind"] == "refused"
    missing = handle(
        worker,
        folder,
        request("call", name="nope", arguments={}, files=files(**{"log.txt": "start\n"})),
    )
    assert missing["error"]["kind"] == "unknown"
    assert handle(worker, folder, {"op": "nope"})["error"]["kind"] == "unknown"


def test_migrate_runs_the_function_in_migrate_py(worker: Worker, folder: Path) -> None:
    (folder / "migrate.py").write_text(
        "from pathlib import Path\n\n\ndef migrate() -> None:\n"
        "    with open('log.txt', 'a') as f:\n        f.write('migrated\\n')\n"
    )
    answer = handle(worker, folder, request("migrate", files=files(**{"log.txt": "start\n"})))
    assert answer == {"changes": {"log.txt": b64("start\nmigrated\n")}, "result": None}
    (folder / "migrate.py").unlink()
    missing = handle(worker, folder, request("migrate", files=files(**{"log.txt": "start\n"})))
    assert missing["error"] == {"kind": "failed", "message": "migrate.py must define migrate()"}


def start(folder: Path, source: str) -> subprocess.Popen[str]:
    path = folder / "app.py"
    path.write_text(source)
    return subprocess.Popen(
        [sys.executable, "-m", "cliquet.worker", str(folder)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def send(process: subprocess.Popen[str], body: dict[str, Any]) -> dict[str, Any]:
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(json.dumps(body) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    return json.loads(line)


def test_the_process_answers_on_its_pipe(tmp_path: Path) -> None:
    folder = tmp_path / "app"
    folder.mkdir()
    process = start(folder, LOG_APP)
    try:
        first = send(process, {"id": 1, "op": "manifest"})
        assert first["id"] == 1
        assert first["result"]["name"] == "log"
        second = send(
            process,
            {
                "id": 2,
                **request(
                    "call",
                    name="append",
                    arguments={"line": "x"},
                    files=files(**{"log.txt": "start\n"}),
                ),
            },
        )
        assert second["id"] == 2
        assert second["changes"] == {"log.txt": b64("start\nx\n")}
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=10) == 0
    finally:
        process.kill()
        process.wait()


def test_the_result_crosses_the_process(tmp_path: Path) -> None:
    folder = tmp_path / "app"
    folder.mkdir()
    process = start(folder, TALLY.read_text())
    try:
        added = send(
            process,
            {"id": 1, **request("call", name="add", arguments={"amounts": ["1.50"]}, files={})},
        )
        assert added == {
            "id": 1,
            "changes": {"amounts.txt": b64("1.50\n")},
            "result": {"added": 1, "total": "1.50", "day": "2026-11-10"},
        }
        wrong = send(process, {"id": 2, **request("call", name="wrong", arguments={}, files={})})
        assert wrong == {
            "id": 2,
            "error": {
                "kind": "app_error",
                "message": "- wrong returned 3, which doesn't match `-> Added`",
                "problems": [
                    {
                        "where": "action wrong",
                        "message": "wrong returned 3, which doesn't match `-> Added`",
                    }
                ],
            },
        }
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=10) == 0
    finally:
        process.kill()
        process.wait()


def test_prints_from_app_code_stay_off_the_pipe(tmp_path: Path) -> None:
    source = LOG_APP.replace(
        '    if not line:\n        raise Refused("Empty line")\n',
        '    print("hello")\n    if not line:\n        raise Refused("Empty line")\n',
    )
    assert source != LOG_APP
    folder = tmp_path / "app"
    folder.mkdir()
    process = start(folder, source)
    try:
        answer = send(
            process,
            {
                "id": 1,
                **request(
                    "call",
                    name="append",
                    arguments={"line": "hello"},
                    files=files(**{"log.txt": "start\n"}),
                ),
            },
        )
        assert answer["changes"] == {"log.txt": b64("start\nhello\n")}
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        assert process.stderr is not None
        assert "hello" in process.stderr.read()
    finally:
        process.kill()
        process.wait()


def test_an_app_that_does_not_load_is_reported(tmp_path: Path) -> None:
    folder = tmp_path / "app"
    folder.mkdir()
    process = start(folder, LOG_APP + '\nsecond = App("second", files=[])\n')
    try:
        out, _ = process.communicate(timeout=10)
    finally:
        process.kill()
        process.wait()
    answer = json.loads(out)
    assert answer["id"] is None
    assert answer["error"]["kind"] == "app_error"
    (problem,) = answer["error"]["problems"]
    assert problem["where"] == "App('second')"
    assert answer["error"]["message"] == f"- {problem['message']}"


def test_a_line_that_is_not_json(tmp_path: Path) -> None:
    folder = tmp_path / "app"
    folder.mkdir()
    process = start(folder, LOG_APP)
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write("hello\n")
        process.stdin.flush()
        answer = json.loads(process.stdout.readline())
        assert answer == {"id": None, "error": {"kind": "failed", "message": "not JSON"}}
        assert send(process, {"id": 3, "op": "manifest"})["id"] == 3
        process.stdin.close()
        assert process.wait(timeout=10) == 0
    finally:
        process.kill()
        process.wait()


def test_a_key_error_in_app_code_is_a_failure(tmp_path: Path) -> None:
    app = tmp_path / "keys"
    app.mkdir()
    (app / "app.py").write_text(
        HEADER
        + '''"""Keys."""

from cliquet import App, Denied, QueryContext

keys = App("keys", files=["k.txt"])


@keys.query
def closed(ctx: QueryContext) -> int:
    """Deny, in the app's own words."""
    raise Denied("not on Sundays")


@keys.query
def missing(ctx: QueryContext) -> int:
    """Raise a KeyError with data in it."""
    return {}["Landlord"]
'''
    )
    worker = Worker(app / "app.py", module_name=f"cliquet_process_{next(_names)}")
    try:
        failed = handle(
            worker,
            app,
            request("query", name="missing", arguments={}, files=files(**{"k.txt": ""})),
        )
        trace = failed["error"].pop("traceback")
        assert failed["error"] == {
            "kind": "failed",
            "message": "KeyError: 'Landlord'",
            "type": "KeyError",
        }
        # Where in app.py it was raised, which the server keeps for the app's owner.
        assert 'return {}["Landlord"]' in trace and "in missing" in trace
        assert trace.rstrip().endswith("KeyError: 'Landlord'")
        denied = handle(
            worker, app, request("query", name="closed", arguments={}, files=files(**{"k.txt": ""}))
        )
        assert denied["error"] == {"kind": "denied", "message": "not on Sundays"}
        unknown = handle(
            worker, app, request("query", name="nope", arguments={}, files=files(**{"k.txt": ""}))
        )
        assert unknown["error"] == {"kind": "unknown", "message": "there is no query named nope"}
    finally:
        worker.close()


def test_a_malformed_request_is_unknown(worker: Worker, folder: Path) -> None:
    some = files(**{"log.txt": "start\n"})
    for bad in (
        {"op": 3},
        {"op": ["call"]},
        request("query", name=["lines"], arguments={}, files=some),
        request("call", name={"x": 1}, arguments={}, files=some),
    ):
        assert handle(worker, folder, bad)["error"]["kind"] == "unknown", bad
    not_a_dict: Any = ["op", "call"]
    assert handle(worker, folder, not_a_dict)["error"]["kind"] == "unknown"


def test_a_failed_invariant_is_the_app_failing(worker: Worker, folder: Path) -> None:
    answer = handle(
        worker,
        folder,
        request("call", name="append", arguments={"line": "boom"}, files=files(**{"log.txt": ""})),
    )
    checked = handle(worker, folder, request("check", files=files(**{"log.txt": "boom\n"})))
    for failed in (answer, checked):
        trace = failed["error"].pop("traceback")
        assert "in no_boom" in trace and trace.rstrip().endswith("RuntimeError: it went boom")
    message = "RuntimeError: it went boom, in the invariant no_boom"
    assert answer == {"error": {"kind": "failed", "message": message, "type": "RuntimeError"}}
    assert checked == answer
    # A refusal is only its message and its invariants' problems.
    refused = handle(
        worker,
        folder,
        request(
            "call", name="append", arguments={"line": "forbidden"}, files=files(**{"log.txt": ""})
        ),
    )
    assert set(refused["refusal"]) == {"message", "invariants", "descriptions"}


def test_the_caller_s_token_id_is_optional_on_the_wire(tmp_path: Path) -> None:
    app = tmp_path / "token"
    app.mkdir()
    (app / "app.py").write_text(
        HEADER
        + '''"""The token behind a call."""

from cliquet import App, QueryContext

token = App("token", files=["t.txt"])


@token.query
def mine(ctx: QueryContext) -> str | None:
    """The id of the token behind the call."""
    return ctx.caller.token_id
'''
    )
    worker = Worker(app / "app.py", module_name=f"cliquet_process_{next(_names)}")
    try:
        ask = request("query", name="mine", arguments={}, files=files(**{"t.txt": ""}))
        assert handle(worker, app, ask) == {"result": None}
        through = {**ask, "caller": {"id": "sam", "role": None, "token_id": "tok_0123"}}
        assert handle(worker, app, through) == {"result": "tok_0123"}
    finally:
        worker.close()
