import base64
import subprocess
import traceback
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import pytest
from mcp_types import TextContent

from cliquet.context import Caller
from cliquet.errors import AppError, Denied, Problem, Refused, from_app
from cliquet.icons import drawings
from cliquet.limits import MAX_DATA
from cliquet.mcp_server import AppServer
from cliquet.runtime import Committed, InvalidArguments, Refusal, Runtime
from cliquet.scaffold import new_app
from cliquet.worker import MAX_TRACEBACK, Changes, Checked
from cliquet_server.engine import MAX_RESULT, AppFailed, SandboxEngine, UnknownName
from cliquet_server.executor import (
    MAX_ERROR,
    BuildProblems,
    Built,
    LocalExecutor,
    SandboxClient,
    UnknownVersion,
)
from cliquet_server.repos import AppRepo

from .server_helpers import APPS, GIT_ENV, NOW, OWNER, STAFF, served_example, unexpected

VERSION = "0123456789abcdef"
NO_ARGUMENTS: dict[str, Any] = {"type": "object", "properties": {}}
MANIFEST: dict[str, Any] = {
    "name": "shop",
    "roles": ["owner", "staff"],
    "files": ["a.csv", "b.csv", "items.csv"],
    "actions": [
        {"name": "record_sale", "roles": None, "input_schema": NO_ARGUMENTS},
        {"name": "set_price", "roles": ["owner"], "input_schema": NO_ARGUMENTS},
    ],
    "queries": [{"name": "stock", "roles": None, "input_schema": NO_ARGUMENTS}],
    "views": [
        {
            "name": "overview",
            "roles": None,
            "input_schema": {"type": "object", "properties": {"month": {"type": "string"}}},
        }
    ],
}


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


class FakeClient:
    def __init__(self, answers: list[Any]) -> None:
        self.answers = answers
        self.requests: list[dict[str, Any]] = []
        self.puts: list[tuple[str, str, str, dict[str, bytes]]] = []

    def run(self, login: str, app: str, version: str, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def put_version(self, login: str, app: str, version: str, files: dict[str, bytes]) -> Built:
        self.puts.append((login, app, version, files))
        return Built({}, b"lock")


def made(
    answers: list[Any], code: dict[str, bytes] | None = None, manifest: dict[str, Any] = MANIFEST
) -> tuple[SandboxEngine, FakeClient]:
    fake = FakeClient(answers)
    engine = SandboxEngine(
        cast(SandboxClient, fake),
        "etienne",
        "shop",
        VERSION,
        manifest,
        lambda: code or {"app.py": b"code"},
    )
    return engine, fake


def error(kind: str, message: str) -> SandboxEngine:
    return made([{"error": {"kind": kind, "message": message}}])[0]


def test_requests_carry_the_call() -> None:
    engine, fake = made([{"changes": {}}])
    agent = Caller("sam", "owner", "tok_0123456789abcdef")
    engine.run_action(agent, NOW, "record_sale", {"sku": "TEA-SEN"}, {"a.csv": b"x"})
    sent = fake.requests[0]
    assert sent["op"] == "call"
    assert sent["caller"] == {"id": "sam", "role": "owner", "token_id": "tok_0123456789abcdef"}
    assert sent["now"] == NOW.isoformat()
    assert sent["name"] == "record_sale"
    assert sent["arguments"] == {"sku": "TEA-SEN"}
    assert sent["files"] == {"a.csv": b64("x")}


def test_changes_and_refusals_are_decoded() -> None:
    engine, _ = made([{"changes": {"a.csv": b64("x"), "b.csv": None}}])
    assert engine.run_action(OWNER, NOW, "x", {}, {"a.csv": b"old"}) == Changes(
        {"a.csv": b"x", "b.csv": None}
    )
    refusal = {
        "message": "no",
        "invariants": {"stock": ["one left"]},
        "descriptions": {"stock": "Stock stays positive"},
    }
    engine, _ = made([{"refusal": refusal}])
    assert engine.run_action(OWNER, NOW, "x", {}, {}) == Refusal(
        message="no",
        invariants={"stock": ["one left"]},
        descriptions={"stock": "Stock stays positive"},
    )


def test_migrate_is_sent_like_an_action() -> None:
    engine, fake = made([{"changes": {"items.csv": b64("x")}}])

    assert engine.migrate(OWNER, {"items.csv": b"old"}) == Changes({"items.csv": b"x"})
    sent = fake.requests[0]
    assert sent["op"] == "migrate"
    assert sent["caller"] == {"id": "sam", "role": "owner", "token_id": None}
    assert sent["files"] == {"items.csv": b64("old")}


def test_check_is_decoded() -> None:
    refusal = {
        "message": None,
        "invariants": {"stock": ["one left"]},
        "descriptions": {"stock": "Stock stays positive"},
    }
    engine, _ = made(
        [{"result": {"refusal": refusal, "views": [{"where": "view v", "message": "bad"}]}}]
    )
    assert engine.check(NOW, {}) == Checked(
        Refusal(
            message=None,
            invariants={"stock": ["one left"]},
            descriptions={"stock": "Stock stays positive"},
        ),
        [Problem("view v", "bad")],
    )
    engine, _ = made([{"result": {"refusal": None, "views": []}}])
    assert engine.check(NOW, {}) == Checked(None, [])


def test_errors_keep_their_type() -> None:
    with pytest.raises(InvalidArguments, match="bad arguments"):
        error("invalid_arguments", "bad arguments").run_action(OWNER, NOW, "x", {}, {})
    with pytest.raises(Refused, match="no stock"):
        error("refused", "no stock").run_action(OWNER, NOW, "x", {}, {})
    with pytest.raises(Denied, match="not on Sundays"):
        error("denied", "not on Sundays").query_json(OWNER, NOW, "x", {}, {}, None)
    with pytest.raises(UnknownName, match="there is no action named x") as unknown:
        error("unknown", "no such action").run_action(OWNER, NOW, "x", {}, {})
    # Cliquet's own text about a name: every caller may read it.
    assert not from_app(unknown.value)
    with pytest.raises(AppError) as problems:
        error("app_error", "- first\n- second").run_action(OWNER, NOW, "x", {}, {})
    assert problems.value.problems == [Problem("app.py", "first"), Problem("app.py", "second")]
    # The worker says where each problem is: the engine keeps it.
    sent = {"kind": "app_error", "message": "- no data/\n- b: c"}
    sent["problems"] = [
        {"where": "tests/test_a.py:3", "message": "no data/"},
        {"where": "app.py:12 total", "message": "b: c"},
    ]
    with pytest.raises(AppError) as located:
        made([{"error": sent}])[0].run_action(OWNER, NOW, "x", {}, {})
    assert located.value.problems == [
        Problem("tests/test_a.py:3", "no data/"),
        Problem("app.py:12 total", "b: c"),
    ]
    with pytest.raises(AppFailed, match="it broke"):
        error("failed", "it broke").run_action(OWNER, NOW, "x", {}, {})


def test_a_lost_version_is_put_again() -> None:
    code = {"app.py": b"code"}
    engine, fake = made([UnknownVersion("gone"), {"changes": {"a.csv": b64("x")}}], code)
    assert engine.run_action(OWNER, NOW, "x", {}, {}) == Changes({"a.csv": b"x"})
    assert fake.puts == [("etienne", "shop", VERSION, code)]
    assert len(fake.requests) == 2


def outside_git(folder: Path) -> dict[str, bytes]:
    """Every file under `folder` but those in a `.git` folder, with its content."""
    return {
        str(path.relative_to(folder)): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(folder).parts
    }


@pytest.mark.parametrize("name", ["/tmp/x", "../x", "other.csv"])
def test_an_undeclared_change_is_refused(tmp_path: Path, name: str) -> None:
    engine, _ = made([{"changes": {name: b64("x")}}])
    with pytest.raises(AppFailed, match=f"a change to {name}, which the app doesn't declare"):
        engine.run_action(OWNER, NOW, "record_sale", {}, {"a.csv": b"old"})

    folder = tmp_path / "shop"
    AppRepo(folder).publish(
        None,
        {"app.py": b"code", "data/a.csv": b"old\n"},
        version=VERSION,
        note="test",
        login="etienne",
        when=NOW,
    )
    escape = tmp_path / "escape.csv"
    engine, _ = made([{"changes": {str(escape): b64("x")}}, {"changes": {name: b64("x")}}])
    served = Runtime(folder, engine=engine, clock=lambda: NOW, owner=OWNER.id)
    head = served.store.head()
    before = outside_git(tmp_path)
    for _ in range(2):
        with pytest.raises(AppFailed, match="which the app doesn't declare"):
            served.call(OWNER, "record_sale", {})
    assert served.store.head() == head
    assert outside_git(tmp_path) == before
    assert not escape.exists()


def test_a_change_is_at_most_21_mb() -> None:
    full = b"x" * MAX_DATA
    engine, _ = made([{"changes": {"a.csv": base64.b64encode(full).decode()}}])
    assert engine.run_action(OWNER, NOW, "record_sale", {}, {}) == Changes({"a.csv": full})
    big = base64.b64encode(b"x" * (MAX_DATA + 1)).decode()
    engine, _ = made([{"changes": {"a.csv": big}}, {"changes": {"a.csv": big}}])
    refused = engine.run_action(OWNER, NOW, "record_sale", {}, {})
    assert refused == Refusal(
        message="data/a.csv is 21.0 MB, and a file may be at most 21 MB: bring it under "
        "that in one change, or remove it."
    )
    # A refusal is Cliquet's own sentence, so everyone reads it, the staff included.
    assert engine.run_action(STAFF, NOW, "record_sale", {}, {}) == refused


def published(folder: Path) -> None:
    AppRepo(folder).publish(
        None,
        {"app.py": b"code", "data/a.csv": b"old\n"},
        version=VERSION,
        note="test",
        login="etienne",
        when=NOW,
    )


def test_the_engine_passes_the_result(tmp_path: Path) -> None:
    imported = {"added": 42, "skipped": 3}
    engine, _ = made([{"changes": {"a.csv": b64("new\n")}, "result": imported}])
    published(tmp_path / "shop")
    served = Runtime(tmp_path / "shop", engine=engine, clock=lambda: NOW, owner=OWNER.id)
    assert served.call(OWNER, "record_sale", {}) == Committed(
        served.store.head(), ["a.csv"], imported
    )
    # Only an action has a result: a migration's or a revert's answer can't add one.
    engine, _ = made([{"changes": {"items.csv": b64("x")}, "result": None}])
    assert engine.migrate(OWNER, {}) == Changes({"items.csv": b"x"})
    engine, _ = made([{"changes": {"items.csv": b64("x")}, "result": imported}])
    with pytest.raises(AppFailed, match="a result for a migration, which has none"):
        engine.migrate(OWNER, {})
    engine, _ = made([{"changes": {"a.csv": b64("x")}, "result": imported}])
    with pytest.raises(AppFailed, match="a result for the change 'revert', which has none"):
        engine.apply(OWNER, "revert", {}, {"a.csv": b"x"})


def test_a_result_over_1_mb_is_refused(tmp_path: Path) -> None:
    fits = "x" * (MAX_RESULT - 2)  # with its quotes, exactly 1 MB of JSON
    engine, _ = made([{"changes": {}, "result": fits}, {"changes": {}, "result": fits + "x"}])
    assert engine.run_action(OWNER, NOW, "record_sale", {}, {}) == Changes({}, fits)
    with pytest.raises(AppFailed, match="the sandbox answered a result of more than 1 MB"):
        engine.run_action(OWNER, NOW, "record_sale", {}, {})

    published(tmp_path / "shop")
    big = {"changes": {"a.csv": b64("new\n")}, "result": fits + "x"}
    engine, _ = made([big])
    served = Runtime(tmp_path / "shop", engine=engine, clock=lambda: NOW, owner=OWNER.id)
    head = served.store.head()
    with pytest.raises(AppFailed, match="more than 1 MB"):
        served.call(OWNER, "record_sale", {})
    assert served.store.head() == head


def test_a_migration_may_remove_a_file_the_repository_has() -> None:
    engine, _ = made([{"changes": {"old.csv": None, "items.csv": b64("x")}}])
    assert engine.migrate(OWNER, {"old.csv": b"old"}) == Changes(
        {"old.csv": None, "items.csv": b"x"}
    )
    engine, _ = made([{"changes": {"old.csv": b64("x")}}])
    with pytest.raises(AppFailed, match=r"a change to old\.csv"):
        engine.migrate(OWNER, {"old.csv": b"old"})


LEDGER_MANIFEST = {**MANIFEST, "files": ["notes.txt", "**/*.beancount"]}


@pytest.mark.parametrize("name", ["ledger.beancount", "2026/02.beancount", "notes.txt"])
def test_a_change_to_a_file_a_pattern_matches_is_taken(name: str) -> None:
    engine, _ = made([{"changes": {name: b64("x")}}], manifest=LEDGER_MANIFEST)
    assert engine.run_action(OWNER, NOW, "record_sale", {}, {}) == Changes({name: b"x"})


@pytest.mark.parametrize("name", ["2026/02.csv", ".cache/a.beancount", "../a.beancount"])
def test_a_change_to_a_file_no_pattern_matches_is_refused(name: str) -> None:
    engine, _ = made([{"changes": {name: b64("x")}}], manifest=LEDGER_MANIFEST)
    with pytest.raises(AppFailed, match="which the app doesn't declare"):
        engine.run_action(OWNER, NOW, "record_sale", {}, {})


def test_a_migration_may_remove_only_a_file_no_pattern_matches() -> None:
    given = {"old.csv": b"old", "2026/01.beancount": b"jan"}
    engine, _ = made([{"changes": {"old.csv": None}}], manifest=LEDGER_MANIFEST)
    assert engine.migrate(OWNER, given) == Changes({"old.csv": None})
    engine, _ = made([{"changes": {"old.csv": b64("x")}}], manifest=LEDGER_MANIFEST)
    with pytest.raises(AppFailed, match=r"a change to old\.csv"):
        engine.migrate(OWNER, given)


def test_a_manifest_may_declare_patterns_but_not_bad_ones() -> None:
    answers = [["notes.txt", "**/*.beancount"], ["a.csv", "**/*"]]

    def handler(request: httpx.Request) -> httpx.Response:
        manifest = {**MANIFEST, "files": answers.pop(0)}
        return httpx.Response(200, json={"manifest": manifest, "lock": b64("lock")})

    client = SandboxClient("http://sandbox", transport=httpx.MockTransport(handler))
    built = client.put_version("etienne", "shop", VERSION, {"app.py": b"code"})
    assert built.manifest["files"] == ["notes.txt", "**/*.beancount"]
    with pytest.raises(BuildProblems) as refused:
        client.put_version("etienne", "shop", VERSION, {"app.py": b"code"})
    assert refused.value.problems == [
        "'**/*' can't be declared: its last part is only a wildcard, which would match any "
        "file, such as a database's journal or a cache: end it with a name's end, such as "
        "*.csv. Declare a data file's name, such as stock.csv, or a pattern of names with at "
        "most one ** as a whole part, for any folders, and one * inside a part, ending in a "
        "name's end, such as **/*.beancount or 2026/*.csv."
    ]


def test_a_bad_manifest_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        manifest = {**MANIFEST, "files": ["a.csv", "../x"]}
        return httpx.Response(200, json={"manifest": manifest, "lock": b64("lock")})

    client = SandboxClient("http://sandbox", transport=httpx.MockTransport(handler))
    with pytest.raises(BuildProblems) as refused:
        client.put_version("etienne", "shop", VERSION, {"app.py": b"code"})
    assert any("'../x' can't be a data file" in problem for problem in refused.value.problems)


def test_a_forbidden_screen_is_refused(tmp_path: Path) -> None:
    # The engine passes the view's arguments, and answers the sandbox's screen as it is.
    script = {"arguments": {}, "body": [["script", {}, "alert(1)"]]}
    engine, fake = made([{"result": script}])
    assert engine.view(OWNER, NOW, "overview", {"month": "2026-11"}, {}, None) == script
    (sent,) = fake.requests
    assert sent["op"] == "view" and sent["arguments"] == {"month": "2026-11"}

    # The runtime checks it, wherever the app's code ran: a sandbox may answer anything.
    button = ["button", {"class": "btn"}, "Set"]
    owner_only = {"arguments": {}, "body": [["form", {"data-call": "set_price"}, button]]}
    published(tmp_path / "shop")
    engine, _ = made([{"result": script}, {"result": owner_only}, {"result": owner_only}])
    served = Runtime(tmp_path / "shop", engine=engine, clock=lambda: NOW, owner=OWNER.id)
    with pytest.raises(AppError, match="<script> isn't allowed"):
        served.view(OWNER, "overview")
    assert served.view(OWNER, "overview")["tools"]["set_price"]["kind"] == "action"
    with pytest.raises(AppError, match="'staff' may open this view but not use set_price"):
        served.view(STAFF, "overview")


def test_a_screen_s_icons_are_drawn_from_the_server_s_copy(tmp_path: Path) -> None:
    # A sandbox may answer a drawing of its own beside the tree, or an <svg> in it: the server
    # adds the drawings of the icons the tree names from its own copy, and refuses the <svg>.
    heading = ["h2", {}, ["span", {"data-icon": "plus"}], " Ajouter"]
    hostile = [["image", {"href": "https://example.com/x.png"}], ["path", {"onload": "x()"}]]
    made_up = {"arguments": {}, "body": [heading], "icons": {"plus": hostile, "x": hostile}}
    drawn = {"arguments": {}, "body": [["svg", {}, ["path", {"d": "M0 0"}]]]}
    published(tmp_path / "shop")
    engine, _ = made([{"result": made_up}, {"result": drawn}])
    served = Runtime(tmp_path / "shop", engine=engine, clock=lambda: NOW, owner=OWNER.id)
    screen = served.view(OWNER, "overview")
    assert screen["icons"] == {"plus": [["path", {"d": "M5 12h14"}], ["path", {"d": "M12 5v14"}]]}
    assert screen["icons"]["plus"] == drawings()["plus"]
    with pytest.raises(AppError, match="<svg> isn't allowed"):
        served.view(OWNER, "overview")


def git(folder: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=folder, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def runtime(tmp_path: Path, executor: LocalExecutor) -> Iterator[Runtime]:
    made_runtime = served_example(tmp_path / "shop", executor)
    yield made_runtime
    made_runtime.close()


def test_an_action_commits_in_the_server_s_repository(runtime: Runtime) -> None:
    outcome = runtime.call(
        OWNER, "record_sale", {"sku": "TEA-SEN", "quantity": 2, "receipt": "R-0100"}
    )
    assert isinstance(outcome, Committed)
    assert outcome.files == ["movements.csv"]
    assert git(runtime.store.root, "log", "-1", "--format=%an %s").strip() == "sam record_sale"


def test_a_request_id_is_done_once(runtime: Runtime) -> None:
    body = {"sku": "TEA-SEN", "quantity": 2, "receipt": "R-0101"}
    first = runtime.call(OWNER, "record_sale", body, request_id="r-1")
    second = runtime.call(OWNER, "record_sale", body, request_id="r-1")
    assert isinstance(first, Committed)
    assert second == Committed(first.commit, first.files, replayed=True)
    subjects = git(runtime.store.root, "log", "--format=%s").splitlines()
    assert subjects.count("record_sale") == 1


def test_refusals_come_back_whole(runtime: Runtime) -> None:
    too_many = runtime.call(
        OWNER, "record_sale", {"sku": "POT-GLS", "quantity": 9, "receipt": "R-0102"}
    )
    assert isinstance(too_many, Refusal)
    assert str(too_many).startswith("Refused by: Stock never goes below zero")
    unknown = runtime.call(
        OWNER, "record_sale", {"sku": "TEA-XXX", "quantity": 1, "receipt": "R-0103"}
    )
    assert isinstance(unknown, Refusal)
    assert unknown.message is not None
    assert unknown.message.startswith("There is no product TEA-XXX")


def test_roles_are_checked_by_the_server(runtime: Runtime) -> None:
    with pytest.raises(Denied):
        runtime.call(STAFF, "set_price", {"sku": "TEA-SEN", "price": "9.00"})


def test_wrong_arguments(runtime: Runtime) -> None:
    with pytest.raises(InvalidArguments):
        runtime.call(OWNER, "record_sale", {"sku": "TEA-SEN", "quantity": "lots"})


def test_queries_and_views(runtime: Runtime) -> None:
    assert len(runtime.query_json(STAFF, "stock", {})) == 5
    assert runtime.view(STAFF, "shelves")


def test_revert(runtime: Runtime) -> None:
    before = {row["sku"]: row["quantity"] for row in runtime.query_json(OWNER, "stock", {})}
    outcome = runtime.call(
        OWNER, "record_sale", {"sku": "TEA-SEN", "quantity": 2, "receipt": "R-0104"}
    )
    assert isinstance(outcome, Committed) and outcome.commit is not None
    reverted = runtime.revert(OWNER, outcome.commit)
    assert isinstance(reverted, Committed)
    after = {row["sku"]: row["quantity"] for row in runtime.query_json(OWNER, "stock", {})}
    assert after["TEA-SEN"] == before["TEA-SEN"]


def test_check(runtime: Runtime) -> None:
    assert runtime.check() is None
    assert runtime.check_views() == []


def test_a_failed_invariant_is_the_app_s_failure() -> None:
    # A sandbox reports an invariant that raised as the app failing, not as a refusal.
    failed_answer = {"kind": "failed", "message": "KeyError: 'x', in the invariant a"}
    engine, _ = made([{"error": {**failed_answer, "type": "KeyError"}}])
    with pytest.raises(AppFailed) as failed:
        engine.run_action(OWNER, NOW, "x", {}, {})
    assert failed.value.kind == "KeyError"
    assert not getattr(failed.value, "__notes__", [])
    # Where the app's code raised, as the sandbox says it, is a note: in the error's
    # traceback, for the app's owner, never in its message. Only its end is taken.
    trace = "x" * MAX_TRACEBACK + "\n  File \"app.py\", line 3, in a\nKeyError: 'x'\n"
    engine, _ = made([{"error": {**failed_answer, "type": "KeyError", "traceback": trace}}])
    with pytest.raises(AppFailed) as traced:
        engine.run_action(OWNER, NOW, "x", {}, {})
    (note,) = traced.value.__notes__
    assert note.startswith("In the sandbox:\nxxx") and note.endswith("in a\nKeyError: 'x'")
    assert len(note) < MAX_TRACEBACK + 20
    assert str(traced.value) == failed_answer["message"]
    assert "app.py" in "".join(traceback.format_exception(traced.value))
    engine, _ = made([{"error": {**failed_answer, "traceback": ["app.py"]}}])
    with pytest.raises(AppFailed) as untraced:
        engine.run_action(OWNER, NOW, "x", {}, {})
    assert not getattr(untraced.value, "__notes__", [])
    # A refusal is only its message and its invariants' problems: any other field is dropped.
    stray = {"refusal": {"invariants": {"a": ["x"]}, "failed": {"a": "KeyError"}}}
    engine, _ = made([stray])
    outcome = engine.run_action(OWNER, NOW, "x", {}, {})
    assert outcome == Refusal(invariants={"a": ["x"]})


def test_a_failure_s_type_is_its_own_field_never_its_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A long message is cut to its head. Its tail looks like an exception's type and text, as
    # a member was once told: only the field the worker sets says the type.
    tail = "x_y: Landlord owes 1200 " + "d" * 1976
    assert len(tail) == MAX_ERROR
    long = "ValueError: " + "z " * 1500 + tail
    answers = [
        {"kind": "failed", "message": long, "type": "ValueError"},
        {"kind": "failed", "message": long},
        {"kind": "failed", "message": long, "type": "Landlord owes: 1200"},
        {"kind": "failed", "message": long, "type": ["KeyError"]},
    ]
    engine, _ = made([{"error": answer} for answer in answers])
    kinds = []
    for _ in answers:
        with pytest.raises(AppFailed) as failed:
            engine.run_action(OWNER, NOW, "x", {}, {})
        kinds.append(failed.value.kind)
        assert str(failed.value).startswith("ValueError: z z")
        assert len(str(failed.value)) <= MAX_ERROR and "Landlord" not in str(failed.value)
    assert kinds == ["ValueError", None, None, None]

    # What a member of the app reads of it, through its MCP server.
    published(tmp_path / "shop")
    sale = {"name": "record_sale", "roles": None, "description": "Sell.", "output_schema": None}
    sale["input_schema"] = {"type": "object", "properties": {}}
    manifest = {**MANIFEST, "description": "A shop.", "actions": [sale], "queries": []}
    told = []
    for answer in ({"kind": "failed", "message": tail, "type": "KeyError"}, answers[1]):
        engine, _ = made([{"error": answer}])
        engine.manifest = {**manifest, "views": []}
        served = Runtime(tmp_path / "shop", engine=engine, clock=lambda: NOW, owner="etienne")
        result = AppServer(served, STAFF).call("record_sale", {})
        (content,) = result.content
        assert isinstance(content, TextContent) and result.is_error
        told.append(content.text)
    assert told[0].startswith("The app failed with a KeyError. Nothing was changed. Error e-")
    assert told[1].startswith("The app failed. Nothing was changed. Error e-")
    assert not any("x_y" in text or "Landlord" in text for text in told)
    # The app's own failure: logged under its id, never as unexpected.
    assert "Landlord owes 1200" in caplog.text
    assert not unexpected(caplog)


def test_an_unknown_name_is_told_in_the_server_s_words() -> None:
    answer = {"error": {"kind": "unknown", "message": "there is no query named Landlord owes"}}
    engine, _ = made([answer, answer])
    with pytest.raises(UnknownName) as unknown:
        engine.query_json(OWNER, NOW, "stock", {}, {}, None)
    assert str(unknown.value) == "there is no query named stock"
    with pytest.raises(UnknownName) as unknown:
        engine.check(NOW, {})
    assert str(unknown.value) == "the sandbox doesn't know this request"


def test_the_server_and_the_local_runner_give_the_same_local_date(
    tmp_path: Path, executor: LocalExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Half past midnight in Paris, the stock example's zone, and still the day before in UTC.
    late = datetime(2026, 11, 10, 23, 30, tzinfo=UTC)
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    hosted = served_example(tmp_path / "hosted", executor, clock=lambda: late)
    local = Runtime(
        new_app(tmp_path / "local", source=APPS / "stock", lock=False),
        module_name="cliquet_test_local_stock",
        owner=OWNER.id,
        # The machine's own zone, as `cliquet serve` reads it.
        clock=lambda: late.astimezone(ZoneInfo("America/Los_Angeles")),
    )
    sale = {"sku": "TEA-SEN", "quantity": 1, "receipt": "R-0200"}
    try:
        for runtime in (hosted, local):
            assert isinstance(runtime.call(OWNER, "record_sale", sale), Committed)
            movements = (runtime.store.data_dir / "movements.csv").read_text()
            assert movements.splitlines()[-1].startswith("2026-11-11,TEA-SEN,-1,sale,")
    finally:
        hosted.close()
        local.close()
