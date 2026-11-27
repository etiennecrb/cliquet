import os
import subprocess
import sys
import textwrap
import types
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from conftest import WriteApp, module_name
from tally_app import Added, add, clear, count, tally
from tally_app import total as tally_total
from test_runtime import CLOCK_APP, DAYS_APP, after

from cliquet.errors import AppFailed
from cliquet.manifest import load_app
from cliquet.runtime import CHANGED, Committed
from cliquet.screens import text
from cliquet.testing import Denied, InvalidArguments, World, given, refused

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

NOTES = """
\"\"\"Notes and tags.\"\"\"

from pathlib import Path

from cliquet import ActionContext, App, InvariantContext, QueryContext, Refused, Screen

notes = App("notes", files=["notes.txt", "tags.txt"], roles=["owner", "reader"])


@notes.action(roles=["owner"])
def add(ctx: ActionContext, text: str, times: int = 1) -> None:
    \"\"\"Add a note.\"\"\"
    if not text:
        raise Refused("Empty note")
    with open("notes.txt", "a") as f:
        f.write((text + "\\n") * times)


@notes.invariant("No note says secret")
def no_secret(ctx: InvariantContext) -> list[str]:
    path = Path("notes.txt")
    return ["a note says secret"] if path.exists() and "secret" in path.read_text() else []


@notes.invariant("Tags are lower case")
def lower_tags(ctx: InvariantContext) -> list[str]:
    text = Path("tags.txt").read_text() if Path("tags.txt").exists() else ""
    return ["upper case tag"] if text != text.lower() else []


@notes.query(roles=["owner", "reader"])
def count(ctx: QueryContext) -> int:
    \"\"\"How many notes there are.\"\"\"
    path = Path("notes.txt")
    return len(path.read_text().splitlines()) if path.exists() else 0


BOARD = \"\"\"
<h2>{{ total }} notes</h2>
<ul class="list">{% for note in found %}<li class="list-row">{{ note }}</li>{% endfor %}</ul>
{% if owner %}
<form data-call="add" data-args='{"text": "more"}'><button class="btn">Add</button></form>
{% endif %}
\"\"\"


@notes.view(roles=["owner", "reader"])
def board(ctx: QueryContext, containing: str = "") -> Screen:
    \"\"\"The notes, on a screen.\"\"\"
    path = Path("notes.txt")
    found = [n for n in path.read_text().splitlines() if containing in n] if path.exists() else []
    return Screen(
        BOARD,
        args={"containing": containing},
        total=count(ctx),
        found=found,
        owner=ctx.caller.role == "owner",
    )
"""


def load(write_app: WriteApp, source: str = NOTES) -> Any:
    path = write_app(source)
    return load_app(path, module_name(path)).module


def world(module: Any, files: dict[str, str | bytes] | None = None) -> World:
    return given(module.notes, files if files is not None else {"notes.txt": "first\n"}, now=NOW)


def test_calls_and_queries_go_through_the_runtime(write_app: WriteApp) -> None:
    m = load(write_app)
    owner = world(m).connect("owner")
    owner.call(m.add, "second", times=2).expect_ok()
    assert owner.query(m.count) == 3
    assert owner.call(m.add, text="").expect_refused(message="Empty note") == ["Empty note"]
    refused = owner.call(m.add, text="secret").expect_refused(by=m.no_secret)
    assert refused == ["a note says secret"]


def test_a_call_can_depend_on_what_was_read(write_app: WriteApp) -> None:
    m = load(write_app)
    w = world(m)
    owner, reader = w.connect("owner"), w.connect("reader", id="alex")
    # The version a query's answer gives, for every role.
    read = owner.data_version
    assert reader.data_version == read == w.runtime.store.head()
    owner.call(m.add, "second").expect_ok()
    stale = owner.if_unchanged_since(read).call(m.add, "third")
    assert stale.expect_refused(message=CHANGED) == [CHANGED]
    assert owner.query(m.count) == 2
    owner.if_unchanged_since(owner.data_version).call(m.add, "third").expect_ok()
    # The connection it came from is unchanged, and so is a transaction's.
    owner.call(m.add, "fourth").expect_ok()
    t = owner.begin()
    assert t.data_version == owner.data_version
    t.if_unchanged_since(t.data_version).call(m.add, "fifth").expect_ok()
    assert t.data_version != owner.data_version and t.query(m.count) == 5


def test_the_first_commit_is_dated_by_the_world_s_clock() -> None:
    world = given(tally, {"amounts.txt": "1\n"}, now=NOW)
    owner = world.connect()
    owner.call(add, amounts=[Decimal("2")]).expect_ok()
    (first, *_) = reversed(world.runtime.history(owner.caller))
    assert first.time == NOW.isoformat()
    # So an app's own tests can ask what changed since a time: before `now`, there was no
    # data; from `now`, the given data and the call.
    before = datetime(2026, 11, 9, tzinfo=UTC).isoformat()
    changed = world.runtime.changes_since(owner.caller, "total", {}, before)
    assert (changed["removed"], changed["added"]) == (["0"], ["3"])
    assert len(world.runtime.history(owner.caller, since=before)) == 2
    assert len(world.runtime.history(owner.caller, since=NOW.isoformat())) == 2
    later = after(NOW)
    assert world.runtime.history(owner.caller, since=later) == []


def test_every_caller_of_a_world_is_the_owner() -> None:
    world = given(tally, {"amounts.txt": "1\n"}, now=NOW)
    sam, pat = world.connect(id="sam"), world.connect(id="pat")
    theirs = sam.call(add, amounts=[Decimal("2")])
    theirs.expect_ok()
    # A test sees every change, and may undo it, whoever made it.
    assert world.runtime.is_owner(sam.caller) and world.runtime.is_owner(pat.caller)
    assert world.runtime.history(pat.caller)[0].commit == theirs.commit
    assert "now" in world.runtime.changes_since(pat.caller, "total", {}, NOW.isoformat())
    assert theirs.commit is not None
    assert isinstance(world.runtime.revert(pat.caller, theirs.commit), Committed)
    assert pat.query(tally_total) == Decimal("1")


def test_expect_ok_returns_the_typed_result() -> None:
    owner = given(tally, {"amounts.txt": "1\n"}, now=NOW).connect()
    imported: Added = owner.call(add, amounts=[Decimal("2.5")]).expect_ok()
    assert imported == Added(added=1, total=Decimal("3.5"), day=NOW.date())
    counted: int = owner.call(count).expect_ok()
    assert counted == 2
    nothing: None = owner.call(clear).expect_ok()
    assert nothing is None
    refusal = owner.call(add, amounts=[])
    assert refusal.result is None
    refusal.expect_refused(message="Give at least one amount")


UNREBUILDABLE = """
\"\"\"Results whose JSON doesn't validate back into their model.\"\"\"

from datetime import date

from pydantic import field_validator

from cliquet import ActionContext, App, BaseModel, QueryContext

odd = App("odd", files=["x.txt"])


# Checking the app refuses what writes JSON a result doesn't read back, but not a validator
# that won't read it: the harness still says what it got.
class Stamped(BaseModel):
    day: date

    @field_validator("day", mode="before")
    @classmethod
    def _not_text(cls, value: object) -> object:
        if isinstance(value, str):
            raise ValueError("a date, never its text")
        return value


@odd.action
def stamp(ctx: ActionContext) -> Stamped:
    \"\"\"Return today's date.\"\"\"
    return Stamped(day=ctx.now.date())


@odd.action
def measure(ctx: ActionContext) -> list[Stamped]:
    \"\"\"Return today's date, in a list.\"\"\"
    return [Stamped(day=ctx.now.date())]


@odd.query
def stamped(ctx: QueryContext) -> Stamped:
    \"\"\"Today's date.\"\"\"
    return Stamped(day=ctx.now.date())
"""


def test_a_result_that_cannot_be_rebuilt_shows_its_json(write_app: WriteApp) -> None:
    m = load(write_app, UNREBUILDABLE)
    caller = given(m.odd, {}, now=NOW).connect()
    stamped = caller.call(m.stamp)  # the call itself doesn't rebuild the result
    with pytest.raises(AssertionError, match=r'stamp\'s result can\'t be rebuilt.*"2026-11-10"'):
        stamped.expect_ok()
    measured = caller.call(m.measure)
    with pytest.raises(AssertionError, match=r'measure.*\[\{"day": "2026-11-10"\}\]'):
        _ = measured.result
    with pytest.raises(
        AssertionError, match=r'from the JSON an agent gets: \{"day": "2026-11-10"\}'
    ):
        caller.query(m.stamped)


def test_the_world_s_clock_is_in_the_app_s_timezone(write_app: WriteApp) -> None:
    path = write_app(CLOCK_APP.format(zone=', timezone="America/New_York"'), header="")
    m = load_app(path, module_name(path)).module
    by_default = given(m.clock, {"days.txt": ""}).connect()
    assert datetime.fromisoformat(by_default.query(m.now)).utcoffset() in ZONE_OFFSETS
    fixed = given(m.clock, {"days.txt": ""}, now=NOW).connect()  # 12:00 UTC
    assert fixed.query(m.now) == "2026-11-10T07:00:00-05:00"


# New York is five hours behind UTC in winter, and four in summer.
ZONE_OFFSETS = (timedelta(hours=-5), timedelta(hours=-4))


def test_a_query_result_is_rebuilt_from_its_json(write_app: WriteApp) -> None:
    path = write_app(DAYS_APP, header="")
    m = load_app(path, module_name(path)).module
    owner = given(m.days, {"days.txt": "2026-11-09 rain\n"}, now=NOW).connect("owner")
    assert owner.query(m.noted) == [m.Day(day=date(2026, 11, 9), note="rain")]
    t = owner.begin()
    t.call(m.note, text="sun").expect_ok()
    assert t.query(m.noted)[1] == m.Day(day=NOW.date(), note="sun")
    # What an agent gets has the public name, which the model reads back.
    assert t.query(m.latest) == m.Latest(day=NOW.date())
    t.rollback()
    assert owner.query(m.latest) == m.Latest(day=date(2026, 11, 9))


def test_views_are_laid_out_through_the_runtime(write_app: WriteApp) -> None:
    m = load(write_app)
    w = world(m, {"notes.txt": "first\nsecond\n"})
    owner, reader = w.connect("owner"), w.connect("reader")
    assert text(reader.view(m.board)["body"]) == "2 notes first second"
    # The screen is laid out for the caller: only the owner gets the form that adds.
    assert "'data-call': 'add'" in str(owner.view(m.board))
    assert "add" in owner.view(m.board)["tools"]
    assert "add" not in reader.view(m.board)["tools"]
    # A view takes its arguments as a query does, and says how to draw it again.
    found = reader.view(m.board, containing="sec")
    assert found["arguments"] == {"containing": "sec"}
    assert text(found["body"]) == "2 notes second"
    with pytest.raises(InvalidArguments):
        reader.view(m.board, containing=3)
    with pytest.raises(TypeError, match="is not a view of notes"):
        owner.view(m.count)


def test_files_not_given_start_absent(write_app: WriteApp) -> None:
    m = load(write_app)
    owner = world(m, {}).connect("owner")
    assert owner.query(m.count) == 0
    owner.call(m.add, text="hello").expect_ok()
    assert owner.query(m.count) == 1


def test_given_refuses_files_the_app_does_not_declare(write_app: WriteApp) -> None:
    m = load(write_app)
    with pytest.raises(ValueError, match="not one of notes's files"):
        world(m, {"other.txt": "x"})


LEDGER = """
\"\"\"A ledger kept as a file a month.\"\"\"

from pathlib import Path

from cliquet import App, InvariantContext, QueryContext

ledger = App("ledger", files=["**/*.beancount"])


@ledger.invariant("No line says forbidden")
def no_forbidden(ctx: InvariantContext) -> list[str]:
    return [p.as_posix() for p in Path().rglob("*.beancount") if "forbidden" in p.read_text()]


@ledger.query
def months(ctx: QueryContext) -> list[str]:
    \"\"\"The ledger's files.\"\"\"
    return sorted(p.as_posix() for p in Path().rglob("*") if p.is_file())
"""


def test_given_takes_any_file_a_pattern_matches(write_app: WriteApp) -> None:
    m = load(write_app, LEDGER)
    w = given(m.ledger, {"ledger.beancount": "open\n", "2026/01.beancount": "jan\n"}, now=NOW)
    assert w.connect().query(m.months) == ["2026/01.beancount", "ledger.beancount"]
    with pytest.raises(ValueError, match=r"not one of ledger's files: \*\*/\*\.beancount"):
        given(m.ledger, {"2026/01.csv": "x"}, now=NOW)


def test_a_pattern_that_matches_nothing_yet_starts_empty(write_app: WriteApp) -> None:
    m = load(write_app, LEDGER)
    assert given(m.ledger, {}, now=NOW).connect().query(m.months) == []


def test_change_makes_a_file_a_pattern_matches_in_a_new_folder(write_app: WriteApp) -> None:
    m = load(write_app, LEDGER)
    w = given(m.ledger, {}, now=NOW)
    w.change("2026/02.beancount", append="feb\n").expect_ok()
    w.change("2026/q1/03.beancount", append="forbidden\n").expect_refused(by=m.no_forbidden)
    assert w.connect().query(m.months) == ["2026/02.beancount"]
    with pytest.raises(ValueError, match="not one of ledger's files"):
        w.change("notes.txt", append="x")


def test_change_bypasses_actions(write_app: WriteApp) -> None:
    m = load(write_app)
    w = world(m)
    w.change("tags.txt", append="Work\n").expect_refused(by=m.lower_tags)
    w.change("notes.txt", replace=("first", "third")).expect_ok()
    assert w.connect("reader").query(m.count) == 1


def test_roles(write_app: WriteApp) -> None:
    m = load(write_app)
    w = world(m)
    with pytest.raises(Denied):
        w.connect("reader").call(m.add, text="x")
    with pytest.raises(TypeError, match="several roles"):
        w.connect()
    with pytest.raises(ValueError, match="not a role"):
        w.connect("admin")
    single = load(write_app, NOTES.replace('roles=["owner", "reader"]', 'roles=["owner"]'))
    assert world(single).connect().caller.role == "owner"


def test_failure_messages_say_what_happened(write_app: WriteApp) -> None:
    m = load(write_app)
    owner = world(m).connect("owner")
    with pytest.raises(AssertionError, match="Refused by: No note says secret"):
        owner.call(m.add, text="secret").expect_ok()
    with pytest.raises(AssertionError, match="it was committed"):
        owner.call(m.add, text="fine").expect_refused(by=m.no_secret)
    with pytest.raises(AssertionError, match="expected a refusal by 'Tags are lower case'"):
        owner.call(m.add, text="secret").expect_refused(by=m.lower_tags)
    with pytest.raises(AssertionError, match="expected the refusal 'Other'"):
        owner.call(m.add, text="").expect_refused(message="Other")


def test_a_crash_in_the_app_s_code_keeps_its_exception(write_app: WriteApp) -> None:
    crashing = NOTES.replace(
        '        raise Refused("Empty note")', '        raise Refused("Empty note")\n    1 / times'
    )
    m = load(write_app, crashing)
    w = world(m)
    owner = w.connect("owner")
    # As an agent would be told: the app failed. The app's own exception is the cause.
    with pytest.raises(AppFailed, match="ZeroDivisionError: division by zero") as failed:
        owner.call(m.add, text="x", times=0)
    assert isinstance(failed.value.__cause__, ZeroDivisionError)
    assert failed.value.__cause__.__traceback__ is not None
    # A test's own edit isn't the app's code: its error is its own.
    with pytest.raises(AssertionError, match=r"'nothing' is not in notes\.txt"):
        w.change("notes.txt", replace=("nothing", "x"))


def test_only_functions_of_the_app_can_be_called(write_app: WriteApp) -> None:
    m = load(write_app)
    owner = world(m).connect("owner")
    with pytest.raises(TypeError, match="is not an action"):
        owner.call(m.count)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="is not a query"):
        owner.query(len)  # ty: ignore[invalid-argument-type]


def test_refusals_are_recorded_for_the_check(write_app: WriteApp) -> None:
    m = load(write_app)
    owner = world(m).connect("owner")
    owner.call(m.add, text="secret").expect_refused(by=m.no_secret)
    assert refused[str(Path(m.__file__).resolve())] == {"no_secret"}


def run_cliquet_test(app_dir: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLIQUET_IN_APP_ENV": "1"}
    return subprocess.run(
        [sys.executable, "-m", "cliquet.cli", "test", str(app_dir), "-q"],
        capture_output=True,
        text=True,
        env=env,
    )


def make_app_dir(tmp_path: Path, source: str, test: str) -> Path:
    app_dir = tmp_path / "notes"
    (app_dir / "tests").mkdir(parents=True)
    header = '# /// script\n# dependencies = ["cliquet-sdk"]\n# ///\n'
    (app_dir / "app.py").write_text(header + textwrap.dedent(source))
    (app_dir / "tests" / "test_notes.py").write_text(textwrap.dedent(test))
    return app_dir


TEST_ONE_RULE = """
from datetime import UTC, datetime
from decimal import Decimal

from app import add, no_secret, notes

from cliquet.testing import given


def test_secret() -> None:
    owner = given(notes, {}, now=datetime(2026, 11, 10, tzinfo=UTC)).connect("owner")
    owner.call(add, text="secret").expect_refused(by=no_secret)
"""


def test_cliquet_test_fails_when_an_invariant_never_refuses(tmp_path: Path) -> None:
    result = run_cliquet_test(make_app_dir(tmp_path, NOTES, TEST_ONE_RULE))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "notes: lower_tags never refused a change" in result.stdout


def test_cliquet_test_passes_when_every_invariant_refuses(tmp_path: Path) -> None:
    test = (
        TEST_ONE_RULE.replace("no_secret, notes", "lower_tags, no_secret, notes")
        + """

def test_tags() -> None:
    world = given(notes, {}, now=datetime(2026, 11, 10, tzinfo=UTC))
    world.change("tags.txt", append="Work\\n").expect_refused(by=lower_tags)
"""
    )
    result = run_cliquet_test(make_app_dir(tmp_path, NOTES, test))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "every invariant refused at least once" in result.stdout


def test_cliquet_test_reports_definition_problems(tmp_path: Path) -> None:
    source = NOTES.replace(
        '    """How many notes there are."""\n', "    import time\n    time.time()\n"
    )
    result = run_cliquet_test(make_app_dir(tmp_path, source, TEST_ONE_RULE))
    assert result.returncode == 1
    assert "reads the system clock" in result.stderr


def test_given_serves_the_module_that_defines_the_app(write_app: WriteApp) -> None:
    """Under plain pytest a test module that imported the app can come first."""
    m = load(write_app)
    importer = types.ModuleType("cliquet_test_importer")
    importer.__file__ = str(Path(m.__file__).parent / "test_importer.py")
    vars(importer)["notes"] = m.notes
    sys.modules["cliquet_test_importer"] = importer
    sys.modules[m.__name__] = sys.modules.pop(m.__name__)  # now after the importer
    try:
        w = world(m)
        assert w.runtime.app_file == Path(m.__file__).resolve()
        assert str(Path(m.__file__).resolve()) in refused
        assert str(Path(importer.__file__).resolve()) not in refused
    finally:
        del sys.modules["cliquet_test_importer"]


TEST_WITHOUT_HARNESS = """
def test_nothing() -> None:
    assert True
"""


def test_cliquet_test_fails_when_no_test_uses_the_harness(tmp_path: Path) -> None:
    result = run_cliquet_test(make_app_dir(tmp_path, NOTES, TEST_WITHOUT_HARNESS))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "notes: no_secret never refused a change" in result.stdout
    assert "notes: lower_tags never refused a change" in result.stdout


def test_a_transaction_in_the_harness(write_app: WriteApp) -> None:
    owner = given(tally, {"amounts.txt": "1\n"}, now=NOW).connect()
    t = owner.begin()
    added: Added = t.call(add, amounts=[Decimal("2")]).expect_ok()
    assert added.total == Decimal("3")
    total: Decimal = t.query(tally_total)
    assert total == Decimal("3")
    assert owner.query(tally_total) == Decimal("1")
    t.commit().expect_ok()
    assert owner.query(tally_total) == Decimal("3")
    with pytest.raises(InvalidArguments, match="was committed as"):
        t.query(tally_total)

    dropped = owner.begin()
    dropped.call(clear).expect_ok()
    assert dropped.query(tally_total) == Decimal("0")
    dropped.rollback()
    assert owner.query(tally_total) == Decimal("3")

    m = load(write_app)
    notes_owner = world(m).connect("owner")
    inside = notes_owner.begin()
    inside.call(m.add, text="secret").expect_refused(by=m.no_secret)
    assert refused[str(Path(m.__file__).resolve())] == {"no_secret"}
    inside.call(m.add, text="second").expect_ok()
    screen = inside.view(m.board)
    assert "2 notes" in text(screen["body"])
    # The screen's calls stay in the transaction.
    assert screen["transaction"] == inside.id
    outside = notes_owner.view(m.board)
    assert "1 notes" in text(outside["body"]) and outside["transaction"] is None
    with pytest.raises(TypeError):
        inside.call(len)  # ty: ignore[invalid-argument-type]
    inside.rollback()


CLAIMS = """
\"\"\"Tasks, each claimed by one agent's token.\"\"\"

from pathlib import Path

from cliquet import ActionContext, App, InvariantContext, QueryContext

claims = App("claims", files=["claims.txt"])


def _read(path: Path) -> dict[str, tuple[str, str]]:
    lines = path.read_text().splitlines() if path.exists() else []
    return {task: (person, token) for task, person, token in (line.split() for line in lines)}


@claims.action
def claim(ctx: ActionContext, task: str) -> str | None:
    \"\"\"Claim a task for the token behind the call.\"\"\"
    with open("claims.txt", "a") as f:
        f.write(f"{task} {ctx.caller.id} {ctx.caller.token_id}\\n")
    return ctx.caller.token_id


@claims.action
def close(ctx: ActionContext, task: str) -> None:
    \"\"\"Close a task.\"\"\"
    kept = {t: who for t, who in _read(Path("claims.txt")).items() if t != task}
    Path("claims.txt").write_text("".join(f"{t} {p} {k}\\n" for t, (p, k) in kept.items()))


@claims.invariant("Only the person and token that claimed a task close it")
def own_tasks(ctx: InvariantContext) -> list[str]:
    if ctx.action in ("app_revert", "app_publish", "app_check"):  # Cliquet itself
        return []
    after, caller = _read(Path("claims.txt")), ctx.caller
    return [
        f"{task} was claimed by another agent"
        for task, (person, token) in _read(ctx.before / "claims.txt").items()
        if task not in after
        and (person != caller.id or (token != "None" and token != caller.token_id))
    ]


@claims.query
def mine(ctx: QueryContext) -> str | None:
    \"\"\"The id of the token behind the call, for an agent that needs its own.\"\"\"
    return ctx.caller.token_id
"""


def test_app_code_reads_the_token_behind_a_call(write_app: WriteApp) -> None:
    m = load(write_app, CLAIMS)
    world = given(m.claims, {"claims.txt": ""}, now=NOW)
    first, second = world.connect(token_id="tok_a"), world.connect(token_id="tok_b")
    assert first.caller.token_id == "tok_a" and world.connect().caller.token_id is None
    assert first.query(m.mine) == "tok_a"
    assert world.connect().query(m.mine) is None
    assert first.call(m.claim, "wash").expect_ok() == "tok_a"
    # Another token of the same person can't close the first one's task, nor anyone else.
    second.call(m.close, "wash").expect_refused(by=m.own_tasks)
    world.connect(id="ann").call(m.close, "wash").expect_refused(by=m.own_tasks)
    first.call(m.close, "wash").expect_ok()
