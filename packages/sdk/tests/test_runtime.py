import base64
import itertools
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from cliquet import Caller
from cliquet import transactions as tx
from cliquet.errors import AppError, AppFailed, Denied, Refused
from cliquet.manifest import load_app
from cliquet.runtime import (
    CHANGED,
    EVERYONE,
    Committed,
    Everyone,
    InvalidArguments,
    OldData,
    Refusal,
    Runtime,
    data_version,
)
from cliquet.store import GitStore, Moved, StoreError, scratch_repository
from cliquet.testing import given
from cliquet.transactions import NoSuchTransaction, TransactionEnded
from cliquet.worker import Worker, handle

NOW = datetime(2026, 11, 10, 18, 30, tzinfo=ZoneInfo("Europe/Paris"))
# When the first commit of a test's app is made, outside Cliquet.
STARTED = datetime(2026, 11, 1, 9, 0, tzinfo=ZoneInfo("Europe/Paris"))
OWNER = Caller("sam", "owner")

# The test apps, as conftest names them: a file with a `# /// script` block in a string
# is a script to ty, which then can't import conftest.
APPS = Path(__file__).parent / "apps"


@contextmanager
def cut_off_write(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """A write that stops once HEAD moved, before the files on disk and the index follow it."""

    def stop(self: GitStore, changes: dict[str, bytes | None], entries: bytes) -> None:
        raise KeyboardInterrupt("stopped once HEAD moved")

    with monkeypatch.context() as patched:
        patched.setattr(GitStore, "_mirror", stop)
        yield


def after(when: datetime) -> str:
    """The second after `when`, as `since`: after every commit dated `when`, which counts as
    made since `when` itself."""
    return (when + timedelta(seconds=1)).isoformat()


READER = Caller("alex", "reader")
PUBLISH = "Publish\n\nApp-Action: publish\nApp-Caller: sam\nApp-Version: 0123abcd\n"
# An app whose actions return what they did.
TALLY = Path(__file__).parent / "tally_app.py"
# Read-only data is read-only by its file modes, which root ignores. CI runs as a user.
skip_as_root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")

LOG_APP = """\
# /// script
# dependencies = ["cliquet-sdk"]
# ///
\"\"\"A log of lines, appended by an agent.\"\"\"

from datetime import date
from pathlib import Path
from typing import Annotated

from cliquet import ActionContext, App, Field, InvariantContext, QueryContext, Refused

log = App("log", files=["log.txt", "extra/notes.txt"], roles=["owner", "reader"])


@log.action(roles=["owner"])
def append(ctx: ActionContext, line: str) -> None:
    \"\"\"Append a line.\"\"\"
    if not line:
        raise Refused("Empty line")
    with open("log.txt", "a") as f:
        f.write(line + "\\n")


@log.action(roles=["owner"])
def rewrite(ctx: ActionContext, text: str) -> None:
    \"\"\"Replace the whole log.\"\"\"
    Path("log.txt").write_text(text)


@log.action(roles=["owner"])
def stray(ctx: ActionContext) -> None:
    \"\"\"Write a file the app doesn't declare.\"\"\"
    Path("other.txt").write_text("x")


@log.action(roles=["owner"])
def dated(ctx: ActionContext, day_: Annotated[date, Field(alias="day")]) -> None:
    \"\"\"Append a date and the time of the call.\"\"\"
    with open("log.txt", "a") as f:
        f.write(f"{day_.isoformat()} {ctx.now.isoformat()}\\n")


@log.action(roles=["owner"])
def note(ctx: ActionContext, text: str) -> None:
    \"\"\"Replace the note, in a subfolder.\"\"\"
    Path("extra").mkdir(exist_ok=True)
    Path("extra/notes.txt").write_text(text)


@log.invariant("No line says forbidden")
def no_forbidden(ctx: InvariantContext) -> list[str]:
    lines = Path("log.txt").read_text().splitlines()
    return [f"line {i}" for i, line in enumerate(lines, 1) if line == "forbidden"]


@log.invariant("Lines are only appended")
def append_only(ctx: InvariantContext) -> list[str]:
    before = ctx.before / "log.txt"
    if before.exists() and not Path("log.txt").read_text().startswith(before.read_text()):
        return ["Existing lines were changed"]
    return []


@log.invariant("No line says boom")
def no_boom(ctx: InvariantContext) -> list[str]:
    if "boom" in Path("log.txt").read_text():
        raise RuntimeError("it went boom")
    return []


@log.query(roles=["owner", "reader"])
def lines(ctx: QueryContext) -> list[str]:
    \"\"\"All lines.\"\"\"
    return Path("log.txt").read_text().splitlines()


@log.query(roles=["owner", "reader"])
def line(ctx: QueryContext, number: int) -> str:
    \"\"\"One line, counting from 1.\"\"\"
    lines = Path("log.txt").read_text().splitlines()
    if not 1 <= number <= len(lines):
        raise Refused(f"There is no line {number}: the log has {len(lines)}")
    return lines[number - 1]


@log.query(roles=["owner"])
def scribble(ctx: QueryContext) -> int:
    \"\"\"Try to write while reading.\"\"\"
    Path("log.txt").write_text("x")
    return 0
"""

_names = itertools.count()
GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@localhost",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@localhost",
}


def git(root: Path, *args: str, when: datetime | None = None) -> str:
    """Run git in `root`; with `when`, a commit it makes is dated then."""
    stamp = {} if when is None else {"DATE": when.isoformat()}
    dates = {f"GIT_{who}_{k}": v for k, v in stamp.items() for who in ("AUTHOR", "COMMITTER")}
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=root,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV, **dates},
        check=True,
    )
    return result.stdout


def git_init(folder: Path) -> None:
    """Start a repository that ignores the bytecode Python writes when it loads an app."""
    git(folder, "init", "-q")
    with (folder / ".git" / "info" / "exclude").open("a") as exclude:
        exclude.write("__pycache__/\n")


def make_app(
    app_dir: Path,
    source: str,
    data: dict[str, str],
    repo_root: Path | None = None,
    owner: str | Everyone | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Runtime:
    """Write an app and its data, commit them in a new repository, and serve it.

    `owner` is the caller id of the app's owner: None makes nobody the owner, and
    `EVERYONE` every caller. The first commit is dated `STARTED`; the runtime's clock is
    `clock`, by default `NOW`."""
    (app_dir / "data").mkdir(parents=True)
    (app_dir / "app.py").write_text(source)
    for name, text in data.items():
        (app_dir / "data" / name).parent.mkdir(parents=True, exist_ok=True)
        (app_dir / "data" / name).write_text(text)
    root = repo_root or app_dir
    git_init(root)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "start", when=STARTED)
    return Runtime(
        app_dir,
        module_name=f"cliquet_runtime_{next(_names)}",
        clock=clock or (lambda: NOW),
        owner=owner,
    )


@pytest.fixture
def rt(tmp_path: Path) -> Runtime:
    # Every caller is the owner, as under `cliquet serve`: these tests read the whole history.
    return make_app(tmp_path / "log", LOG_APP, {"log.txt": "start\n"}, owner=EVERYONE)


def data(rt: Runtime, name: str = "log.txt") -> str:
    return (rt.store.data_dir / name).read_text()


def test_an_action_commits_its_change(rt: Runtime) -> None:
    outcome = rt.call(OWNER, "append", {"line": "hello"})
    assert isinstance(outcome, Committed)
    assert outcome.files == ["log.txt"]
    assert outcome.commit == rt.store.head()
    assert data(rt) == "start\nhello\n"
    (entry, _) = rt.history(OWNER)
    assert (entry.action, entry.caller, entry.role, entry.files) == (
        "append",
        "sam",
        "owner",
        ["log.txt"],
    )
    assert datetime.fromisoformat(entry.time) == NOW
    assert entry.subject == "append"
    assert rt.store.dirty() == []


def test_a_change_that_changes_nothing_is_not_committed(rt: Runtime) -> None:
    head = rt.store.head()
    assert rt.call(OWNER, "rewrite", {"text": "start\n"}) == Committed(None, [])
    assert rt.store.head() == head


def test_the_action_can_refuse(rt: Runtime) -> None:
    head = rt.store.head()
    assert rt.call(OWNER, "append", {"line": ""}) == Refusal(message="Empty line")
    assert rt.store.head() == head


def test_an_invariant_refuses_and_nothing_is_kept(rt: Runtime) -> None:
    head = rt.store.head()
    outcome = rt.call(OWNER, "append", {"line": "forbidden"})
    assert isinstance(outcome, Refusal)
    assert outcome.invariants == {"no_forbidden": ["line 2"]}
    assert "Refused by: No line says forbidden" in str(outcome)
    assert rt.store.head() == head
    assert data(rt) == "start\n"


def test_every_failing_invariant_is_listed(rt: Runtime) -> None:
    outcome = rt.call(OWNER, "rewrite", {"text": "forbidden\n"})
    assert isinstance(outcome, Refusal)
    assert set(outcome.invariants) == {"no_forbidden", "append_only"}


def test_an_invariant_that_raises_is_the_app_failing(rt: Runtime) -> None:
    head = rt.store.head()
    with pytest.raises(AppFailed) as failed:
        rt.call(OWNER, "append", {"line": "boom"})
    assert str(failed.value) == "RuntimeError: it went boom, in the invariant no_boom"
    assert failed.value.kind == "RuntimeError"
    assert isinstance(failed.value.__cause__, RuntimeError)
    assert rt.store.head() == head
    assert data(rt) == "start\n"


def test_undeclared_files_are_refused(rt: Runtime) -> None:
    outcome = rt.call(OWNER, "stray", {})
    assert isinstance(outcome, Refusal)
    assert outcome.message is not None and "other.txt" in outcome.message


def test_declared_files_can_be_in_subfolders(rt: Runtime) -> None:
    assert rt.call(OWNER, "note", {"text": "hi"}) == Committed(rt.store.head(), ["extra/notes.txt"])
    assert data(rt, "extra/notes.txt") == "hi"


def test_arguments_are_validated_by_public_or_python_name(rt: Runtime) -> None:
    assert isinstance(rt.call(OWNER, "dated", {"day": "2026-11-09"}), Committed)
    assert isinstance(rt.call(OWNER, "dated", {"day_": date(2026, 11, 8)}), Committed)
    assert data(rt).splitlines()[1] == f"2026-11-09 {NOW.astimezone(UTC).isoformat()}"
    with pytest.raises(InvalidArguments):
        rt.call(OWNER, "dated", {"day": "not a date"})
    with pytest.raises(InvalidArguments):
        rt.call(OWNER, "append", {"line": "x", "extra": 1})


def test_an_unknown_name_is_invalid_arguments(rt: Runtime) -> None:
    # Cliquet's own sentence, which every caller reads: not a KeyError, as the app's code raises.
    with pytest.raises(InvalidArguments, match="there is no action 'nope'"):
        rt.call(OWNER, "nope", {})
    with pytest.raises(InvalidArguments, match="there is no query 'nope'"):
        rt.query_json(OWNER, "nope", {})
    with pytest.raises(InvalidArguments, match="there is no query 'nope'"):
        rt.query_json(READER, "nope", {})
    with pytest.raises(InvalidArguments, match="there is no view 'nope'"):
        rt.view(OWNER, "nope")


def test_roles_are_checked_before_any_app_code(rt: Runtime) -> None:
    with pytest.raises(Denied):
        rt.call(READER, "append", {"line": "x"})
    with pytest.raises(Denied):
        rt.query_json(READER, "scribble", {})
    with pytest.raises(Denied):
        rt.call(Caller("eve", "admin"), "append", {"line": "x"})
    assert rt.query_json(READER, "lines", {}) == ["start"]


@skip_as_root
def test_queries_read_the_last_commit_read_only(rt: Runtime) -> None:
    rt.call(OWNER, "append", {"line": "hello"})
    assert rt.query_json(OWNER, "lines", {}) == ["start", "hello"]
    with pytest.raises(AppFailed, match="PermissionError") as failed:
        rt.query_json(OWNER, "scribble", {})
    assert isinstance(failed.value.__cause__, PermissionError)
    assert rt.query_json(OWNER, "lines", {}) == ["start", "hello"]


def test_changes_since(rt: Runtime) -> None:
    rt.call(OWNER, "append", {"line": "a"})
    second = rt.call(OWNER, "append", {"line": "b"})
    assert isinstance(second, Committed)
    answer = rt.changes_since(READER, "lines", {}, (NOW - timedelta(minutes=1)).isoformat())
    assert (answer["added"], answer["removed"], answer["now"]) == (["a", "b"], [], second.commit)
    after_start = rt.history(READER, since=after(STARTED))
    assert [e.action for e in after_start] == ["append", "append"]
    assert after_start[0].commit == second.commit
    with pytest.raises(InvalidArguments):
        rt.changes_since(READER, "lines", {}, "nope")


def test_revert_undoes_a_change(rt: Runtime) -> None:
    outcome = rt.call(OWNER, "note", {"text": "first"})
    rt.call(OWNER, "note", {"text": "second"})
    head = rt.store.head()
    assert head is not None
    reverted = rt.revert(OWNER, head)
    assert isinstance(reverted, Committed)
    assert data(rt, "extra/notes.txt") == "first"
    latest = rt.history(OWNER)[0]
    assert (latest.action, latest.reverts) == ("app_revert", head)
    assert isinstance(outcome, Committed)
    # It is stored as Cliquet always stored an undo: `revert`, with its trailer.
    assert isinstance(reverted, Committed) and reverted.commit is not None
    stored = git(rt.store.root, "log", "-1", "--format=%s|%(trailers:key=App-Action)")
    assert stored.strip() == "revert|App-Action: revert"


def test_an_undo_retried_with_its_request_id_answers_its_commit(rt: Runtime) -> None:
    rt.call(OWNER, "note", {"text": "first"})
    second = rt.call(OWNER, "note", {"text": "second"})
    assert isinstance(second, Committed) and second.commit is not None
    undone = rt.revert(OWNER, second.commit, request_id="undo-1")
    assert isinstance(undone, Committed) and not undone.replayed
    again = rt.revert(OWNER, second.commit, request_id="undo-1")
    assert again == Committed(undone.commit, undone.files, replayed=True)
    # The same id for an action is someone else's call.
    with pytest.raises(InvalidArguments, match="was already used by sam for app_revert"):
        rt.call(OWNER, "note", {"text": "third"}, request_id="undo-1")


def test_revert_keeps_later_changes(rt: Runtime) -> None:
    rt.call(OWNER, "note", {"text": "1\n2\n3\n4\n5\n"})
    early = rt.call(OWNER, "note", {"text": "1x\n2\n3\n4\n5\n"})
    rt.call(OWNER, "note", {"text": "1x\n2\n3\n4\n5x\n"})
    assert isinstance(early, Committed) and early.commit is not None
    assert isinstance(rt.revert(OWNER, early.commit), Committed)
    assert data(rt, "extra/notes.txt") == "1\n2\n3\n4\n5x\n"


def test_revert_refuses_when_later_changes_conflict(rt: Runtime) -> None:
    early = rt.call(OWNER, "note", {"text": "first"})
    rt.call(OWNER, "note", {"text": "second"})
    assert isinstance(early, Committed) and early.commit is not None
    outcome = rt.revert(OWNER, early.commit)
    assert isinstance(outcome, Refusal)
    assert outcome.message is not None and "can't be undone" in outcome.message


def test_revert_goes_through_the_invariants(rt: Runtime) -> None:
    rt.call(OWNER, "append", {"line": "hello"})
    head = rt.store.head()
    assert head is not None
    outcome = rt.revert(OWNER, head)
    assert isinstance(outcome, Refusal)
    assert set(outcome.invariants) == {"append_only"}


def test_revert_needs_the_role_of_the_original_action(rt: Runtime) -> None:
    rt.call(OWNER, "note", {"text": "x"})
    head = rt.store.head()
    assert head is not None
    with pytest.raises(Denied):
        rt.revert(READER, head)


UNDO_APP = """\
# /// script
# dependencies = ["cliquet-sdk"]
# ///
\"\"\"Notes, with an action of the app's own named revert.\"\"\"

from pathlib import Path

from cliquet import ActionContext, App

notes = App("notes", files=["notes.txt"], roles=["owner", "clerk"])


@notes.action(roles=["owner", "clerk"])
def write(ctx: ActionContext, text: str) -> None:
    \"\"\"Replace the notes.\"\"\"
    Path("notes.txt").write_text(text)


@notes.action(roles=["owner"])
def revert(ctx: ActionContext) -> None:
    \"\"\"Empty the notes.\"\"\"
    Path("notes.txt").write_text("")
"""


# An undo is stored as `revert`, and reads as `app_revert` by its `App-Reverts` trailer,
# whatever its label. An app may have an action of that name, or of any other.
@pytest.mark.parametrize("earlier", ["revert", "undo"])
def test_an_undo_reads_as_cliquet_s_whatever_its_label(tmp_path: Path, earlier: str) -> None:
    app = UNDO_APP.replace("def revert(", f"def {earlier}(")
    rt = make_app(tmp_path / "notes", app, {"notes.txt": "start"}, owner="sam")
    clerk, owner = Caller("cam", "clerk"), Caller("sam", "owner")
    first = rt.call(clerk, "write", {"text": "first"})
    assert isinstance(first, Committed) and first.commit is not None
    trailers = f"App-Caller: cam\nApp-Role: clerk\nApp-Reverts: {first.commit}\n"
    message = f"{earlier}\n\nApp-Action: {earlier}\n{trailers}"
    old = rt.store.commit(first.commit, {"notes.txt": b"start"}, message, "cliquet", NOW)
    assert rt.history(clerk)[0].action == "app_revert"
    # So undoing it takes a role that writes, not the role of the app's own action.
    assert isinstance(rt.revert(clerk, old), Committed)
    assert data(rt, "notes.txt") == "first"
    # A commit of the app's own action keeps its name, and undoing it takes its role.
    own = rt.call(owner, earlier, {})
    assert isinstance(own, Committed) and own.commit is not None
    assert (rt.history(owner)[0].action, rt.history(owner)[0].reverts) == (earlier, None)
    with pytest.raises(Denied):
        rt.revert(Caller("sam", "clerk"), own.commit)
    rt.close()


# A publish is stored as `publish`, as the server writes it, and reads as `app_publish` by its
# `App-Version` trailer, so an app may have an action of that name too.
def test_a_publish_reads_as_cliquet_s_beside_the_app_s_own_action_named_publish(
    tmp_path: Path,
) -> None:
    app = UNDO_APP.replace("def revert(", "def publish(")
    rt = make_app(tmp_path / "notes", app, {"notes.txt": "start"}, owner="sam")
    owner, clerk = Caller("sam", "owner"), Caller("sam", "clerk")
    base = rt.store.head()
    published = rt.store.commit(base, {"notes.txt": b"migrated"}, PUBLISH, "sam", NOW)
    own = rt.call(owner, "publish", {})
    assert isinstance(own, Committed) and own.commit is not None

    newest, publish, *_ = rt.history(owner)

    assert (newest.commit, newest.action, newest.version) == (own.commit, "publish", None)
    assert (publish.commit, publish.action, publish.version) == (
        published,
        "app_publish",
        "0123abcd",
    )
    # Undoing the app's own action takes its role. Undoing a publish's change to the data takes a
    # role that writes, as an undo does.
    with pytest.raises(Denied):
        rt.revert(clerk, own.commit)
    assert isinstance(rt.revert(owner, own.commit), Committed)
    assert isinstance(rt.revert(clerk, published), Committed)
    assert data(rt, "notes.txt") == "start"
    rt.close()


NAMED_APP = """\
# /// script
# dependencies = ["cliquet-sdk"]
# ///
\"\"\"Notes, with actions named as Cliquet's own runs once were.\"\"\"

from pathlib import Path

from cliquet import ActionContext, App, InvariantContext

notes = App("notes", files=["notes.txt"])


@notes.action
def check(ctx: ActionContext) -> None:
    \"\"\"Mark the notes.\"\"\"
    Path("notes.txt").write_text("marked")


@notes.action
def migrate(ctx: ActionContext) -> None:
    \"\"\"Mark the notes.\"\"\"
    Path("notes.txt").write_text("marked")


@notes.action
def change(ctx: ActionContext) -> None:
    \"\"\"Mark the notes.\"\"\"
    Path("notes.txt").write_text("marked")


@notes.invariant("The notes are never marked")
def unmarked(ctx: InvariantContext) -> list[str]:
    return [f"marked by {ctx.action}"] if Path("notes.txt").read_text() == "marked" else []
"""
MARKING = """\
from pathlib import Path


def migrate() -> None:
    Path("notes.txt").write_text("marked")
"""


# What Cliquet does itself has a name no action can have, so an invariant tells a check, a
# migration and a test's edit from an app's own action named check, migrate or change.
def test_cliquet_s_own_runs_have_names_no_action_can_have(tmp_path: Path) -> None:
    folder = tmp_path / "notes"
    rt = make_app(folder, NAMED_APP, {"notes.txt": "marked"})
    (folder / "migrate.py").write_text(MARKING)
    # A migration's files, as the server sends them.
    plain = {"notes.txt": base64.b64encode(b"plain").decode()}
    sam = {"caller": {"id": "sam", "role": None}, "now": NOW.isoformat()}

    refused = {}
    try:
        for name in ("check", "migrate", "change"):
            outcome = rt.call(Caller("sam", None), name, {})
            assert isinstance(outcome, Refusal)
            refused[name] = outcome.invariants
        checked = rt.check()
        assert isinstance(rt.engine, Worker)
        migrated = handle(rt.engine, folder, {**sam, "op": "migrate", "files": plain})
    finally:
        rt.close()
    module = load_app(folder / "app.py", f"cliquet_named_{next(_names)}").module
    edited = given(module.notes, {"notes.txt": ""}, now=NOW).change("notes.txt", append="marked")

    assert refused == {name: {"unmarked": [f"marked by {name}"]} for name in refused}
    assert checked is not None and checked.invariants == {"unmarked": ["marked by app_check"]}
    # A migration's change is kept by the publish, and checked as it.
    assert migrated["refusal"]["invariants"] == {"unmarked": ["marked by app_publish"]}
    assert edited.expect_refused(by=module.unmarked) == ["marked by app_change"]


def test_writes_refuse_changes_not_made_by_cliquet(rt: Runtime) -> None:
    (rt.store.data_dir / "log.txt").write_text("edited by hand\n")
    with pytest.raises(StoreError, match="not made by Cliquet"):
        rt.call(OWNER, "append", {"line": "x"})


def test_writes_are_serialized(rt: Runtime) -> None:
    def write(i: int) -> None:
        assert isinstance(rt.call(OWNER, "append", {"line": f"line {i}"}), Committed)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(data(rt).splitlines()[1:]) == sorted(f"line {i}" for i in range(8))
    assert len(rt.history(OWNER)) == 9


def test_check_runs_the_invariants_on_the_current_data(tmp_path: Path) -> None:
    assert make_app(tmp_path / "ok", LOG_APP, {"log.txt": "start\n"}).check() is None
    bad = make_app(tmp_path / "bad", LOG_APP, {"log.txt": "forbidden\n"})
    refusal = bad.check()
    assert refusal is not None and set(refusal.invariants) == {"no_forbidden"}


def test_only_the_data_folder_is_committed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "readme.md").write_text("hello\n")
    rt = make_app(root / "apps" / "log", LOG_APP, {"log.txt": "start\n"}, repo_root=root)
    (root / "readme.md").write_text("edited, not committed\n")
    git(root, "add", "readme.md")
    assert isinstance(rt.call(OWNER, "append", {"line": "x"}), Committed)
    assert git(root, "show", "--name-only", "--format=", "HEAD").split() == [
        "apps/log/data/log.txt"
    ]
    assert git(root, "status", "--porcelain").splitlines() == ["M  readme.md"]


def test_an_app_without_roles_lets_anyone_in(tmp_path: Path) -> None:
    source = LOG_APP.replace(', roles=["owner", "reader"]', "")
    source = source.replace('(roles=["owner"])', "").replace('(roles=["owner", "reader"])', "")
    rt = make_app(tmp_path / "open", source, {"log.txt": "start\n"})
    assert isinstance(rt.call(Caller("anyone", None), "append", {"line": "x"}), Committed)


def stock_app(tmp_path: Path) -> Runtime:
    """The stock example, on its own data, in a new repository."""
    app_dir = tmp_path / "shop"
    shutil.copytree(APPS / "stock" / "data", app_dir / "data")
    source = (APPS / "stock" / "app.py").read_text()
    files = {p.name: p.read_text() for p in (app_dir / "data").iterdir()}
    shutil.rmtree(app_dir / "data")
    return make_app(app_dir, source, files)


def test_the_stock_example(tmp_path: Path) -> None:
    rt = stock_app(tmp_path)
    staff = Caller("till", "staff")
    sale = {"sku": "TEA-SEN", "quantity": 3, "receipt": "R-0100"}
    assert isinstance(rt.call(staff, "record_sale", sale), Committed)
    levels = {s["sku"]: s["quantity"] for s in rt.query_json(staff, "stock", {})}
    assert levels["TEA-SEN"] == 8
    too_many = {"sku": "POT-GLS", "quantity": 5, "receipt": "R-0101"}
    outcome = rt.call(staff, "record_sale", too_many)
    assert isinstance(outcome, Refusal) and set(outcome.invariants) == {"never_negative"}
    delivery = {"invoice": "LI-200", "lines": [{"sku": "POT-GLS", "quantity": 4}]}
    assert isinstance(rt.call(staff, "receive_delivery", delivery), Committed)
    with pytest.raises(Denied):
        rt.call(staff, "set_price", {"sku": "POT-GLS", "price": Decimal("20")})
    assert rt.check() is None
    rt.close()


def test_a_commit_made_meanwhile_is_kept(rt: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    """Someone commits to the data between the runtime's read and its commit."""
    real_read = GitStore.read
    raced: list[bool] = []

    def read_then_someone_commits(
        self: GitStore, commit: str | None, files: tuple[str, ...]
    ) -> dict[str, bytes]:
        contents = real_read(self, commit, files)
        if commit is not None and not raced:
            raced.append(True)
            path = self.data_dir / "log.txt"
            path.write_text(path.read_text() + "pulled\n")
            git(self.root, "commit", "-qam", "pulled from elsewhere")
        return contents

    monkeypatch.setattr(GitStore, "read", read_then_someone_commits)
    assert isinstance(rt.call(OWNER, "append", {"line": "mine"}), Committed)
    assert data(rt) == "start\npulled\nmine\n"
    assert rt.store.dirty() == []


def test_a_write_gives_up_if_others_keep_committing(
    rt: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_read = GitStore.read

    def read_then_someone_commits(
        self: GitStore, commit: str | None, files: tuple[str, ...]
    ) -> dict[str, bytes]:
        contents = real_read(self, commit, files)
        if commit is not None:
            path = self.data_dir / "log.txt"
            path.write_text(path.read_text() + "again\n")
            git(self.root, "commit", "-qam", "busy")
        return contents

    monkeypatch.setattr(GitStore, "read", read_then_someone_commits)
    with pytest.raises(Moved):
        rt.call(OWNER, "append", {"line": "mine"})


def test_a_write_cut_off_after_its_commit_is_finished(rt: Runtime) -> None:
    assert isinstance(rt.call(OWNER, "append", {"line": "one"}), Committed)
    # As if the process stopped after moving HEAD, before updating the disk and index: the
    # record still names the commit those two matched.
    entry = f"100644,{blob('start\n')},{rt.store.prefix}/log.txt"
    before = git(rt.store.root, "rev-parse", "HEAD^").strip()
    git(rt.store.root, "update-ref", rt.store.record_ref, before)
    git(rt.store.root, "update-index", "--cacheinfo", entry)
    (rt.store.data_dir / "log.txt").write_text("start\n")
    assert rt.store.dirty() == ["log.txt"]
    assert isinstance(rt.call(OWNER, "append", {"line": "two"}), Committed)
    assert data(rt) == "start\none\ntwo\n"
    assert rt.store.dirty() == []


def test_a_publish_beyond_a_cut_off_write_leaves_the_files_to_cliquet(
    rt: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write cut off after its commit, then a publish, as the server makes one: a fresh
    store, committing straight onto HEAD. The publish finishes the write first, so HEAD never
    moves past files Cliquet still has to write."""
    assert isinstance(rt.call(OWNER, "append", {"line": "one"}), Committed)
    with cut_off_write(monkeypatch), pytest.raises(KeyboardInterrupt):
        rt.call(OWNER, "append", {"line": "two"})
    assert data(rt) == "start\none\n" and rt.store.dirty() == ["log.txt"]
    source = (rt.store.root / "app.py").read_bytes()
    published = GitStore(rt.store.root).commit(
        rt.store.head(), {"app.py": source}, PUBLISH, "sam", NOW
    )
    # The publish put the data where its commit left it, and the repository's record says so.
    assert data(rt) == "start\none\ntwo\n" and rt.store.dirty() == []
    assert GitStore(rt.store.root).recorded() == published
    assert isinstance(rt.call(OWNER, "append", {"line": "three"}), Committed)
    assert data(rt) == "start\none\ntwo\nthree\n"
    assert rt.store.dirty() == []
    rt.close()


def test_the_first_commit_of_a_new_repository(tmp_path: Path) -> None:
    app_dir = tmp_path / "fresh"
    (app_dir / "data").mkdir(parents=True)
    (app_dir / "app.py").write_text(LOG_APP)
    git_init(app_dir)
    rt = Runtime(app_dir, module_name=f"cliquet_runtime_{next(_names)}", clock=lambda: NOW)
    assert rt.store.head() is None
    outcome = rt.call(OWNER, "append", {"line": "first"})
    assert isinstance(outcome, Committed) and outcome.commit == rt.store.head()
    assert git(app_dir, "show", "--name-only", "--format=", "HEAD").split() == ["data/log.txt"]
    assert git(app_dir, "status", "--porcelain").splitlines() == ["?? app.py"]


def test_each_commit_names_the_app_py_that_checked_it(rt: Runtime) -> None:
    rt.call(OWNER, "append", {"line": "x"})
    (entry, _) = rt.history(OWNER)
    app_py = rt.store.data_dir.parent / "app.py"
    assert entry.app == git(rt.store.root, "hash-object", str(app_py)).strip()


def blob(text: str) -> str:
    result = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], input=text, capture_output=True, text=True
    )
    return result.stdout.strip()


def test_a_retried_request_returns_the_first_outcome(rt: Runtime) -> None:
    first = rt.call(OWNER, "append", {"line": "once"}, request_id="req-1")
    again = rt.call(OWNER, "append", {"line": "once"}, request_id="req-1")
    assert isinstance(first, Committed)
    assert again == Committed(first.commit, first.files, replayed=True)
    assert data(rt) == "start\nonce\n"
    assert rt.history(OWNER)[0].request == "req-1"


def test_a_request_id_belongs_to_one_call(rt: Runtime) -> None:
    rt.call(OWNER, "append", {"line": "once"}, request_id="req-1")
    with pytest.raises(InvalidArguments, match="already used"):
        rt.call(OWNER, "note", {"text": "x"}, request_id="req-1")
    with pytest.raises(InvalidArguments, match="already used"):
        rt.call(Caller("other", "owner"), "append", {"line": "once"}, request_id="req-1")
    with pytest.raises(InvalidArguments, match="request_id must be"):
        rt.call(OWNER, "append", {"line": "x"}, request_id="has spaces")


def test_a_refused_request_runs_again_when_retried(rt: Runtime) -> None:
    assert isinstance(rt.call(OWNER, "append", {"line": ""}, request_id="r"), Refusal)
    assert isinstance(rt.call(OWNER, "append", {"line": "fixed"}, request_id="r"), Committed)


def test_request_ids_match_exactly(rt: Runtime) -> None:
    rt.call(OWNER, "append", {"line": "a"}, request_id="req-10")
    assert isinstance(rt.call(OWNER, "append", {"line": "b"}, request_id="req-1"), Committed)
    assert data(rt) == "start\na\nb\n"
    # A final newline isn't dropped: kept without it, the id would never be found again,
    # and a retry would run twice.
    for wrong in ("req-2\n", "\nreq-2", "req 2"):
        with pytest.raises(InvalidArguments, match="app_request_id must be"):
            rt.call(OWNER, "append", {"line": "never"}, request_id=wrong)
    assert data(rt) == "start\na\nb\n"


def test_a_remark_is_kept_with_its_change(rt: Runtime) -> None:
    noted = rt.call(OWNER, "note", {"text": "x"}, remark="  Asked by  Alex, at breakfast ")
    assert isinstance(noted, Committed) and noted.commit is not None
    (entry,) = rt.history(OWNER, limit=1)
    assert entry.remark == "Asked by Alex, at breakfast"
    body = git(rt.store.root, "log", "-1", "--format=%B")
    assert "App-Remark: Asked by Alex, at breakfast\n" in body
    # One line, at most 200 characters: nothing runs otherwise.
    wrong: list[Any] = ["two\nlines", "a tab\there", "x" * 201, 3]
    for remark in wrong:
        with pytest.raises(InvalidArguments, match="app_remark must be"):
            rt.call(OWNER, "append", {"line": "never"}, remark=remark)
    assert "never" not in data(rt)
    rt.call(OWNER, "append", {"line": "plain"}, remark="   ")
    assert rt.history(OWNER, limit=1)[0].remark is None
    # An undo carries its own, and each call of a transaction its own.
    undone = rt.revert(OWNER, noted.commit, remark="The note was a mistake")
    assert isinstance(undone, Committed)
    assert rt.history(OWNER, limit=1)[0].remark == "The note was a mistake"
    t = rt.begin(OWNER)
    rt.call(OWNER, "append", {"line": "one"}, transaction=t, remark="first")
    rt.call(OWNER, "append", {"line": "two"}, transaction=t)
    assert isinstance(rt.commit_transaction(OWNER, t), Committed)
    two, one = rt.history(OWNER, limit=2)
    assert (one.remark, two.remark) == ("first", None)
    # The arguments the transaction's record keeps are the app's only.
    (record,) = tx.Records(rt.store.git_dir).every()
    assert "first" not in json.dumps(record.calls)


def test_the_agent_is_kept_with_its_change(rt: Runtime) -> None:
    rt.call(OWNER, "append", {"line": "one"}, agent="Morning routine")
    (entry,) = rt.history(OWNER, limit=1)
    assert entry.agent == "Morning routine"
    body = git(rt.store.root, "log", "-1", "--format=%B")
    assert "App-Agent: Morning routine\n" in body
    # A call with no token names none.
    noted = rt.call(OWNER, "note", {"text": "x"})
    assert isinstance(noted, Committed) and noted.commit is not None
    assert rt.history(OWNER, limit=1)[0].agent is None
    assert "App-Agent" not in git(rt.store.root, "log", "-1", "--format=%B")
    # An undo keeps the token that undid, and each call of a transaction its own.
    rt.revert(OWNER, noted.commit, agent="Phone")
    assert rt.history(OWNER, limit=1)[0].agent == "Phone"
    t = rt.begin(OWNER)
    rt.call(OWNER, "append", {"line": "two"}, transaction=t, agent="Phone")
    rt.call(OWNER, "append", {"line": "three"}, transaction=t)
    assert isinstance(rt.commit_transaction(OWNER, t), Committed)
    three, two = rt.history(OWNER, limit=2)
    assert (two.agent, three.agent) == ("Phone", None)


@pytest.fixture
def tally(tmp_path: Path) -> Iterator[Runtime]:
    runtime = make_app(tmp_path / "tally", TALLY.read_text(), {"amounts.txt": "1\n"})
    yield runtime
    runtime.close()


def test_committed_has_the_result(tally: Runtime) -> None:
    outcome = tally.call(OWNER, "add", {"amounts": ["2.50"]})
    assert outcome == Committed(
        tally.store.head(), ["amounts.txt"], {"added": 1, "total": "3.50", "day": "2026-11-10"}
    )
    assert isinstance(outcome, Committed) and not outcome.replayed


def test_a_result_without_a_change(tally: Runtime) -> None:
    head = tally.store.head()
    assert tally.call(OWNER, "count", {}) == Committed(None, [], 1)
    assert tally.store.head() == head


def test_a_refusal_has_no_result(tally: Runtime) -> None:
    head = tally.store.head()
    by_invariant = tally.call(OWNER, "add", {"amounts": ["-1"]})
    assert isinstance(by_invariant, Refusal) and "positive" in by_invariant.invariants
    by_action = tally.call(OWNER, "add", {"amounts": []})
    assert by_action == Refusal(message="Give at least one amount")
    assert not hasattr(by_action, "result")
    assert tally.store.head() == head


def test_a_result_that_does_not_fit_keeps_nothing(tally: Runtime) -> None:
    head = tally.store.head()
    with pytest.raises(AppError, match="doesn't match `-> Added`"):
        tally.call(OWNER, "wrong", {})
    assert tally.store.head() == head
    assert data(tally, "amounts.txt") == "1\n"


def test_a_replay_says_the_result_was_given_once(tally: Runtime) -> None:
    first = tally.call(OWNER, "add", {"amounts": ["2"]}, request_id="req-1")
    assert isinstance(first, Committed) and first.result is not None
    again = tally.call(OWNER, "add", {"amounts": ["2"]}, request_id="req-1")
    assert again == Committed(first.commit, ["amounts.txt"], None, replayed=True)
    assert data(tally, "amounts.txt") == "1\n2\n"


# An app whose queries' results have public names that differ from the Python ones.
DAYS_APP = """\
# /// script
# dependencies = ["cliquet-sdk"]
# ///
\"\"\"Days noted, read back by their public names.\"\"\"

from datetime import date
from pathlib import Path
from typing import Any

from cliquet import ActionContext, App, BaseModel, Field, QueryContext

days = App("days", files=["days.txt"], roles=["owner", "reader"])


class Day(BaseModel):
    day_: date = Field(alias="day")
    note: str


class Latest(BaseModel):
    # A public name, which callers' JSON is read back by.
    day_: date = Field(alias="day")


def _days() -> list[Day]:
    lines = Path("days.txt").read_text().splitlines()
    return [Day(day=date.fromisoformat(line[:10]), note=line[11:]) for line in lines]


@days.action(roles=["owner"])
def note(ctx: ActionContext, text: str) -> None:
    \"\"\"Note something today.\"\"\"
    with open("days.txt", "a") as f:
        f.write(f"{ctx.now.date().isoformat()} {text}\\n")


@days.query(roles=["owner", "reader"])
def noted(ctx: QueryContext) -> list[Day]:
    \"\"\"Every day noted.\"\"\"
    return _days()


@days.query(roles=["owner", "reader"])
def latest(ctx: QueryContext) -> Latest:
    \"\"\"The last day noted.\"\"\"
    return Latest(day=_days()[-1].day_)


@days.query(roles=["owner", "reader"])
def count(ctx: QueryContext) -> int:
    \"\"\"How many days are noted, but the word "none" for none: not an int.\"\"\"
    found: Any = len(_days()) or "none"
    return found
"""


def test_a_query_answers_by_alias_as_its_output_schema(tmp_path: Path) -> None:
    rt = make_app(tmp_path / "days", DAYS_APP, {"days.txt": "2026-11-09 rain\n"})
    schemas = {q["name"]: q["output_schema"] for q in rt.manifest["queries"]}
    assert rt.query_json(OWNER, "noted", {}) == [{"day": "2026-11-09", "note": "rain"}]
    assert set(schemas["noted"]["$defs"]["Day"]["properties"]) == {"day", "note"}
    assert rt.query_json(OWNER, "latest", {}) == {"day": "2026-11-09"}
    assert set(schemas["latest"]["properties"]) == {"day"}
    rt.close()


def test_a_query_result_that_does_not_fit_fails_the_call(tmp_path: Path) -> None:
    rt = make_app(tmp_path / "days", DAYS_APP, {"days.txt": ""})
    with pytest.raises(AppError) as error:
        rt.query_json(READER, "count", {})
    assert [(p.where, p.message) for p in error.value.problems] == [
        ("query count", "count returned 'none', which doesn't match `-> int`")
    ]
    # On the data of a time it doesn't fit, it is old data, as when the query fails there.
    rt.call(OWNER, "note", {"text": "rain"})
    assert rt.query_json(READER, "count", {}) == 1
    with pytest.raises(OldData) as old:
        rt.changes_since(READER, "count", {}, after(STARTED))
    told = f"count can't run on the data of {after(STARTED)}. Ask about a later time or version."
    assert str(old.value) == told
    assert isinstance(old.value.__cause__, AppError)
    rt.close()


def test_query_json_matches_the_output_schema(tmp_path: Path) -> None:
    app_dir = tmp_path / "shop"
    shutil.copytree(APPS / "stock" / "data", app_dir / "data")
    files = {p.name: p.read_text() for p in (app_dir / "data").iterdir()}
    shutil.rmtree(app_dir / "data")
    rt = make_app(app_dir, (APPS / "stock" / "app.py").read_text(), files)
    sales = rt.query_json(Caller("o", "owner"), "sales", {"month": "2026-11"})
    assert {
        "sku": "TEA-SEN",
        "name": "Sencha green tea 100 g",
        "quantity": 12,
        "revenue": "102.00",
    } in sales


@pytest.mark.parametrize("name", ["/tmp/x", "../x"])
def test_commit_refuses_a_bad_name(tmp_path: Path, name: str) -> None:
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    (root / "data" / "a.csv").write_text("a\n")
    git_init(root)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "start")
    store = GitStore(root / "data")
    head = store.head()
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    target = Path(name) if name.startswith("/") else None
    with pytest.raises(StoreError, match="data folder"):
        store.commit(head, {name: b"written\n"}, "bad", "sam", NOW)
    assert store.head() == head
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before
    assert target is None or not target.exists() or target.read_bytes() != b"written\n"
    blob = subprocess.run(
        ["git", "hash-object", "--stdin"], input=b"written\n", capture_output=True, check=True
    )
    found = subprocess.run(["git", "cat-file", "-e", blob.stdout.decode().strip()], cwd=root)
    assert found.returncode != 0, "the content was hashed into the repository"


def test_commit_refuses_a_symlinked_folder(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "data").mkdir(parents=True)
    (root / "data" / "a.csv").write_text("a\n")
    (root / "data" / "linked").symlink_to(outside, target_is_directory=True)
    git_init(root)
    git(root, "add", "data/a.csv")
    git(root, "commit", "-q", "-m", "start")
    store = GitStore(root / "data")
    head = store.head()
    with pytest.raises(StoreError, match="symbolic link"):
        store.commit(head, {"linked/x.csv": b"x\n"}, "bad", "sam", NOW)
    assert store.head() == head
    assert list(outside.iterdir()) == []


# Transactions


def branches(rt: Runtime) -> list[str]:
    """The transaction branches the repository holds: the data's own refs, not its record."""
    out = git(rt.store.root, "for-each-ref", "--format=%(refname)", "refs/app/transactions/")
    return out.split()


def kept(rt: Runtime) -> str:
    """The last call of the transaction HEAD kept: the second parent of the commit that
    kept it."""
    return git(rt.store.root, "rev-parse", "HEAD^2").strip()


def test_a_transaction_is_kept_on_commit(rt: Runtime) -> None:
    head = rt.store.head()
    t = rt.begin(OWNER)
    assert t.startswith("t-") and len(t) == 18
    assert isinstance(rt.call(OWNER, "append", {"line": "one"}, transaction=t), Committed)
    assert isinstance(rt.call(OWNER, "append", {"line": "two"}, transaction=t), Committed)
    assert rt.query_json(OWNER, "lines", {}, transaction=t) == ["start", "one", "two"]
    assert rt.query_json(OWNER, "lines", {}, transaction=t) == ["start", "one", "two"]
    assert rt.query_json(OWNER, "lines", {}) == ["start"]
    assert rt.store.head() == head and data(rt) == "start\n"

    done = rt.commit_transaction(OWNER, t)
    # Kept as one commit of the live history, dated now, which the answer names: its first
    # parent is the data it began from, and its second the last call.
    assert isinstance(done, Committed) and done.commit == rt.store.head()
    assert done.commits is not None and done.commits[-1] == kept(rt)
    assert git(rt.store.root, "rev-parse", "HEAD^1").strip() == head
    assert done.files == ["log.txt"] and not done.replayed
    assert data(rt) == "start\none\ntwo\n"
    assert rt.store.dirty() == []
    two, one, _ = rt.history(OWNER)
    assert (one.subject, two.subject) == ("append", "append")
    assert one.transaction == two.transaction == t
    assert done.commits == [one.commit, two.commit]
    with pytest.raises(TransactionEnded) as ended:
        rt.check_transaction(OWNER, t)
    assert str(ended.value) == (
        f"Transaction {t} was committed as 2 commits, one per call, oldest first: "
        f"{one.commit[:12]}, {two.commit[:12]}, changing log.txt. "
        f"It left the data at app_data_version {done.commit}."
    )
    assert f"App-Transaction: {t}" in git(rt.store.root, "log", "-1", "--format=%B")
    assert branches(rt) == []
    assert rt.begin(OWNER) != t


def test_rollback_keeps_nothing(rt: Runtime) -> None:
    head = rt.store.head()
    history = rt.history(OWNER)
    t = rt.begin(OWNER)
    rt.call(OWNER, "append", {"line": "one"}, transaction=t)
    assert rt.rollback(OWNER, t) is None
    assert rt.store.head() == head
    assert rt.history(OWNER) == history
    assert data(rt) == "start\n" and rt.store.dirty() == []
    assert branches(rt) == []
    with pytest.raises(InvalidArguments, match=f"Transaction {t} was rolled back."):
        rt.query_json(OWNER, "lines", {}, transaction=t)


def test_a_moved_head_refuses_the_commit(rt: Runtime) -> None:
    t = rt.begin(OWNER)
    rt.call(OWNER, "append", {"line": "mine"}, transaction=t)
    rt.call(OWNER, "note", {"text": "a note"}, transaction=t)
    rt.call(Caller("kim", "owner"), "append", {"line": "theirs"})
    head = rt.store.head()

    refused = rt.commit_transaction(OWNER, t)
    assert isinstance(refused, Refusal) and refused.message is not None
    assert refused.message.startswith(
        "The data changed since this transaction began, so nothing was kept. To keep these "
        "changes, begin a new transaction and make the calls again. If the person should see "
        "the result first, check it before you commit, or ask them."
    )
    assert 'append {"line": "mine"}' in refused.message
    assert 'note {"text": "a note"}' in refused.message
    assert rt.store.head() == head and data(rt) == "start\ntheirs\n"
    assert branches(rt) == []
    assert rt.commit_transaction(OWNER, t) == refused
    with pytest.raises(InvalidArguments) as ended:
        rt.call(OWNER, "append", {"line": "again"}, transaction=t)
    assert str(ended.value).startswith(
        f"Transaction {t} was not committed: the data changed since it began."
    )
    assert 'append {"line": "mine"}' in str(ended.value)
    assert rt.store.head() == head


def test_a_refused_call_in_a_transaction(rt: Runtime) -> None:
    t = rt.begin(OWNER)
    inside = rt.call(OWNER, "append", {"line": "forbidden"}, transaction=t)
    outside = rt.call(OWNER, "append", {"line": "forbidden"})
    assert isinstance(inside, Refusal) and inside == outside
    assert isinstance(rt.call(OWNER, "append", {"line": "fine"}, transaction=t), Committed)
    assert rt.query_json(OWNER, "lines", {}, transaction=t) == ["start", "fine"]
    assert isinstance(rt.commit_transaction(OWNER, t), Committed)
    assert data(rt) == "start\nfine\n"


def test_a_transaction_is_its_callers(rt: Runtime) -> None:
    t = rt.begin(OWNER)
    rt.call(OWNER, "append", {"line": "mine"}, transaction=t)
    other = Caller("kim", "owner")
    for transaction in (t, "t-0000000000000000", "../../HEAD", "t-" + "a" * 500, ""):
        uses = [
            lambda t=transaction: rt.call(other, "append", {"line": "x"}, transaction=t),
            lambda t=transaction: rt.query_json(other, "lines", {}, transaction=t),
            lambda t=transaction: rt.query_json(other, "lines", {}, transaction=t),
            lambda t=transaction: rt.commit_transaction(other, t),
            lambda t=transaction: rt.rollback(other, t),
        ]
        for use in uses:
            with pytest.raises(InvalidArguments) as missing:
                use()
            assert str(missing.value).startswith("There is no transaction")
            assert "sam" not in str(missing.value)
    assert rt.query_json(OWNER, "lines", {}, transaction=t) == ["start", "mine"]
    assert data(rt) == "start\n"


def test_one_open_transaction_per_caller(tmp_path: Path) -> None:
    clock = [NOW]
    rt = make_app(tmp_path / "log", LOG_APP, {"log.txt": "start\n"})
    rt._clock = lambda: clock[0]
    t = rt.begin(OWNER)
    rt.call(OWNER, "append", {"line": "one"}, transaction=t)
    clock[0] = NOW + timedelta(minutes=18)
    rt.call(OWNER, "append", {"line": "two"}, transaction=t)
    with pytest.raises(InvalidArguments) as second:
        rt.begin(OWNER)
    assert str(second.value) == (
        f"You have a transaction open in this app, {t}, begun at 18:30 CET and last changed "
        "at 18:48 CET, with 2 calls: append, append. If you began it, commit or roll it back. "
        "If you didn't, another of your agents may be using it: ask the person before you "
        "touch it. It is dropped at 19:48 CET unless a call changes it first."
    )
    assert rt.begin(Caller("kim", "owner")) != t
    rt.close()


def test_a_transaction_expires(tmp_path: Path) -> None:
    clock = [NOW]
    rt = make_app(tmp_path / "log", LOG_APP, {"log.txt": "start\n"})
    rt._clock = lambda: clock[0]
    t = rt.begin(OWNER)
    rt.call(OWNER, "append", {"line": "one"}, transaction=t)
    clock[0] = NOW + timedelta(minutes=59)
    assert rt.query_json(OWNER, "lines", {}, transaction=t) == ["start", "one"]
    clock[0] = NOW + timedelta(minutes=119)
    assert branches(rt) == [f"refs/app/transactions/{t}"]
    with pytest.raises(InvalidArguments, match=f"Transaction {t} was dropped after an hour"):
        rt.call(OWNER, "append", {"line": "two"}, transaction=t)
    assert branches(rt) == []
    assert data(rt) == "start\n"
    # Another caller's begin ends it too, and a day later the record is gone.
    t2 = rt.begin(OWNER)
    clock[0] += timedelta(hours=2)
    t3 = rt.begin(Caller("kim", "owner"))
    assert branches(rt) == [f"refs/app/transactions/{t3}"]
    with pytest.raises(InvalidArguments, match=f"Transaction {t2} was dropped"):
        rt.commit_transaction(OWNER, t2)
    clock[0] += timedelta(days=1)
    rt.begin(Caller("lee", "owner"))
    with pytest.raises(InvalidArguments, match=f"There is no transaction {t}"):
        rt.query_json(OWNER, "lines", {}, transaction=t)
    rt.close()


def test_a_retried_commit_says_what_happened(rt: Runtime) -> None:
    t = rt.begin(OWNER)
    rt.call(OWNER, "append", {"line": "one"}, transaction=t)
    first = rt.commit_transaction(OWNER, t)
    head = rt.store.head()
    again = rt.commit_transaction(OWNER, t)
    assert isinstance(first, Committed) and isinstance(again, Committed)
    assert first.commits == [kept(rt)] and first.commit == head
    assert again == Committed(first.commit, first.files, replayed=True, commits=first.commits)
    assert rt.store.head() == head and data(rt) == "start\none\n"
    with pytest.raises(InvalidArguments) as ended:
        rt.call(OWNER, "append", {"line": "x"}, transaction=t)
    assert str(ended.value) == (
        f"Transaction {t} was committed as {kept(rt)[:12]}, changing log.txt. "
        f"It left the data at app_data_version {first.commit}."
    )


def test_an_ended_transaction_never_reads_live_data(tmp_path: Path) -> None:
    clock = [NOW]
    rt = make_app(tmp_path / "log", LOG_APP, {"log.txt": "start\n"})
    rt._clock = lambda: clock[0]
    committed = rt.begin(OWNER)
    rt.call(OWNER, "append", {"line": "one"}, transaction=committed)
    rt.commit_transaction(OWNER, committed)
    rolled_back = rt.begin(OWNER)
    rt.rollback(OWNER, rolled_back)
    expired = rt.begin(OWNER)
    clock[0] += timedelta(hours=1)
    head = rt.store.head()
    for t, how in (
        (committed, "was committed as"),
        (rolled_back, "was rolled back"),
        (expired, "was dropped after an hour"),
    ):
        uses = [
            lambda t=t: rt.call(OWNER, "append", {"line": "two"}, transaction=t),
            lambda t=t: rt.query_json(OWNER, "lines", {}, transaction=t),
            lambda t=t: rt.query_json(OWNER, "lines", {}, transaction=t),
            lambda t=t: rt.rollback(OWNER, t),
        ]
        for use in uses:
            with pytest.raises(InvalidArguments, match=f"Transaction {t} {how}"):
                use()
    assert rt.store.head() == head and data(rt) == "start\none\n"
    rt.close()


def test_request_id_inside_a_transaction(rt: Runtime) -> None:
    t = rt.begin(OWNER)
    first = rt.call(OWNER, "append", {"line": "once"}, request_id="r-1", transaction=t)
    again = rt.call(OWNER, "append", {"line": "once"}, request_id="r-1", transaction=t)
    assert isinstance(first, Committed)
    assert again == Committed(first.commit, first.files, replayed=True)
    assert rt.query_json(OWNER, "lines", {}, transaction=t) == ["start", "once"]
    rt.commit_transaction(OWNER, t)
    after = rt.call(OWNER, "append", {"line": "once"}, request_id="r-1")
    assert after == Committed(first.commit, first.files, replayed=True)
    assert data(rt) == "start\nonce\n"


def test_transactions_need_a_role_that_writes(rt: Runtime) -> None:
    with pytest.raises(Denied):
        rt.begin(READER)


def test_a_view_in_a_transaction_reads_its_branch(tmp_path: Path) -> None:
    rt = stock_app(tmp_path)
    t = rt.begin(OWNER)
    rt.call(OWNER, "set_price", {"sku": "TEA-SEN", "price": "99.50"}, transaction=t)
    inside = rt.view(OWNER, "catalogue", transaction=t)
    outside = rt.view(OWNER, "catalogue")
    assert "99.50" in str(inside["body"]) and "99.50" not in str(outside["body"])
    # The screen's calls carry the transaction, so they stay in it.
    assert inside["transaction"] == t
    assert outside["transaction"] is None
    with pytest.raises(InvalidArguments, match="There is no transaction"):
        rt.view(Caller("kim", "owner"), "catalogue", transaction=t)
    rt.close()


def test_a_commit_cut_off_inside_its_advance_is_finished(
    rt: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    # HEAD moved to the commit that keeps the transaction, then a stop before the files on
    # disk follow: the next write finishes it, since that commit is Cliquet's.
    t = rt.begin(OWNER)
    call = rt.call(OWNER, "append", {"line": "one"}, transaction=t)
    assert isinstance(call, Committed)
    with cut_off_write(monkeypatch), pytest.raises(KeyboardInterrupt):
        rt.commit_transaction(OWNER, t)
    assert isinstance(rt.call(OWNER, "append", {"line": "two"}), Committed)
    assert data(rt) == "start\none\ntwo\n" and rt.store.dirty() == []
    # Its record is settled from git: the commit that kept it, now before the next one.
    again = rt.commit_transaction(OWNER, t)
    merged = git(rt.store.root, "rev-parse", "HEAD^").strip()
    assert isinstance(again, Committed) and again.replayed
    assert (again.commit, again.commits) == (merged, [call.commit])


def test_a_commit_cut_off_after_its_advance_is_kept(
    rt: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = rt.begin(OWNER)
    rt.call(OWNER, "append", {"line": "one"}, transaction=t)
    real_advance = GitStore.advance

    def advance_then_stop(self: GitStore, ref: str, base: str | None) -> None:
        real_advance(self, ref, base)
        raise KeyboardInterrupt("stopped before the record says so")

    with monkeypatch.context() as patched:
        patched.setattr(GitStore, "advance", advance_then_stop)
        with pytest.raises(KeyboardInterrupt):
            rt.commit_transaction(OWNER, t)
    again = rt.commit_transaction(OWNER, t)
    # The repair finds the commit that kept it, as the first answer would have named it.
    assert isinstance(again, Committed) and again.commit == rt.store.head()
    assert again.commits == [kept(rt)]
    assert again.files == ["log.txt"] and data(rt) == "start\none\n"
    assert branches(rt) == []


def stop_after_advance(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Make the next advance move HEAD, then stop before the record says so."""
    real_advance = GitStore.advance

    def advance_then_stop(self: GitStore, ref: str, base: str | None) -> None:
        real_advance(self, ref, base)
        raise KeyboardInterrupt("stopped before the record says so")

    monkeypatch.setattr(GitStore, "advance", advance_then_stop)
    return monkeypatch.undo


@pytest.mark.parametrize("then", ["rollback", "call", "query", "expiry", "begin"])
def test_an_advance_whose_record_was_not_marked_is_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, then: str
) -> None:
    clock = [NOW]
    rt = make_app(tmp_path / "log", LOG_APP, {"log.txt": "start\n"})
    rt._clock = lambda: clock[0]
    t = rt.begin(OWNER)
    rt.call(OWNER, "append", {"line": "one"}, transaction=t)
    undo = stop_after_advance(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        rt.commit_transaction(OWNER, t)
    undo()
    committed = f"Transaction {t} was committed as {kept(rt)[:12]}"
    if then == "begin":
        clock[0] += timedelta(hours=2)
        rt.begin(OWNER)
    else:
        if then == "expiry":
            clock[0] += timedelta(hours=2)
        uses = {
            "rollback": lambda: rt.rollback(OWNER, t),
            "call": lambda: rt.call(OWNER, "append", {"line": "x"}, transaction=t),
            "query": lambda: rt.query_json(OWNER, "lines", {}, transaction=t),
            "expiry": lambda: rt.query_json(OWNER, "lines", {}, transaction=t),
        }
        with pytest.raises(TransactionEnded, match=committed):
            uses[then]()
    again = rt.commit_transaction(OWNER, t)
    assert isinstance(again, Committed) and again.replayed and again.files == ["log.txt"]
    assert rt.query_json(OWNER, "lines", {}) == ["start", "one"]
    assert branches(rt) == [b for b in branches(rt) if not b.endswith(t)]
    rt.close()


def test_a_begin_cut_off_before_its_branch_is_put_right(
    rt: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patched:
        patched.setattr(GitStore, "branch", lambda self, ref, base: None)
        t = rt.begin(OWNER)
    assert branches(rt) == []
    assert isinstance(rt.call(OWNER, "append", {"line": "one"}, transaction=t), Committed)
    assert rt.query_json(OWNER, "lines", {}, transaction=t) == ["start", "one"]
    assert isinstance(rt.commit_transaction(OWNER, t), Committed)
    assert data(rt) == "start\none\n"


def test_reads_write_nothing_and_take_no_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [NOW]
    rt = make_app(tmp_path / "log", LOG_APP, {"log.txt": "start\n"})
    rt._clock = lambda: clock[0]
    t = rt.begin(OWNER)
    path = rt.store.git_dir / "app" / "transactions" / f"{t}.json"
    record = path.read_bytes()

    def no_lock() -> None:
        raise AssertionError("the read took the write lock")

    with monkeypatch.context() as patched:
        patched.setattr(rt, "_locked", no_lock)
        for minutes in (1, 30, 59):
            clock[0] = NOW + timedelta(minutes=minutes)
            assert rt.query_json(OWNER, "lines", {}, transaction=t) == ["start"]
            assert rt.history(OWNER, transaction=t) == rt.history(OWNER)
            rt.check_transaction(OWNER, t)
        # Reads don't keep it open: an hour after it began, it is dropped.
        clock[0] = NOW + timedelta(minutes=60)
        with pytest.raises(TransactionEnded, match="dropped after an hour without a change"):
            rt.query_json(OWNER, "lines", {}, transaction=t)
    assert path.read_bytes() == record
    assert branches(rt) == [f"refs/app/transactions/{t}"]
    # The next write settles it.
    with pytest.raises(TransactionEnded, match="dropped after an hour"):
        rt.call(OWNER, "append", {"line": "x"}, transaction=t)
    assert json.loads(path.read_text())["ended"] == tx.EXPIRED and branches(rt) == []
    rt.close()


def test_typed_answers_about_a_transaction(rt: Runtime) -> None:
    with pytest.raises(NoSuchTransaction):
        rt.query_json(OWNER, "lines", {}, transaction="t-0000000000000000")
    t = rt.begin(OWNER)
    rt.rollback(OWNER, t)
    with pytest.raises(TransactionEnded):
        rt.check_transaction(OWNER, t)
    assert issubclass(NoSuchTransaction, InvalidArguments)
    assert issubclass(TransactionEnded, InvalidArguments)


def test_a_new_version_ends_open_transactions(tmp_path: Path) -> None:
    app_dir = tmp_path / "log"
    old = make_app(app_dir, LOG_APP, {"log.txt": "start\n"})
    kim = Caller("kim", "owner")
    t1 = old.begin(OWNER)
    old.call(OWNER, "append", {"line": "one"}, transaction=t1)
    # A runtime of the same version, as after a restart, ends nothing.
    same = Runtime(app_dir, module_name=f"cliquet_runtime_{next(_names)}", clock=lambda: NOW)
    assert same.query_json(OWNER, "lines", {}, transaction=t1) == ["start", "one"]
    same.close()
    # One of another version, as `cliquet serve` started again with new code, ends the
    # transactions begun with the version before when it starts.
    (app_dir / "app.py").write_text(LOG_APP + "\n# version 2\n")
    new = Runtime(app_dir, module_name=f"cliquet_runtime_{next(_names)}", clock=lambda: NOW)
    assert branches(new) == []
    for runtime in (old, new):
        with pytest.raises(TransactionEnded) as ended:
            runtime.query_json(OWNER, "lines", {}, transaction=t1)
        assert str(ended.value) == (
            f"Transaction {t1} ended when a new version of the app was published."
        )
    # The server ends every one when it serves a version it has just published.
    t2 = new.begin(kim)
    new.call(kim, "append", {"line": "two"}, transaction=t2)
    new.end_transactions()
    with pytest.raises(TransactionEnded, match="new version of the app"):
        new.commit_transaction(kim, t2)
    assert data(new) == "start\n" and branches(new) == []
    old.close()
    new.close()


COUNT_QUERY = """

@log.query(roles=["owner", "reader"])
def count(ctx: QueryContext) -> int:
    \"\"\"How many lines there are.\"\"\"
    return len(Path("log.txt").read_text().splitlines())
"""


def test_a_runtime_takes_a_new_version_on_the_same_data(tmp_path: Path) -> None:
    app_dir = tmp_path / "log"
    rt = make_app(app_dir, LOG_APP, {"log.txt": "start\n"})
    rt.call(OWNER, "append", {"line": "one"})
    t = rt.begin(OWNER)
    rt.call(OWNER, "append", {"line": "two"}, transaction=t)
    old = rt.engine
    assert isinstance(old, Worker)
    (app_dir / "app.py").write_text(LOG_APP + COUNT_QUERY)
    new = Worker(app_dir / "app.py", f"cliquet_runtime_{next(_names)}")

    rt.new_version(new)

    assert rt.engine is new and rt.engine is not old
    assert "count" in {query["name"] for query in rt.manifest["queries"]}
    assert rt.query_json(OWNER, "count", {}) == 2
    with pytest.raises(TransactionEnded, match="new version of the app"):
        rt.commit_transaction(OWNER, t)
    assert data(rt) == "start\none\n" and branches(rt) == []
    # A commit names the new app.py, which checked it.
    rt.call(OWNER, "append", {"line": "three"})
    assert rt.history(OWNER)[0].app == git(app_dir, "hash-object", "app.py").strip()
    # The same app.py in a new engine is a new version too: its transactions end.
    t2 = rt.begin(OWNER)
    rt.new_version(Worker(app_dir / "app.py", f"cliquet_runtime_{next(_names)}"))
    with pytest.raises(TransactionEnded, match="new version of the app"):
        rt.query_json(OWNER, "lines", {}, transaction=t2)
    old.close()
    new.close()
    rt.close()


def test_a_new_version_waits_for_the_write_on_the_old_one(
    rt: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = rt.engine
    assert isinstance(old, Worker)
    running, release = threading.Event(), threading.Event()
    run_action = old.run_action

    def slow(*args: Any, **kwargs: Any) -> Any:
        running.set()
        release.wait(10)
        return run_action(*args, **kwargs)

    monkeypatch.setattr(old, "run_action", slow)
    new = Worker(rt.app_file, f"cliquet_runtime_{next(_names)}")
    outcomes: list[Any] = []
    write = threading.Thread(
        target=lambda: outcomes.append(rt.call(OWNER, "append", {"line": "a"}))
    )
    swap = threading.Thread(target=rt.new_version, args=(new,))
    write.start()
    assert running.wait(10)
    swap.start()
    swap.join(0.3)
    # The write holds the lock, on the old engine: the new version waits for it.
    assert swap.is_alive() and rt.engine is old
    release.set()
    write.join(10)
    swap.join(10)
    assert isinstance(outcomes[0], Committed)
    assert rt.engine is new and data(rt) == "start\na\n"
    old.close()


def test_a_replayed_call_the_record_missed_is_listed(
    rt: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = rt.begin(OWNER)
    real_save = tx.Records.save

    def stop(self: tx.Records, record: tx.Record) -> None:
        raise KeyboardInterrupt("stopped before the record says so")

    with monkeypatch.context() as patched:
        patched.setattr(tx.Records, "save", stop)
        with pytest.raises(KeyboardInterrupt):
            rt.call(OWNER, "append", {"line": "one"}, request_id="r-1", transaction=t)
    assert tx.Records.save is real_save
    missed = rt.transactions.records.load(t)
    assert missed is not None and missed.calls == []
    again = rt.call(OWNER, "append", {"line": "one"}, request_id="r-1", transaction=t)
    assert isinstance(again, Committed) and again.replayed
    rt.call(OWNER, "append", {"line": "one"}, request_id="r-1", transaction=t)
    record = rt.transactions.records.load(t)
    assert record is not None
    assert [(c["action"], c["commit"]) for c in record.calls] == [("append", again.commit)]
    # A replay of a commit made before the transaction began isn't the transaction's.
    rt.rollback(OWNER, t)
    rt.call(OWNER, "append", {"line": "two"}, request_id="r-2")
    t2 = rt.begin(OWNER)
    rt.call(OWNER, "append", {"line": "two"}, request_id="r-2", transaction=t2)
    record = rt.transactions.records.load(t2)
    assert record is not None and record.calls == []


def test_a_transaction_that_ends_where_it_began_names_its_files(tally: Runtime) -> None:
    t = tally.begin(OWNER)
    tally.call(OWNER, "clear", {}, transaction=t)
    tally.call(OWNER, "add", {"amounts": ["1"]}, transaction=t)
    done = tally.commit_transaction(OWNER, t)
    assert isinstance(done, Committed) and done.files == ["amounts.txt"]
    with pytest.raises(TransactionEnded, match=r"changing amounts\.txt"):
        tally.check_transaction(OWNER, t)


def test_a_call_that_fails_doesnt_keep_a_transaction_open(tmp_path: Path) -> None:
    clock = [NOW]
    rt = make_app(tmp_path / "log", LOG_APP, {"log.txt": "start\n"})
    rt._clock = lambda: clock[0]
    t = rt.begin(OWNER)
    rt.call(OWNER, "append", {"line": "one"}, request_id="r-1", transaction=t)
    path = rt.store.git_dir / "app" / "transactions" / f"{t}.json"
    record = path.read_bytes()
    clock[0] += timedelta(minutes=50)
    with pytest.raises(InvalidArguments, match="already used"):
        rt.call(OWNER, "note", {"text": "x"}, request_id="r-1", transaction=t)
    assert path.read_bytes() == record
    clock[0] += timedelta(minutes=10)
    with pytest.raises(TransactionEnded, match="dropped after an hour"):
        rt.check_transaction(OWNER, t)
    rt.close()


def test_the_hour_counts_from_the_begin_or_the_last_commit(tmp_path: Path) -> None:
    clock = [NOW]
    # The data's last commit is days old: a transaction begun on it isn't.
    rt = make_app(tmp_path / "log", LOG_APP, {"log.txt": "start\n"})
    rt._clock = lambda: clock[0]
    t = rt.begin(OWNER)
    clock[0] += timedelta(minutes=59)
    assert rt.query_json(OWNER, "lines", {}, transaction=t) == ["start"]
    rt.call(OWNER, "append", {"line": "one"}, transaction=t)
    clock[0] += timedelta(minutes=59)
    assert rt.query_json(OWNER, "lines", {}, transaction=t) == ["start", "one"]
    clock[0] += timedelta(minutes=1)
    expired = f"Transaction {t} was dropped after an hour without a change."
    with pytest.raises(TransactionEnded, match=expired):
        rt.query_json(OWNER, "lines", {}, transaction=t)
    # An ended one answers how for a day, then is forgotten, even before a write removes it.
    rt.begin(Caller("kim", "owner"))
    clock[0] += timedelta(hours=23, minutes=59)
    with pytest.raises(TransactionEnded, match=expired):
        rt.query_json(OWNER, "lines", {}, transaction=t)
    clock[0] += timedelta(minutes=1)
    with pytest.raises(NoSuchTransaction):
        rt.query_json(OWNER, "lines", {}, transaction=t)
    rt.close()


def test_calls_with_an_ended_id_leave_its_record_ended(tmp_path: Path) -> None:
    clock = [NOW]
    rt = make_app(tmp_path / "log", LOG_APP, {"log.txt": "start\n"})
    rt._clock = lambda: clock[0]
    t = rt.begin(OWNER)
    rt.rollback(OWNER, t)
    path = rt.store.git_dir / "app" / "transactions" / f"{t}.json"
    ended = json.loads(path.read_text())
    clock[0] += timedelta(minutes=5)
    for _ in range(2):
        with pytest.raises(TransactionEnded):
            rt.call(OWNER, "append", {"line": "x"}, request_id="r-1", transaction=t)
    assert json.loads(path.read_text()) == ended
    assert ended["ended"] == tx.ROLLED_BACK and data(rt) == "start\n"
    rt.close()


def test_an_ended_or_foreign_id_reads_nothing_and_takes_no_lock(
    rt: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    ended = rt.begin(OWNER)
    rt.rollback(OWNER, ended)
    open_one = rt.begin(OWNER)

    def no_lock() -> None:
        raise AssertionError("the read took the write lock")

    monkeypatch.setattr(rt, "_locked", no_lock)
    with pytest.raises(TransactionEnded, match="was rolled back"):
        rt.query_json(OWNER, "lines", {}, transaction=ended)
    for caller, transaction in ((Caller("kim", "owner"), open_one), (OWNER, "t-" + "0" * 16)):
        with pytest.raises(NoSuchTransaction):
            rt.query_json(caller, "lines", {}, transaction=transaction)


def test_an_end_cut_off_before_its_branch_is_dropped(
    rt: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = rt.begin(OWNER)
    rt.call(OWNER, "append", {"line": "one"}, transaction=t)

    def stop(self: GitStore, ref: str) -> None:
        raise KeyboardInterrupt("stopped before the branch is dropped")

    with monkeypatch.context() as patched:
        patched.setattr(GitStore, "drop", stop)
        with pytest.raises(KeyboardInterrupt):
            rt.rollback(OWNER, t)
    branch = f"refs/app/transactions/{t}"
    assert branches(rt) == [branch]
    # A read says how it ended, and leaves the branch: the next write drops it.
    with pytest.raises(TransactionEnded, match="was rolled back"):
        rt.query_json(OWNER, "lines", {}, transaction=t)
    assert branches(rt) == [branch]
    rt.begin(OWNER)
    assert branch not in branches(rt)
    assert data(rt) == "start\n"


def test_a_read_after_a_begin_cut_off_before_its_branch(
    rt: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patched:
        patched.setattr(GitStore, "branch", lambda self, ref, base: None)
        t = rt.begin(OWNER)
    assert rt.query_json(OWNER, "lines", {}, transaction=t) == ["start"]
    assert rt.history(OWNER, transaction=t) == rt.history(OWNER)
    assert branches(rt) == []
    assert isinstance(rt.call(OWNER, "append", {"line": "one"}, transaction=t), Committed)
    assert rt.query_json(OWNER, "lines", {}, transaction=t) == ["start", "one"]


def test_a_record_keeps_the_calls_arguments_while_they_fit(
    rt: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tx, "SHOWN", 40)
    t = rt.begin(OWNER)
    for line in ("one", "x" * 30, "two"):
        rt.call(OWNER, "append", {"line": line}, transaction=t)
    path = rt.store.git_dir / "app" / "transactions" / f"{t}.json"
    record = json.loads(path.read_text())
    # Whose it is, its calls and how it ended: what it showed is counted from its calls.
    kept = {"id", "caller", "base", "app", "began", "calls", "ended", "ended_at", "commit"}
    assert set(record) == kept
    # Once a call's arguments don't fit, no later call keeps its own, though they'd fit.
    assert ["arguments" in call for call in record["calls"]] == [True, False, False]
    rt.call(OWNER, "append", {"line": "outside"})
    moved = rt.commit_transaction(OWNER, t)
    assert isinstance(moved, Refusal) and moved.message is not None
    assert moved.message.endswith(
        'It made these calls:\n- append {"line": "one"}\n'
        "Then, without their arguments: append, append."
    )


def test_the_history_tools_in_a_transaction(tmp_path: Path) -> None:
    clock = [NOW]
    rt = make_app(tmp_path / "log", LOG_APP, {"log.txt": "start\n"}, owner=OWNER.id)
    rt._clock = lambda: clock[0]
    rt.call(OWNER, "append", {"line": "before"})
    began = NOW + timedelta(minutes=1)
    clock[0] = began
    t = rt.begin(OWNER)
    clock[0] += timedelta(minutes=1)
    one = rt.call(OWNER, "append", {"line": "one"}, transaction=t)
    two = rt.call(OWNER, "note", {"text": "a note"}, transaction=t)
    assert isinstance(one, Committed) and isinstance(two, Committed)
    assert one.commit is not None and two.commit is not None
    # history reads the branch: the transaction's calls, then what it began from.
    inside = rt.history(OWNER, transaction=t)
    assert [e.commit for e in inside[:2]] == [two.commit, one.commit]
    assert inside[2:] == rt.history(OWNER)
    since = began.isoformat()
    assert [e.commit for e in rt.history(OWNER, since=since, transaction=t)] == [
        two.commit,
        one.commit,
    ]
    # changes_since from when it began: what the transaction changes in a query.
    changed = rt.changes_since(OWNER, "lines", {}, since, transaction=t)
    assert (changed["added"], changed["removed"], changed["now"]) == (["one"], [], two.commit)
    assert rt.changes_since(OWNER, "lines", {}, since)["added"] == []
    # revert undoes one of its calls on the branch, as a call of the transaction.
    undone = rt.revert(OWNER, two.commit, transaction=t)
    assert isinstance(undone, Committed) and undone.commit is not None
    assert rt.history(OWNER, limit=1, transaction=t)[0].reverts == two.commit
    assert rt.history(OWNER, limit=1)[0].action == "append"
    assert rt.store.read(undone.commit, ("extra/notes.txt",)) == {}
    # Only what the branch's history holds: a later commit of the live data isn't.
    live = rt.call(OWNER, "append", {"line": "live"})
    assert isinstance(live, Committed) and live.commit is not None
    told = "is not in this app's history. Call app_history for the commits you may undo."
    with pytest.raises(StoreError, match=told):
        rt.revert(OWNER, live.commit, transaction=t)
    # The calls to make again name what the revert undid.
    refused = rt.commit_transaction(OWNER, t)
    assert isinstance(refused, Refusal) and refused.message is not None
    assert f'revert {{"commit": "{two.commit}"}}' in refused.message
    rt.close()


# What a caller sees

NOTES_APP = """\
# /// script
# dependencies = ["cliquet-sdk"]
# ///
\"\"\"Two notes, written by people with roles.\"\"\"

from pathlib import Path

from cliquet import ActionContext, App, QueryContext

notes = App("notes", files=["a.txt", "b.txt"], roles=["owner", "writer", "reader"])


@notes.action(roles=["owner", "writer"])
def put(ctx: ActionContext, name: str, text: str) -> None:
    \"\"\"Write one note.\"\"\"
    Path(name).write_text(text)


@notes.query(roles=["owner", "writer", "reader"])
def read(ctx: QueryContext, name: str) -> str:
    \"\"\"Read one note.\"\"\"
    return Path(name).read_text()
"""
# The app's owner is sam, whatever role sam acts with. pat is a member, even with the role
# named owner.
SAM = Caller("sam", "owner")
PAT = Caller("pat", "owner")
ALEX = Caller("alex", "reader")


def notes_app(tmp_path: Path, owner: str | Everyone | None = "sam") -> Runtime:
    return make_app(tmp_path / "notes", NOTES_APP, {"a.txt": "", "b.txt": ""}, owner=owner)


def put(rt: Runtime, caller: Caller, name: str, text: str) -> str:
    outcome = rt.call(caller, "put", {"name": name, "text": text})
    assert isinstance(outcome, Committed) and outcome.commit is not None
    return outcome.commit


def test_history_shows_your_own_changes(tmp_path: Path) -> None:
    rt = notes_app(tmp_path)
    mine = put(rt, PAT, "a.txt", "1")
    theirs = put(rt, SAM, "b.txt", "2")
    assert rt.is_owner(SAM) and rt.is_owner(Caller("sam", "reader"))
    assert not rt.is_owner(PAT)
    assert [e.commit for e in rt.history(PAT)] == [mine]
    assert rt.history(ALEX) == []
    # The owner sees every change, the first commit made outside Cliquet too.
    everything = [e.commit for e in rt.history(SAM)]
    assert everything[:2] == [theirs, mine] and len(everything) == 3
    assert [e.commit for e in rt.history(Caller("sam", "reader"), limit=1)] == [theirs]


def test_history_reaches_back_for_your_changes(tmp_path: Path) -> None:
    rt = notes_app(tmp_path)
    # "pa" is a prefix of "pat": only pa's own changes count.
    pa = Caller("pa", "writer")
    first = put(rt, pa, "a.txt", "first")
    second = put(rt, pa, "a.txt", "second")
    for n in range(40):
        put(rt, PAT if n % 2 else SAM, "b.txt", str(n))
    assert [e.commit for e in rt.history(pa, limit=2)] == [second, first]
    assert [e.commit for e in rt.history(pa, limit=1)] == [second]
    assert len(rt.history(PAT, limit=15)) == 15
    assert {e.caller for e in rt.history(PAT, limit=50)} == {"pat"}
    assert len(rt.history(PAT, limit=50)) == 20


def test_changes_since_another_callers_commit(tmp_path: Path) -> None:
    clock = Clock(T0)
    rt = make_app(
        tmp_path / "notes", NOTES_APP, {"a.txt": "", "b.txt": ""}, owner="sam", clock=clock
    )
    put(rt, PAT, "a.txt", "1")
    clock.now = T2
    theirs = put(rt, SAM, "b.txt", "2")
    later = put(rt, PAT, "a.txt", "3")
    since = T1.isoformat()
    assert [e.commit for e in rt.history(PAT, since=since)] == [later]
    assert [e.commit for e in rt.history(SAM, since=since)] == [later, theirs]
    # What changed in someone else's note, only through a query the member may use.
    answer = rt.changes_since(PAT, "read", {"name": "b.txt"}, since)
    assert (answer["removed"], answer["added"]) == ([""], ["2"])
    # Or a version: a commit's full id names exactly the data after it, the member's own or
    # not, as a query's app_data_version does.
    assert [e.commit for e in rt.history(SAM, since=theirs)] == [later]
    answer = rt.changes_since(PAT, "read", {"name": "a.txt"}, theirs)
    assert (answer["since"], answer["then"], answer["removed"], answer["added"]) == (
        theirs,
        theirs,
        ["1"],
        ["3"],
    )
    # Never another name git reads: they would let a member search anyone's commits, even
    # when they name the member's own change.
    for name in (theirs[:7], theirs.upper(), "HEAD", ":/put", "HEAD^{/put}", "main", f"-{later}"):
        with pytest.raises(InvalidArguments, match="since must be a time"):
            rt.changes_since(PAT, "read", {"name": "a.txt"}, name)
        with pytest.raises(InvalidArguments, match="since must be a time"):
            rt.history(PAT, since=name)
    # A commit made outside Cliquet, such as the first one, is no one's.
    assert rt.history(PAT, since="2020-01-01T00:00:00+00:00")[-1].caller == "pat"


def test_you_revert_your_own_changes(tmp_path: Path) -> None:
    rt = notes_app(tmp_path)
    writer = Caller("pat", "writer")
    mine = put(rt, writer, "a.txt", "1")
    theirs = put(rt, SAM, "b.txt", "2")
    # The history is checked first: a role the app doesn't have may not use it at all.
    with pytest.raises(Denied, match="is not a role of this app"):
        rt.revert(Caller("pat", "boss"), mine)
    for commit in (theirs, theirs[:7], "0" * 40, "nope", "HEAD", ":/1"):
        with pytest.raises(Denied, match=r"^You can only undo your own changes\.$"):
            rt.revert(writer, commit)
    assert rt.query_json(writer, "read", {"name": "b.txt"}) == "2"
    assert isinstance(rt.revert(writer, mine), Committed)
    assert rt.query_json(writer, "read", {"name": "a.txt"}) == ""
    # Not even the owner undoes a change with a role that may not call `put`.
    again = put(rt, PAT, "a.txt", "4")
    with pytest.raises(Denied):
        rt.revert(Caller("sam", "reader"), again)
    # The owner reverts anyone's change, with a role that may call the action.
    assert isinstance(rt.revert(Caller("sam", "writer"), again), Committed)
    assert isinstance(rt.revert(SAM, theirs), Committed)
    assert rt.query_json(SAM, "read", {"name": "a.txt"}) == ""
    # A commit is named by its id, for the owner too.
    for name in ("HEAD", ":/put", "HEAD~1"):
        with pytest.raises(StoreError, match="is not in this app's history"):
            rt.revert(SAM, name)
        with pytest.raises(InvalidArguments, match="since must be a time"):
            rt.history(SAM, since=name)
    assert rt.query_json(SAM, "read", {"name": "b.txt"}) == ""


def test_a_members_transaction_through_the_history_tools(tmp_path: Path) -> None:
    rt = notes_app(tmp_path)
    writer = Caller("pat", "writer")
    theirs = put(rt, SAM, "b.txt", "2")
    t = rt.begin(writer)
    mine = rt.call(writer, "put", {"name": "a.txt", "text": "1"}, transaction=t)
    assert isinstance(mine, Committed) and mine.commit is not None
    assert [e.commit for e in rt.history(writer, transaction=t)] == [mine.commit]
    since = after(STARTED)
    answer = rt.changes_since(writer, "read", {"name": "a.txt"}, since, transaction=t)
    # The commits compared, for a member too: the data now is the transaction's.
    assert answer.pop("now") == mine.commit and answer.pop("then") != mine.commit
    assert answer == {
        "query": "read",
        "since": since,
        "added": ["1"],
        "removed": [""],
    }
    with pytest.raises(Denied, match="You can only undo your own changes"):
        rt.revert(writer, theirs, transaction=t)
    assert isinstance(rt.revert(writer, mine.commit, transaction=t), Committed)
    assert rt.query_json(writer, "read", {"name": "a.txt"}, transaction=t) == ""
    # Anyone else's transaction doesn't exist for them, the owner's included.
    uses = [
        lambda: rt.history(SAM, transaction=t),
        lambda: rt.changes_since(SAM, "read", {"name": "a.txt"}, since, transaction=t),
        lambda: rt.revert(SAM, mine.commit or "", transaction=t),
    ]
    for use in uses:
        with pytest.raises(NoSuchTransaction):
            use()


def test_no_owner_means_nobody_is(tmp_path: Path) -> None:
    rt = notes_app(tmp_path, owner=None)
    mine = put(rt, PAT, "a.txt", "1")
    theirs = put(rt, SAM, "b.txt", "2")
    assert not rt.is_owner(PAT) and not rt.is_owner(SAM) and not rt.is_owner(ALEX)
    # Each caller sees and undoes only their own changes.
    assert [e.commit for e in rt.history(PAT)] == [mine]
    assert [e.commit for e in rt.history(SAM)] == [theirs]
    assert rt.history(ALEX) == []
    assert [e.commit for e in rt.history(PAT, since=STARTED.isoformat())] == [mine]
    with pytest.raises(Denied, match=r"^You can only undo your own changes\.$"):
        rt.revert(PAT, theirs)
    with pytest.raises(Denied, match=r"^You can only undo your own changes\.$"):
        rt.revert(SAM, mine)
    # Every caller is told which commits were compared, owner or not.
    changed = rt.changes_since(SAM, "read", {"name": "a.txt"}, after(STARTED))
    assert (changed["removed"], changed["added"]) == ([""], ["1"])
    assert changed["now"] == theirs
    assert isinstance(rt.revert(PAT, mine), Committed)
    # Nor who used a request id.
    arguments = {"name": "a.txt", "text": "3"}
    assert isinstance(rt.call(PAT, "put", arguments, request_id="r-1"), Committed)
    with pytest.raises(InvalidArguments) as error:
        rt.call(SAM, "put", arguments, request_id="r-1")
    assert str(error.value) == (
        "app_request_id 'r-1' was already used. Use a new id for a new call."
    )


def test_no_caller_is_named_everyone(tmp_path: Path) -> None:
    # The owner of a live app is a caller id, a string: no id a person holds, such as the
    # login "everyone", makes every caller the owner.
    rt = notes_app(tmp_path, owner="everyone")
    theirs = put(rt, SAM, "b.txt", "2")
    assert rt.is_owner(Caller("everyone", "writer")) and not rt.is_owner(PAT)
    assert rt.history(PAT) == []
    assert [e.commit for e in rt.history(Caller("everyone", "reader"))][:1] == [theirs]


def test_everyone_is_the_owner_when_the_runtime_says_so(tmp_path: Path) -> None:
    rt = notes_app(tmp_path, owner=EVERYONE)
    mine = put(rt, PAT, "a.txt", "1")
    theirs = put(rt, SAM, "b.txt", "2")
    assert rt.is_owner(PAT) and rt.is_owner(ALEX)
    assert [e.commit for e in rt.history(ALEX)][:2] == [theirs, mine]
    assert [e.commit for e in rt.history(PAT, since=STARTED.isoformat())][:2] == [theirs, mine]
    assert isinstance(rt.revert(PAT, theirs), Committed)
    assert isinstance(rt.revert(SAM, mine), Committed)


def test_a_request_id_says_nothing_of_another_callers_change(tmp_path: Path) -> None:
    rt = notes_app(tmp_path)
    arguments = {"name": "a.txt", "text": "1"}
    assert isinstance(rt.call(SAM, "put", arguments, request_id="r-1"), Committed)
    with pytest.raises(InvalidArguments) as error:
        rt.call(PAT, "put", arguments, request_id="r-1")
    assert str(error.value) == (
        "app_request_id 'r-1' was already used. Use a new id for a new call."
    )
    other = {"name": "b.txt", "text": "2"}
    assert put_outcome(rt.call(PAT, "put", other, request_id="r-2"))
    # The owner is told whose it was.
    with pytest.raises(InvalidArguments, match="already used by pat for put"):
        rt.call(Caller("sam", "writer"), "put", other, request_id="r-2")


def put_outcome(outcome: object) -> bool:
    return isinstance(outcome, Committed) and outcome.commit is not None


def test_a_caller_is_known_by_the_id_their_commits_hold(tmp_path: Path) -> None:
    rt = notes_app(tmp_path)
    spaced = Caller("pat  lee", "writer")
    mine = put(rt, spaced, "a.txt", "1")
    put(rt, SAM, "b.txt", "2")
    assert [e.commit for e in rt.history(spaced)] == [mine]
    assert rt.history(spaced, since=after(NOW)) == []
    assert [e.commit for e in rt.history(spaced, since=STARTED.isoformat())] == [mine]
    assert isinstance(rt.revert(spaced, mine), Committed)
    arguments = {"name": "b.txt", "text": "3"}
    assert put_outcome(rt.call(spaced, "put", arguments, request_id="r-9"))
    replayed = rt.call(spaced, "put", arguments, request_id="r-9")
    assert isinstance(replayed, Committed) and replayed.replayed


# changes_since through a query

ROWS_APP_CODE = """\
# /// script
# dependencies = ["cliquet-sdk"]
# ///
\"\"\"Rows, each written by someone.\"\"\"

import json
from pathlib import Path

from cliquet import ActionContext, App, QueryContext

rows_app = App("rows", files=[FILE], roles=["owner", "writer", "reader"])

STORAGE


@rows_app.action(roles=["owner", "writer"])
def put(ctx: ActionContext, key: str, value: str) -> None:
    \"\"\"Set one row.\"\"\"
    rows = {row["key"]: row for row in _rows()}
    rows[key] = {"key": key, "value": value, "by": ctx.caller.id}
    _save(list(rows.values()))


@rows_app.query(roles=["owner", "writer"])
def rows(ctx: QueryContext) -> list[dict[str, str]]:
    \"\"\"Every row.\"\"\"
    return _rows()


@rows_app.query(roles=["owner", "writer", "reader"])
def mine(ctx: QueryContext) -> list[dict[str, str]]:
    \"\"\"The caller's own rows.\"\"\"
    return [row for row in _rows() if row["by"] == ctx.caller.id]


@rows_app.query(roles=["owner", "writer", "reader"])
def maybe(ctx: QueryContext) -> list[str] | None:
    \"\"\"The keys, or None when there are no rows.\"\"\"
    return [row["key"] for row in _rows()] or None


@rows_app.query(roles=["owner", "writer", "reader"])
def count(ctx: QueryContext) -> int:
    \"\"\"How many rows there are.\"\"\"
    return len(_rows())


@rows_app.query(roles=["owner", "writer", "reader"])
def when(ctx: QueryContext) -> str:
    \"\"\"The time of the call.\"\"\"
    return ctx.now.isoformat()
"""
ROWS_TEXT = """\
def _rows() -> list[dict[str, str]]:
    path = Path("rows.txt")
    lines = path.read_text().splitlines() if path.exists() else []
    return [dict(zip(("key", "value", "by"), line.split("|"))) for line in lines if line]


def _save(rows: list[dict[str, str]]) -> None:
    lines = [f"{r['key']}|{r['value']}|{r['by']}" for r in rows]
    Path("rows.txt").write_text("".join(line + "\\n" for line in lines))"""
# The next version keeps the rows as JSON, in another file: the old data has none.
ROWS_JSON = """\
def _rows() -> list[dict[str, str]]:
    return json.loads(Path("rows.json").read_text())


def _save(rows: list[dict[str, str]]) -> None:
    Path("rows.json").write_text(json.dumps(rows))"""
ROWS_APP = ROWS_APP_CODE.replace("FILE", '"rows.txt"').replace("STORAGE", ROWS_TEXT)
ROWS_APP_2 = ROWS_APP_CODE.replace("FILE", '"rows.json"').replace("STORAGE", ROWS_JSON)
PARIS = ZoneInfo("Europe/Paris")
T0 = datetime(2026, 11, 10, 10, 0, tzinfo=PARIS)
T1 = datetime(2026, 11, 10, 11, 0, tzinfo=PARIS)
T2 = datetime(2026, 11, 10, 12, 0, tzinfo=PARIS)
KIM = Caller("kim", "writer")
WRITER = Caller("pat", "writer")


class Clock:
    """A clock a test sets; `tick` moves it on by a second at each reading."""

    def __init__(self, now: datetime, tick: bool = False) -> None:
        self.now = now
        self.tick = tick

    def __call__(self) -> datetime:
        now = self.now
        if self.tick:
            self.now += timedelta(seconds=1)
        return now


def rows_app(tmp_path: Path, clock: Clock, owner: str | None = "sam") -> Runtime:
    return make_app(tmp_path / "rows", ROWS_APP, {"rows.txt": ""}, owner=owner, clock=clock)


def row(key: str, value: str, by: str = "sam") -> dict[str, str]:
    return {"key": key, "value": value, "by": by}


def put_row(rt: Runtime, caller: Caller, key: str, value: str) -> str:
    outcome = rt.call(caller, "put", {"key": key, "value": value})
    assert isinstance(outcome, Committed) and outcome.commit is not None
    return outcome.commit


def test_changes_since_through_a_query(tmp_path: Path) -> None:
    clock = Clock(T0)
    rt = rows_app(tmp_path, clock)
    put_row(rt, SAM, "a", "1")
    then = put_row(rt, SAM, "b", "2")
    clock.now = T2
    put_row(rt, SAM, "c", "3")
    put_row(rt, SAM, "a", "1x")
    since = T1.isoformat()
    answer = rt.changes_since(SAM, "rows", {}, since)
    assert answer == {
        "query": "rows",
        "since": since,
        "then": then,
        "now": rt.store.head(),
        # A changed row is one removed item and one added item.
        "added": [row("a", "1x"), row("c", "3")],
        "removed": [row("a", "1")],
    }
    # Any other result: the old value and the new one, when they differ.
    counted = rt.changes_since(SAM, "count", {}, since)
    assert (counted["removed"], counted["added"]) == ([2], [3])
    # Nothing since HEAD: the query runs once.
    runs = []
    query_json = rt.engine.query_json

    def counted(*args: Any) -> Any:
        runs.append(args)
        return query_json(*args)

    rt.engine.query_json = counted  # ty: ignore[invalid-assignment]
    later = rt.changes_since(SAM, "count", {}, after(T2))
    assert (later["then"], later["removed"], later["added"]) == (rt.store.head(), [], [])
    assert len(runs) == 1
    rt.engine.query_json = query_json  # ty: ignore[invalid-assignment]
    # Both runs read the same ctx.now, so only the data differs.
    clock.tick = True
    timed = rt.changes_since(SAM, "when", {}, since)
    assert (timed["removed"], timed["added"]) == ([], [])
    # A time in another zone, or in UTC with Z, names the same moment.
    utc = rt.changes_since(SAM, "rows", {}, "2026-11-10T10:00:00Z")
    assert utc["then"] == then and utc["since"] == "2026-11-10T10:00:00+00:00"


def test_changes_since_when_the_clock_went_back(tmp_path: Path) -> None:
    clock = Clock(T0)
    rt = rows_app(tmp_path, clock)
    before = put_row(rt, SAM, "a", "1")
    clock.now = T2
    late = put_row(rt, SAM, "b", "2")
    # The clock goes back: this commit is dated before the one it follows.
    clock.now = T1
    back = put_row(rt, SAM, "c", "3")
    # Since a time between the two dates: both were made after it, in the history's order.
    since = (T1 + timedelta(minutes=30)).isoformat()
    answer = rt.changes_since(SAM, "rows", {}, since)
    assert (answer["then"], answer["now"]) == (before, back)
    assert answer["added"] == [row("b", "2"), row("c", "3")]
    assert [e.commit for e in rt.history(SAM, since=since)] == [back, late]
    # Since a time after both dates, nothing changed.
    assert rt.changes_since(SAM, "rows", {}, after(T2))["then"] == back
    assert rt.history(SAM, since=after(T2)) == []


def test_a_change_in_the_second_of_since_counts(tmp_path: Path) -> None:
    clock = Clock(T1 + timedelta(seconds=0.2))
    rt = rows_app(tmp_path, clock)
    early = put_row(rt, SAM, "a", "1")
    asked = clock.now = T1 + timedelta(seconds=0.5)
    late = put_row(rt, SAM, "b", "2")
    # Git dates both to the second, so the one made before `since` is told again: a change
    # made at `since` or after is never missed.
    for since in (asked.isoformat(), T1.isoformat()):
        answer = rt.changes_since(SAM, "rows", {}, since)
        assert answer["added"] == [row("a", "1"), row("b", "2")]
        assert [e.commit for e in rt.history(SAM, since=since)] == [late, early]
    assert rt.changes_since(SAM, "rows", {}, after(T1))["then"] == late
    assert rt.history(SAM, since=after(T1)) == []


def test_changes_since_the_version_a_transaction_began_from(tmp_path: Path) -> None:
    # A fixed clock: everything here happens in one second.
    clock = Clock(T1 + timedelta(seconds=0.2))
    rt = rows_app(tmp_path, clock)
    before = put_row(rt, SAM, "a", "1")
    clock.now = T1 + timedelta(seconds=0.5)
    record = rt.begin_record(SAM)
    t, base = record.id, record.base
    assert base == before
    inside = rt.call(SAM, "put", {"key": "b", "value": "2"}, transaction=t)
    assert isinstance(inside, Committed)
    # From the version it began from, in the transaction: what it changes, and nothing made
    # before it began, even in the same second.
    answer = rt.changes_since(SAM, "rows", {}, base, transaction=t)
    assert (answer["then"], answer["now"]) == (before, inside.commit)
    assert (answer["added"], answer["removed"]) == ([row("b", "2")], [])
    assert [e.commit for e in rt.history(SAM, since=base, transaction=t)] == [inside.commit]
    # A time in that second can't tell the two apart: it tells the earlier change again.
    second = T1.isoformat()
    assert rt.changes_since(SAM, "rows", {}, second, transaction=t)["added"] == [
        row("a", "1"),
        row("b", "2"),
    ]
    assert len(rt.history(SAM, since=second, transaction=t)) == 2
    # Once committed, on the live data: the same version, the same change.
    assert isinstance(rt.commit_transaction(SAM, t), Committed)
    live = rt.changes_since(SAM, "rows", {}, base)
    assert (live["then"], live["now"]) == (before, rt.store.head())
    assert live["added"] == [row("b", "2")]
    assert [e.commit for e in rt.history(SAM, since=base)] == [inside.commit]


def test_a_transaction_s_changes_count_from_its_commit(tmp_path: Path) -> None:
    clock = Clock(T0)
    rt = rows_app(tmp_path, clock)
    before = put_row(rt, SAM, "a", "1")
    t = rt.begin(SAM)
    inside = rt.call(SAM, "put", {"key": "b", "value": "2"}, transaction=t)
    assert isinstance(inside, Committed)
    # Someone looks half an hour later, while the transaction is open: nothing changed.
    looked = T0 + timedelta(minutes=30)
    clock.now = looked
    assert rt.changes_since(SAM, "rows", {}, T0.isoformat())["added"] == [row("a", "1")]
    assert rt.changes_since(SAM, "rows", {}, after(T0))["then"] == before
    # It is committed later still: its call, dated T0, reaches the data now, in the commit
    # that keeps it.
    clock.now = kept = T0 + timedelta(minutes=45)
    assert isinstance(rt.commit_transaction(SAM, t), Committed)
    for since in (looked.isoformat(), kept.isoformat()):
        answer = rt.changes_since(SAM, "rows", {}, since)
        assert (answer["then"], answer["added"]) == (before, [row("b", "2")])
        assert [e.commit for e in rt.history(SAM, since=since)] == [inside.commit]
    assert rt.changes_since(SAM, "rows", {}, after(kept))["then"] == rt.store.head()
    assert rt.history(SAM, since=after(kept)) == []
    # Git holds when it was kept: without its record, as a day later, the same answers.
    for record in rt.transactions.records.folder.iterdir():
        record.unlink()
    assert rt.changes_since(SAM, "rows", {}, looked.isoformat())["then"] == before
    assert rt.changes_since(SAM, "rows", {}, after(kept))["then"] == rt.store.head()


def test_changes_since_follows_the_query(tmp_path: Path) -> None:
    clock = Clock(T0)
    rt = rows_app(tmp_path, clock)
    put_row(rt, WRITER, "p", "1")
    clock.now = T2
    put_row(rt, WRITER, "p", "2")
    put_row(rt, KIM, "k", "1")
    put_row(rt, SAM, "s", "1")
    since = T1.isoformat()
    pat = rt.changes_since(WRITER, "mine", {}, since)
    assert (pat["added"], pat["removed"]) == ([row("p", "2", "pat")], [row("p", "1", "pat")])
    # A member is told the commits compared, the same as the owner.
    owner = rt.changes_since(SAM, "mine", {}, since)
    assert (pat["then"], pat["now"]) == (owner["then"], owner["now"])
    assert pat["now"] == rt.store.head()
    kim = rt.changes_since(KIM, "mine", {}, since)
    assert (kim["added"], kim["removed"]) == ([row("k", "1", "kim")], [])
    reader = rt.changes_since(Caller("alex", "reader"), "mine", {}, since)
    assert (reader["added"], reader["removed"]) == ([], [])


def test_changes_since_needs_the_query_role(tmp_path: Path) -> None:
    rt = rows_app(tmp_path, Clock(T0))
    put_row(rt, SAM, "a", "1")
    since = T1.isoformat()
    alex = Caller("alex", "reader")

    def denied(caller: Caller, query: str) -> str:
        with pytest.raises(Denied) as error:
            rt.changes_since(caller, query, {}, since)
        return str(error.value).replace(query, "<query>", 1)

    # The same answer for a query the role may not use as for one the app lacks: a member
    # isn't told which queries the app has.
    assert denied(alex, "rows") == "<query> is not one of your queries: mine, maybe, count, when"
    assert denied(alex, "nope") == denied(alex, "rows")
    assert denied(WRITER, "nope") == denied(WRITER, "none_here")
    # The owner is told which it is.
    with pytest.raises(Denied, match="may not use the query 'rows'"):
        rt.changes_since(Caller("sam", "reader"), "rows", {}, since)
    with pytest.raises(Denied, match="is not a role of this app"):
        rt.changes_since(Caller("alex", "boss"), "mine", {}, since)
    with pytest.raises(InvalidArguments, match="no query 'nope'"):
        rt.changes_since(SAM, "nope", {}, since)
    with pytest.raises(
        InvalidArguments, match=r"count doesn't take extra\. It takes no arguments\."
    ):
        rt.changes_since(SAM, "count", {"extra": 1}, since)
    # A name and an object, or a clear refusal.
    with pytest.raises(InvalidArguments, match="query must be the name of one of your queries"):
        rt.changes_since(SAM, 3, {}, since)  # ty: ignore[invalid-argument-type]
    for odd in ([], "x", None):
        with pytest.raises(InvalidArguments, match="arguments must be an object"):
            rt.changes_since(alex, "count", odd, since)  # ty: ignore[invalid-argument-type]


def test_changes_since_takes_a_time_or_a_version(tmp_path: Path) -> None:
    rt = rows_app(tmp_path, Clock(T0))
    commit = put_row(rt, SAM, "a", "1")
    # A version of this data: its commit's full id, or git's null id before the first.
    assert rt.history(SAM, since=commit) == []
    assert [e.commit for e in rt.history(SAM, since="0" * 40)][:1] == [commit]
    assert rt.changes_since(SAM, "rows", {}, commit)["added"] == []
    # A version of other data, such as a transaction's that was rolled back, is none of it.
    t = rt.begin(SAM)
    dropped = rt.call(SAM, "put", {"key": "b", "value": "2"}, transaction=t)
    assert isinstance(dropped, Committed) and dropped.commit is not None
    rt.rollback(SAM, t)
    for unknown in ("1" * 40, dropped.commit):
        with pytest.raises(InvalidArguments, match=r"is no version of this data\."):
            rt.history(SAM, since=unknown)
        with pytest.raises(InvalidArguments, match=r"is no version of this data\."):
            rt.changes_since(SAM, "rows", {}, unknown)
    # Or a time with its zone: never another name git reads, such as `HEAD` or `:/put`.
    for since in (commit[:7], "HEAD", ":/put", "HEAD@{yesterday}", "yesterday", ""):
        with pytest.raises(InvalidArguments, match="since must be a time"):
            rt.changes_since(SAM, "rows", {}, since)
        with pytest.raises(InvalidArguments, match="since must be a time"):
            rt.history(SAM, since=since)
    for since in ("2026-11-10T11:00:00", "2026-11-10"):
        with pytest.raises(InvalidArguments, match="with its zone"):
            rt.changes_since(SAM, "rows", {}, since)


def test_changes_since_before_the_first_commit(tmp_path: Path) -> None:
    rt = rows_app(tmp_path, Clock(T0))
    put_row(rt, SAM, "a", "1")
    put_row(rt, SAM, "b", "2")
    answer = rt.changes_since(SAM, "rows", {}, "2026-10-01T00:00:00+02:00")
    assert answer["then"] is None and answer["removed"] == []
    assert answer["added"] == [row("a", "1"), row("b", "2")]
    # Before 1970 too, and early times git would read as a date without their zone.
    assert rt.changes_since(SAM, "count", {}, "1900-01-01T00:00:00+00:00")["added"] == [2]
    for early in ("1970-01-02T00:00:00Z", "1971-01-01T00:00:00Z", "1973-03-03T09:46:39Z"):
        assert rt.changes_since(SAM, "count", {}, early)["then"] is None
    # A list against None: None is no items.
    keys = rt.changes_since(SAM, "maybe", {}, "2026-10-01T00:00:00+02:00")
    assert (keys["added"], keys["removed"]) == (["a", "b"], [])
    # The first commit, made outside Cliquet, holds the empty rows.
    first = rt.changes_since(SAM, "count", {}, after(STARTED))
    assert first["then"] == rt.history(SAM)[-1].commit and first["removed"] == [0]


def test_changes_since_across_a_migration(tmp_path: Path) -> None:
    clock = Clock(T0)
    rt = rows_app(tmp_path, clock)
    put_row(rt, SAM, "a", "1")
    rt.close()
    # The next version, and its migrated data, are published at T2.
    folder = tmp_path / "rows"
    (folder / "app.py").write_text(ROWS_APP_2)
    (folder / "data" / "rows.txt").unlink()
    (folder / "data" / "rows.json").write_text(json.dumps([row("a", "1")]))
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "Publish", when=T2)
    clock.now = T2 + timedelta(hours=1)
    new = Runtime(folder, module_name=f"cliquet_runtime_{next(_names)}", clock=clock, owner="sam")
    put_row(new, SAM, "b", "2")
    with pytest.raises(OldData) as error:
        new.changes_since(SAM, "rows", {}, T1.isoformat())
    assert (
        str(error.value)
        == f"rows can't run on the data of {T1.isoformat()}. Ask about a later time or version."
    )
    assert isinstance(error.value.__cause__, AppFailed)
    assert isinstance(error.value.__cause__.__cause__, FileNotFoundError)
    published = new.changes_since(SAM, "rows", {}, after(T2))
    assert (published["added"], published["removed"]) == ([row("b", "2")], [])
    new.close()

    # With app.py outside the data's repository, as in the test harness: the same answer.
    (tmp_path / "apart").mkdir()
    data = scratch_repository(tmp_path / "apart", {})
    store = GitStore(data)
    store.commit(store.head(), {"rows.json": b"[]"}, "migrate", "sam", T2)
    apart = Runtime(
        folder / "app.py",
        data_dir=data,
        module_name=f"cliquet_runtime_{next(_names)}",
        clock=clock,
    )
    with pytest.raises(OldData) as error:
        apart.changes_since(SAM, "count", {}, T1.isoformat())
    assert (
        str(error.value)
        == f"count can't run on the data of {T1.isoformat()}. Ask about a later time or version."
    )
    apart.close()


def test_a_refusal_on_old_data_is_old_data(tmp_path: Path) -> None:
    rt = make_app(tmp_path / "log", LOG_APP, {"log.txt": "start\n"})
    rt.call(OWNER, "append", {"line": "hello"})
    with pytest.raises(OldData) as error:
        rt.changes_since(OWNER, "line", {"number": 2}, after(STARTED))
    told = f"line can't run on the data of {after(STARTED)}. Ask about a later time or version."
    assert str(error.value) == told
    assert isinstance(error.value.__cause__, Refused)


class Hosted:
    """An engine that isn't the worker in this process, as the server's isn't: `fail` is
    what its next query on the old data raises."""

    def __init__(self, worker: Any, fail: Exception) -> None:
        self.worker, self.fail = worker, fail

    def __getattr__(self, name: str) -> Any:
        return getattr(self.worker, name)

    def query_json(self, *args: Any) -> Any:
        if args[-1] is None:  # the old data
            raise self.fail
        return self.worker.query_json(*args)


def test_only_the_app_s_failure_on_old_data_is_old_data(tmp_path: Path) -> None:
    rt = rows_app(tmp_path, Clock(T0))
    put_row(rt, SAM, "a", "1")
    worker = rt.engine
    since = "2026-10-01T00:00:00+02:00"
    rt.engine = Hosted(worker, AppFailed("KeyError: 'a'"))  # ty: ignore[invalid-assignment]
    with pytest.raises(OldData):
        rt.changes_since(SAM, "rows", {}, since)
    # A fault of the sandbox, or of the server, is its own error.
    rt.engine = Hosted(worker, ConnectionError("the sandbox answered 500"))  # ty: ignore[invalid-assignment]
    with pytest.raises(ConnectionError):
        rt.changes_since(SAM, "rows", {}, since)
    rt.engine = worker


def test_history_since(tmp_path: Path) -> None:
    clock = Clock(T0)
    rt = rows_app(tmp_path, clock)
    put_row(rt, WRITER, "p", "1")
    put_row(rt, SAM, "s", "1")
    clock.now = T2
    mine = put_row(rt, WRITER, "p", "2")
    theirs = put_row(rt, SAM, "s", "2")
    since = T1.isoformat()
    assert [e.commit for e in rt.history(WRITER, since=since)] == [mine]
    assert [e.commit for e in rt.history(SAM, since=since)] == [theirs, mine]
    assert [e.commit for e in rt.history(SAM, 1, since)] == [theirs]
    assert rt.history(KIM, since=since) == []
    assert rt.history(SAM, since=after(T2)) == []
    # Before the first commit: every change the caller sees.
    assert len(rt.history(WRITER, since="2020-01-01T00:00:00+00:00")) == 2
    assert len(rt.history(SAM, since="2020-01-01T00:00:00+00:00")) == 5


def test_the_log_keeps_both_a_text_and_a_caller(tmp_path: Path) -> None:
    rt = notes_app(tmp_path)
    assert put_outcome(rt.call(PAT, "put", {"name": "a.txt", "text": "1"}, request_id="r-1"))
    assert put_outcome(rt.call(SAM, "put", {"name": "a.txt", "text": "2"}, request_id="r-1+"))
    assert [e.request for e in rt.store.log(grep="r-1", caller="sam")] == ["r-1+"]
    assert [e.request for e in rt.store.log(grep="r-1", caller="pat")] == ["r-1"]
    assert rt.store.log(grep="r-2", caller="pat") == []


# An app that tells the time it is given, in the zone it declares.
CLOCK_APP = """\
# /// script
# dependencies = ["cliquet-sdk"]
# ///
\"\"\"The time, as the app sees it.\"\"\"

from cliquet import ActionContext, App, QueryContext

clock = App("clock", files=["days.txt"]{zone})


@clock.action
def stamp(ctx: ActionContext) -> None:
    \"\"\"Note today's date.\"\"\"
    with open("days.txt", "a") as f:
        f.write(ctx.now.date().isoformat() + "\\n")


@clock.query
def now(ctx: QueryContext) -> str:
    \"\"\"The time now.\"\"\"
    return ctx.now.isoformat()
"""
# Half past midnight in Paris, still the day before in UTC.
LATE = datetime(2026, 11, 10, 23, 30, tzinfo=UTC)


def clock_app(tmp_path: Path, zone: str | None, at: datetime) -> Runtime:
    source = CLOCK_APP.format(zone="" if zone is None else f", timezone={zone!r}")
    return make_app(tmp_path / "clock", source, {"days.txt": ""}, clock=lambda: at)


def test_ctx_now_is_in_the_app_s_timezone(tmp_path: Path) -> None:
    rt = clock_app(tmp_path, "Europe/Paris", LATE)
    assert rt.manifest["timezone"] == "Europe/Paris"
    # Without a language, the app's screens are in English.
    assert rt.manifest["language"] == "en"
    assert rt.query_json(OWNER, "now", {}) == "2026-11-11T00:30:00+01:00"
    assert isinstance(rt.call(OWNER, "stamp", {}), Committed)
    assert data(rt, "days.txt") == "2026-11-11\n"


def test_the_manifest_says_the_language_of_the_app_s_screens(tmp_path: Path) -> None:
    source = CLOCK_APP.format(zone=', language="fr"')
    rt = make_app(tmp_path / "clock", source, {"days.txt": ""}, clock=lambda: LATE)
    assert rt.manifest["language"] == "fr"


def test_an_app_with_no_timezone_gets_utc(tmp_path: Path) -> None:
    rt = clock_app(tmp_path, None, LATE.astimezone(ZoneInfo("Europe/Paris")))
    assert rt.manifest["timezone"] == "UTC"
    assert rt.query_json(OWNER, "now", {}) == "2026-11-10T23:30:00+00:00"
    assert isinstance(rt.call(OWNER, "stamp", {}), Committed)
    assert data(rt, "days.txt") == "2026-11-10\n"


def test_an_unknown_timezone_is_refused_when_the_app_loads(tmp_path: Path) -> None:
    with pytest.raises(AppError, match="timezone='Paris' is not a time zone"):
        clock_app(tmp_path, "Paris", LATE)


def test_an_app_keeps_no_more_data_than_one_migration_rewrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = make_app(tmp_path / "tally", TALLY.read_text(), {"amounts.txt": ""})
    assert isinstance(rt.call(OWNER, "add", {"amounts": [str(n) for n in range(1, 41)]}), Committed)
    size = len(data(rt, "amounts.txt"))
    # The app is over the cap now: as an app was before there was one.
    monkeypatch.setattr("cliquet.limits.MAX_APP_DATA", size - 1)
    head = rt.store.head()

    refused = rt.call(OWNER, "add", {"amounts": ["7"]})

    assert isinstance(refused, Refusal)
    assert refused.message == (
        f"This change would leave the app with {size + 2:,} bytes of data, 3 bytes more than "
        f"the {size - 1:,} bytes an app keeps, so that one migration can rewrite all of it. "
        "Remove data before adding more."
    )
    assert rt.store.head() == head
    # A change that doesn't grow the data passes, so the app can get back under the cap.
    assert isinstance(rt.call(OWNER, "clear", {}), Committed)
    assert isinstance(rt.call(OWNER, "add", {"amounts": ["7"]}), Committed)


# A write that depends on what was read: `if_unchanged_since`


def version(rt: Runtime, caller: Caller, transaction: str | None = None) -> str:
    """The version of the data a query of `caller` reads, live or in a transaction."""
    return rt.query_version(caller, "read", {"name": "a.txt"}, transaction)[1]


def test_a_query_answers_with_the_version_of_its_data(tmp_path: Path) -> None:
    rt = notes_app(tmp_path)
    answer, first = rt.query_version(ALEX, "read", {"name": "a.txt"})
    # The version is the id of the commit the query read, for a role that only reads too.
    assert answer == "" and first == rt.store.head()
    # The same data, the same version, whoever reads and whatever they ask.
    assert rt.query_version(PAT, "read", {"name": "b.txt"})[1] == first
    commit = put(rt, SAM, "a.txt", "one")
    second = version(rt, ALEX)
    assert second != first and second == commit == data_version(commit)
    # Someone else's commit opens nothing to a member: undo takes only their own changes.
    with pytest.raises(Denied, match=r"^You can only undo your own changes\.$"):
        rt.revert(PAT, second)


def test_an_action_is_refused_when_the_data_changed_since(tmp_path: Path) -> None:
    rt = notes_app(tmp_path)
    read = version(rt, PAT)
    theirs = put(rt, SAM, "a.txt", "sold out")
    refused = rt.call(PAT, "put", {"name": "b.txt", "text": "order 6"}, if_unchanged_since=read)
    # Cliquet's fixed words, the same for a member and the owner: no diff, no commit.
    assert refused == Refusal(message=CHANGED)
    assert CHANGED == (
        "The data changed since you read it, so the call wasn't made. Read it again, decide "
        "again, and call with the new app_data_version."
    )
    assert rt.store.head() == theirs and data(rt, "b.txt") == ""
    # Read again: the call runs on the version it names, and that version is then old.
    again = version(rt, PAT)
    done = rt.call(PAT, "put", {"name": "b.txt", "text": "order 0"}, if_unchanged_since=again)
    assert isinstance(done, Committed) and data(rt, "b.txt") == "order 0"
    stale = rt.call(PAT, "put", {"name": "b.txt", "text": "more"}, if_unchanged_since=again)
    assert stale == Refusal(message=CHANGED)
    # Without it, or with an empty one, a call runs whatever changed.
    assert isinstance(rt.call(PAT, "put", {"name": "b.txt", "text": "any"}), Committed)
    empty = rt.call(PAT, "put", {"name": "b.txt", "text": "more"}, if_unchanged_since="")
    assert isinstance(empty, Committed)


def test_a_retried_call_answers_its_first_commit_whatever_the_version(tmp_path: Path) -> None:
    rt = notes_app(tmp_path)
    read = version(rt, PAT)
    arguments = {"name": "a.txt", "text": "one"}
    first = rt.call(PAT, "put", arguments, request_id="r-1", if_unchanged_since=read)
    assert isinstance(first, Committed) and first.commit is not None
    # Its own commit made the version old: the retry still answers that commit.
    again = rt.call(PAT, "put", arguments, request_id="r-1", if_unchanged_since=read)
    assert again == Committed(first.commit, ["a.txt"], replayed=True)
    put(rt, SAM, "b.txt", "theirs")
    for sent in (read, data_version(first.commit), version(rt, PAT), None):
        again = rt.call(PAT, "put", arguments, request_id="r-1", if_unchanged_since=sent)
        assert again == Committed(first.commit, ["a.txt"], replayed=True)
    # A new call with the old version is refused, under any request id.
    new = rt.call(PAT, "put", arguments, request_id="r-2", if_unchanged_since=read)
    assert new == Refusal(message=CHANGED)


@pytest.mark.parametrize("sent", [" ", "HEAD", ":/put", "main", "g" * 40, "0" * 39, 7])
def test_a_version_that_is_none_is_an_argument_error(tmp_path: Path, sent: Any) -> None:
    rt = notes_app(tmp_path)
    head = rt.store.head()
    assert head is not None
    # Only a full commit id, as a query gives it: git never reads a caller's version.
    for wrong in (sent, head[:7], head.upper(), f"{head}^", f"v-{head}"):
        with pytest.raises(InvalidArguments) as error:
            rt.call(PAT, "put", {"name": "a.txt", "text": "x"}, if_unchanged_since=wrong)
        assert str(error.value) == (
            "app_if_unchanged_since must be the app_data_version of a query's answer, a "
            "commit's id of 40 lower-case hexadecimal characters: call the query again and pass "
            "the one it gives"
        )
    assert rt.store.head() == head


def test_a_transaction_s_calls_are_counted_with_one_git_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = notes_app(tmp_path)
    t = rt.begin(PAT)
    for name, text in (("a.txt", "x"), ("b.txt", "x"), ("a.txt", "y")):
        put = {"name": name, "text": text}
        assert isinstance(rt.call(PAT, "put", put, transaction=t), Committed)
    started = []
    git = rt.store._git_bytes

    def counted(*args: str, **options: Any) -> bytes:
        started.append(args)
        return git(*args, **options)

    monkeypatch.setattr(rt.store, "_git_bytes", counted)
    calls = rt.transaction_calls(PAT, t)["calls"]
    assert [call["files"] for call in calls] == [
        [{"file": "a.txt", "added": 1, "removed": 0}],
        [{"file": "b.txt", "added": 1, "removed": 0}],
        [{"file": "a.txt", "added": 1, "removed": 1}],
    ]
    # One process reads the transaction, and one counts every call's changes.
    assert [args[0] for args in started] == ["for-each-ref", "diff-tree"]


def test_a_version_in_a_transaction_is_its_branch_s(tmp_path: Path) -> None:
    rt = notes_app(tmp_path)
    live = version(rt, PAT)
    t = rt.begin(PAT)
    # A transaction begins on the live data, so its version is the live one until it changes.
    assert version(rt, PAT, t) == live
    one = {"name": "a.txt", "text": "one"}
    first = rt.call(PAT, "put", one, transaction=t, if_unchanged_since=live)
    assert isinstance(first, Committed)
    inside = version(rt, PAT, t)
    assert inside != live and version(rt, PAT) == live
    # In the transaction, the call runs on its branch: the live version is old there.
    two = {"name": "b.txt", "text": "two"}
    stale = rt.call(PAT, "put", two, transaction=t, if_unchanged_since=live)
    assert stale == Refusal(message=CHANGED)
    assert [call["action"] for call in rt.transaction_calls(PAT, t)["calls"]] == ["put"]
    assert isinstance(rt.call(PAT, "put", two, transaction=t, if_unchanged_since=inside), Committed)
    # Outside it, the transaction's version is not the live data's.
    outside = rt.call(PAT, "put", two, if_unchanged_since=inside)
    assert outside == Refusal(message=CHANGED)
    assert isinstance(rt.commit_transaction(PAT, t), Committed)
    assert (data(rt, "a.txt"), data(rt, "b.txt")) == ("one", "two")


def test_a_write_that_runs_again_checks_the_version_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Someone commits between the check and the commit: the second attempt is refused."""
    rt = notes_app(tmp_path)
    read = version(rt, PAT)
    real_read = GitStore.read
    raced: list[bool] = []

    def read_then_someone_commits(
        self: GitStore, commit: str | None, files: tuple[str, ...]
    ) -> dict[str, bytes]:
        contents = real_read(self, commit, files)
        if not raced:
            raced.append(True)
            (self.data_dir / "a.txt").write_text("pulled")
            git(self.root, "commit", "-qam", "pulled from elsewhere")
        return contents

    monkeypatch.setattr(GitStore, "read", read_then_someone_commits)
    outcome = rt.call(PAT, "put", {"name": "b.txt", "text": "mine"}, if_unchanged_since=read)
    assert raced and outcome == Refusal(message=CHANGED)
    assert (data(rt, "a.txt"), data(rt, "b.txt")) == ("pulled", "")


def test_the_version_before_any_commit(tmp_path: Path) -> None:
    app_dir = tmp_path / "fresh"
    (app_dir / "data").mkdir(parents=True)
    (app_dir / "app.py").write_text(LOG_APP)
    git_init(app_dir)
    rt = Runtime(
        app_dir, module_name=f"cliquet_runtime_{next(_names)}", clock=lambda: NOW, owner="sam"
    )
    assert rt.store.head() is None
    empty = data_version(None)
    first = rt.call(OWNER, "append", {"line": "first"}, if_unchanged_since=empty)
    assert isinstance(first, Committed) and data_version(first.commit) != empty
    assert rt.query_version(READER, "lines", {})[1] == data_version(first.commit)
    late = rt.call(OWNER, "append", {"line": "second"}, if_unchanged_since=empty)
    assert late == Refusal(message=CHANGED)


def test_a_write_is_dated_when_it_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A write that waited for the lock is dated when it got it, not when it asked: dated
    # earlier, it could land after a routine's check made since that date, and be missed.
    clock = Clock(T0)
    rt = make_app(
        tmp_path / "notes", NOTES_APP, {"a.txt": "", "b.txt": ""}, owner="sam", clock=clock
    )
    held = rt._locked

    @contextmanager
    def waited() -> Generator[datetime]:
        clock.now = T2  # someone else's write held the lock until T2
        with held() as now:
            yield now

    monkeypatch.setattr(rt, "_locked", waited)
    mine = put(rt, PAT, "a.txt", "1")
    assert [e.commit for e in rt.history(PAT, since=T1.isoformat())] == [mine]
    clock.now = T0
    began = rt.begin_record(PAT).began
    assert began == T2.isoformat()


def test_history_since_and_an_undo_read_the_same_across_a_pack(rt: Runtime) -> None:
    """A full pack rewrites no history: the log, a `since` answer and an undo read as before."""
    assert isinstance(rt.call(OWNER, "note", {"text": "first"}), Committed)
    second = rt.call(OWNER, "note", {"text": "second"})
    assert isinstance(second, Committed) and second.commit is not None
    since = (NOW - timedelta(minutes=1)).isoformat()
    before_since = rt.changes_since(READER, "lines", {}, since)
    before_history = rt.history(OWNER, since=after(STARTED))
    rt.store.pack()
    assert rt.changes_since(READER, "lines", {}, since) == before_since
    assert rt.history(OWNER, since=after(STARTED)) == before_history
    undone = rt.revert(OWNER, second.commit)
    assert isinstance(undone, Committed) and undone.commit is not None
    assert data(rt, "extra/notes.txt") == "first"
    latest = rt.history(OWNER)[0]
    assert (latest.action, latest.reverts) == ("app_revert", second.commit)
    assert isinstance(rt.call(OWNER, "note", {"text": "third"}), Committed)
    assert data(rt, "extra/notes.txt") == "third"


def test_a_pack_keeps_the_record_a_cut_off_write_left(
    rt: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record refs of a write stay refs through a pack, and the commit one names stays
    readable: the next write still finishes the write that was cut off."""
    assert isinstance(rt.call(OWNER, "append", {"line": "one"}), Committed)
    with cut_off_write(monkeypatch), pytest.raises(KeyboardInterrupt):
        rt.call(OWNER, "append", {"line": "two"})
    assert rt.store.dirty() == ["log.txt"]
    recorded = rt.store.recorded()
    assert recorded is not None
    rt.store.pack()
    refs = git(rt.store.root, "for-each-ref", "--format=%(refname)", "refs/app/files")
    assert rt.store.record_ref in refs.split()
    assert rt.store.recorded() == recorded
    assert isinstance(rt.call(OWNER, "append", {"line": "three"}), Committed)
    assert data(rt) == "start\none\ntwo\nthree\n"
    assert rt.store.dirty() == []


# Data files declared by a pattern

LEDGER_APP = """\
# /// script
# dependencies = ["cliquet-sdk"]
# ///
\"\"\"A ledger kept as a file a month, beside its top file.\"\"\"

from pathlib import Path

from cliquet import ActionContext, App, InvariantContext, QueryContext

ledger = App("ledger", files=["**/*.beancount"])


@ledger.action
def record(ctx: ActionContext, month: str, line: str) -> None:
    \"\"\"Append a line to a month's file, such as 2026/02, made in its folder if need be.\"\"\"
    path = Path(f"{month}.beancount")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(line + "\\n")


@ledger.action
def forget(ctx: ActionContext, month: str) -> None:
    \"\"\"Remove a month's file.\"\"\"
    Path(f"{month}.beancount").unlink()


@ledger.action
def jot(ctx: ActionContext, name: str) -> None:
    \"\"\"Write a file by its name, whatever it is.\"\"\"
    Path(name).parent.mkdir(parents=True, exist_ok=True)
    Path(name).write_text("jotted\\n")


@ledger.invariant("No line says forbidden")
def no_forbidden(ctx: InvariantContext) -> list[str]:
    return [p.as_posix() for p in Path().rglob("*.beancount") if "forbidden" in p.read_text()]


@ledger.query
def files(ctx: QueryContext) -> list[str]:
    \"\"\"Every file the app is given.\"\"\"
    return sorted(p.as_posix() for p in Path().rglob("*") if p.is_file())
"""
KEEPER = Caller("sam", None)
LEDGER = {"ledger.beancount": "open\n", "2026/01.beancount": "january\n", "notes.txt": "mine\n"}


def ledger_app(tmp_path: Path, data: dict[str, str] = LEDGER) -> Runtime:
    """The ledger app, on `data`: a top file, a month in a folder, and a file nothing matches."""
    return make_app(tmp_path / "ledger", LEDGER_APP, data, owner=EVERYONE)


def test_an_action_adds_a_file_a_pattern_matches_in_a_new_folder(tmp_path: Path) -> None:
    rt = ledger_app(tmp_path)
    assert rt.manifest["files"] == ["**/*.beancount"]
    before = rt.store.head()
    assert rt.query_json(KEEPER, "files", {}) == ["2026/01.beancount", "ledger.beancount"]

    outcome = rt.call(KEEPER, "record", {"month": "2027/02", "line": "february"})

    assert outcome == Committed(rt.store.head(), ["2027/02.beancount"])
    assert data(rt, "2027/02.beancount") == "february\n"
    after = ["2026/01.beancount", "2027/02.beancount", "ledger.beancount"]
    assert rt.query_json(KEEPER, "files", {}) == after
    # Each commit reads the set as its own tree has it.
    assert sorted(rt.store.read_declared(rt.store.head(), rt.manifest["files"])) == after
    assert sorted(rt.store.read_declared(before, rt.manifest["files"])) == after[::2]


def test_a_committed_file_nothing_matches_is_never_given_to_the_app(tmp_path: Path) -> None:
    rt = ledger_app(tmp_path)
    assert "notes.txt" not in rt.query_json(KEEPER, "files", {})
    assert isinstance(rt.call(KEEPER, "record", {"month": "2026/01", "line": "more"}), Committed)
    # It stays in the repository as it was, and no change touched it.
    assert data(rt, "notes.txt") == "mine\n"
    assert "notes.txt" not in rt.query_json(KEEPER, "files", {})


@pytest.mark.parametrize("name", ["notes.txt", "2026/01.csv", ".cache/ledger.beancount"])
def test_a_file_no_pattern_matches_is_refused_naming_the_pattern(tmp_path: Path, name: str) -> None:
    rt = ledger_app(tmp_path)
    head = rt.store.head()
    outcome = rt.call(KEEPER, "jot", {"name": name})
    assert outcome == Refusal(
        message=f"the change left files the app doesn't declare: {name}. "
        "App(files=[...]) declares **/*.beancount: declare them there too, or remove them."
    )
    assert rt.store.head() == head


def test_changes_since_sees_a_file_the_set_gained_or_lost(tmp_path: Path) -> None:
    rt = ledger_app(tmp_path)
    start = rt.version(KEEPER)
    gained = rt.call(KEEPER, "record", {"month": "2026/02", "line": "february"})
    assert isinstance(gained, Committed) and gained.commit is not None
    answer = rt.changes_since(KEEPER, "files", {}, start)
    assert (answer["added"], answer["removed"]) == (["2026/02.beancount"], [])
    assert isinstance(rt.call(KEEPER, "forget", {"month": "2026/01"}), Committed)
    answer = rt.changes_since(KEEPER, "files", {}, gained.commit)
    assert (answer["added"], answer["removed"]) == ([], ["2026/01.beancount"])


def test_an_undo_removes_a_file_the_set_gained_and_brings_back_one_it_lost(
    tmp_path: Path,
) -> None:
    rt = ledger_app(tmp_path)
    gained = rt.call(KEEPER, "record", {"month": "2026/02", "line": "february"})
    assert isinstance(gained, Committed) and gained.commit is not None
    assert isinstance(rt.revert(KEEPER, gained.commit), Committed)
    assert not (rt.store.data_dir / "2026/02.beancount").exists()

    lost = rt.call(KEEPER, "forget", {"month": "2026/01"})
    assert isinstance(lost, Committed) and lost.commit is not None
    assert not (rt.store.data_dir / "2026/01.beancount").exists()
    assert isinstance(rt.revert(KEEPER, lost.commit), Committed)
    assert data(rt, "2026/01.beancount") == "january\n"
    assert rt.query_json(KEEPER, "files", {}) == ["2026/01.beancount", "ledger.beancount"]


def test_a_pattern_s_whole_set_counts_toward_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = ledger_app(tmp_path)
    # The files the pattern matches hold 13 bytes: notes.txt, matched by none, is out.
    monkeypatch.setattr("cliquet.limits.MAX_APP_DATA", 22)
    assert isinstance(rt.call(KEEPER, "record", {"month": "2026/02", "line": "feb"}), Committed)
    refused = rt.call(KEEPER, "record", {"month": "2026/03", "line": "march"})
    assert isinstance(refused, Refusal) and refused.message is not None
    assert refused.message.startswith("This change would leave the app with 23 bytes of data")


def test_a_pattern_that_matches_nothing_yet_starts_empty(tmp_path: Path) -> None:
    rt = ledger_app(tmp_path, {})
    assert rt.query_json(KEEPER, "files", {}) == []
    outcome = rt.call(KEEPER, "record", {"month": "2026/01", "line": "first"})
    assert outcome == Committed(rt.store.head(), ["2026/01.beancount"])
    assert rt.query_json(KEEPER, "files", {}) == ["2026/01.beancount"]


def test_an_invariant_sees_every_file_of_the_set(tmp_path: Path) -> None:
    rt = ledger_app(tmp_path)
    refused = rt.call(KEEPER, "record", {"month": "2026/q1/03", "line": "forbidden"})
    assert isinstance(refused, Refusal)
    assert refused.invariants == {"no_forbidden": ["2026/q1/03.beancount"]}
