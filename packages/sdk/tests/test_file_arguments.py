"""An action's file argument: the file the person uploads reaches the action as a file in its
working copy, never through the call's arguments, and a reference to it is used once."""

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import mcp_types as types
import pytest
from conftest import WriteApp, module_name
from files_app import Imported, import_rows, look, maybe_import, rows, two_fields, write_beside
from test_runtime import NOW, git, make_app

from cliquet import Caller, Refused
from cliquet.errors import AppError, AppFailed
from cliquet.files import FILE_FORMAT, INPUTS, input_name
from cliquet.inputs import Asked, Claim, Input
from cliquet.limits import MAX_FILE_ARGUMENT, file_too_big
from cliquet.manifest import FILE_TOLD, build_manifest
from cliquet.mcp_server import NO_FILES, AppServer
from cliquet.runtime import EVERYONE, Committed, Refusal, Runtime
from cliquet.screens import check, parse
from cliquet.store import GitStore
from cliquet.testing import given
from cliquet.tools import ASK_FOR_FILE, BUILT_IN

FILES_APP = Path(__file__).parent / "files_app.py"
OWNER = Caller("sam", "owner")
# A string of the file that must never reach an agent: not in the arguments, nor in an answer.
MARKER = "MARKER-7f3a"
CSV = f"{MARKER},12\nbread,3\n"
_names = iter(range(10**6))


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    source = FILES_APP.read_text()
    found = make_app(tmp_path / "rows", source, {"rows.csv": "start,1\n"}, owner=EVERYONE)
    yield found
    found.close()


def data(runtime: Runtime) -> str:
    return (runtime.store.data_dir / "rows.csv").read_text()


@dataclass
class Counted:
    """A claim that counts what the runtime did with it."""

    content: bytes = CSV.encode()
    name: str = "statement.csv"
    refused: str | None = None
    claims: int = 0
    uses: int = 0
    releases: int = 0

    def claim(self) -> Input:
        self.claims += 1
        if self.refused is not None:
            raise Refused(self.refused)
        return Input(self.name, self.content)

    def use(self) -> None:
        self.uses += 1

    def release(self) -> None:
        self.releases += 1


def call(runtime: Runtime, claim: Claim, action: str = "import_rows", **kwargs: Any) -> Any:
    arguments = {"statement": "r-1"}
    return runtime.call(OWNER, action, arguments, inputs={"statement": claim}, **kwargs)


# ---------------------------------------------------------------- The manifest


def test_the_manifest_says_which_argument_is_a_file() -> None:
    manifest = build_manifest(FILES_APP, "cliquet_files_manifest")
    actions = {one["name"]: one for one in manifest["actions"]}

    assert actions["import_rows"]["file_argument"] == "statement"
    assert actions["maybe_import"]["file_argument"] == "statement"
    statement = actions["import_rows"]["input_schema"]["properties"]["statement"]
    assert statement["type"] == "string" and statement["format"] == FILE_FORMAT
    # The agent is told how to pass it: a reference, never the contents.
    assert statement["description"] == FILE_TOLD
    assert ASK_FOR_FILE in FILE_TOLD
    optional = actions["maybe_import"]["input_schema"]["properties"]["statement"]
    assert optional["anyOf"][0]["format"] == FILE_FORMAT and optional["default"] is None
    # The tool's name leaves an app's name the room it had.
    assert len(ASK_FOR_FILE) < len("app_rollback_transaction") == max(map(len, BUILT_IN))


@pytest.mark.parametrize(
    ("form", "refused"),
    [
        ('<form data-call="import_rows"><button class="btn">Import</button></form>', True),
        (
            '<form data-call="maybe_import"><input name="statement">'
            '<button class="btn">Import</button></form>',
            True,
        ),
        ('<form data-call="maybe_import"><button class="btn">Count</button></form>', False),
    ],
)
def test_a_form_can_t_send_a_file(form: str, refused: bool) -> None:
    manifest = build_manifest(FILES_APP, "cliquet_files_manifest")
    body, problems = parse(form)
    assert problems == []
    found = check(body, {}, manifest, "owner", "home")[0]
    told = [one for one in found if "a form can't send one" in one]
    assert bool(told) == refused
    # The file argument isn't also said to be missing.
    assert not any("needs" in one and "statement" in one for one in found if one not in told)


@pytest.mark.parametrize(
    "body",
    [
        "@app.query\ndef q(ctx: QueryContext, f: File) -> int:\n    'Q.'\n    return 1\n",
        "@app.view\ndef v(ctx: QueryContext, f: File | None = None) -> Screen:\n"
        "    'V.'\n    return Screen('<p>x</p>')\n",
        "@app.action\ndef a(ctx: ActionContext, f: list[File]) -> None:\n    'A.'\n",
        "class Box(BaseModel):\n    f: File\n\n\n"
        "@app.action\ndef a(ctx: ActionContext, box: Box) -> None:\n    'A.'\n",
        "@app.action\ndef a(ctx: ActionContext, f: dict[str, File]) -> None:\n    'A.'\n",
        "@app.query\ndef q(ctx: QueryContext) -> File:\n    'Q.'\n    return Path('x')\n",
    ],
)
def test_a_file_is_only_an_action_s_own_argument(write_app: WriteApp, body: str) -> None:
    path = write_app(
        "from pathlib import Path\n"
        "from cliquet import ActionContext, App, BaseModel, File, QueryContext, Screen\n"
        "'''An app.'''\n"
        "app = App('app', files=['a.txt'])\n\n\n" + body
    )
    with pytest.raises(AppError) as refused:
        build_manifest(path, module_name(path))
    assert "a File is an action's own argument" in str(refused.value)


def test_an_action_takes_one_file_at_most(write_app: WriteApp) -> None:
    path = write_app(
        """\
        from cliquet import ActionContext, App, File
        '''An app.'''
        app = App('app', files=['a.txt'])


        @app.action
        def a(ctx: ActionContext, first: File, second: File) -> None:
            '''A.'''
        """
    )
    with pytest.raises(AppError, match=r"takes 2 files \(first, second\), and an action takes one"):
        build_manifest(path, module_name(path))


def test_a_file_s_name_is_cleaned() -> None:
    assert input_name("relevé de mars.csv") == "releve_de_mars.csv"
    assert input_name("../../etc/passwd") == "passwd"
    assert input_name(r"C:\Users\sam\.bank.ofx") == "bank.ofx"
    assert input_name("...") == "file"
    assert input_name("x" * 300 + ".csv").endswith("x.csv")
    assert len(input_name("x" * 300 + ".csv")) == 100


# ---------------------------------------------------------------- In the working copy


def test_the_action_reads_the_file_in_a_dot_folder_and_only_its_data_is_kept(
    runtime: Runtime,
) -> None:
    claim = Counted()

    outcome = call(runtime, claim)

    assert isinstance(outcome, Committed)
    # The path is relative to the working copy, in its own folder, named after the file.
    assert outcome.result == {"rows": 2, "read_from": f"{INPUTS}/statement.csv"}
    # The file isn't data: only the data file it wrote is in the change. The invariant that
    # looks for it found it gone.
    assert outcome.files == ["rows.csv"]
    assert data(runtime) == f"start,1\n{CSV}"
    assert not (runtime.store.data_dir / INPUTS).exists()
    assert (claim.claims, claim.uses, claim.releases) == (1, 1, 0)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_the_file_is_read_only(runtime: Runtime) -> None:
    claim = Counted()
    with pytest.raises(AppFailed, match="Permission"):
        call(runtime, claim, "write_beside")
    assert (claim.uses, claim.releases) == (0, 1)


def test_a_call_that_brings_no_file_for_a_file_argument_is_wrong(runtime: Runtime) -> None:
    with pytest.raises(Exception, match="is a file the person uploads, and the call brought none"):
        runtime.call(OWNER, "import_rows", {"statement": "r-1"})
    # An optional one may be left out.
    optional = runtime.call(OWNER, "maybe_import", {})
    assert isinstance(optional, Committed) and optional.result == 1


# ---------------------------------------------------------------- Using a reference up


def test_a_retry_with_the_same_request_id_answers_the_first_commit(runtime: Runtime) -> None:
    claim = Counted()
    first = call(runtime, claim, request_id="req-1")
    again = call(runtime, claim, request_id="req-1")

    assert isinstance(again, Committed) and again.replayed
    assert again.commit == first.commit
    # The retry never claimed the reference, which the first call used up.
    assert (claim.claims, claim.uses, claim.releases) == (1, 1, 0)


def test_a_write_that_runs_again_keeps_the_file_it_claimed(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_read = GitStore.read
    once = [True]

    def read_then_someone_commits(
        self: GitStore, commit: str | None, files: tuple[str, ...]
    ) -> dict[str, bytes]:
        contents = real_read(self, commit, files)
        if commit is not None and once:
            once.clear()
            path = self.data_dir / "rows.csv"
            path.write_text(path.read_text() + "pulled,2\n")
            git(self.root, "commit", "-qam", "busy")
        return contents

    monkeypatch.setattr(GitStore, "read", read_then_someone_commits)
    claim = Counted()

    assert isinstance(call(runtime, claim), Committed)
    assert data(runtime) == f"start,1\npulled,2\n{CSV}"
    assert (claim.claims, claim.uses, claim.releases) == (1, 1, 0)


def test_a_refused_call_gives_the_reference_back(runtime: Runtime) -> None:
    by_the_action = Counted(content=b"REFUSE\n")
    by_an_invariant = Counted(content=b"no amount here\n")
    nothing_changed = Counted()

    assert call(runtime, by_the_action).message == "The file asks to be refused."
    assert two_fields.__name__ in call(runtime, by_an_invariant).invariants
    assert call(runtime, nothing_changed, "look").commit is None

    for claim in (by_the_action, by_an_invariant, nothing_changed):
        assert (claim.claims, claim.uses, claim.releases) == (1, 0, 1)
    assert data(runtime) == "start,1\n"


def test_a_reference_that_can_t_be_claimed_refuses_the_call(runtime: Runtime) -> None:
    claim = Counted(refused="The file for r-1 isn't uploaded yet.")

    outcome = call(runtime, claim)

    assert isinstance(outcome, Refusal) and outcome.message == claim.refused
    # Nothing was claimed, so nothing is given back.
    assert (claim.claims, claim.uses, claim.releases) == (1, 0, 0)


def test_a_file_over_the_cap_is_refused_with_the_limit(runtime: Runtime) -> None:
    claim = Counted(content=b"a,1\n" * (MAX_FILE_ARGUMENT // 4 + 1))

    outcome = call(runtime, claim)

    assert isinstance(outcome, Refusal)
    assert outcome.message == file_too_big("changed")
    assert "at most 5 MB (5,242,880 bytes)" in outcome.message
    assert (claim.uses, claim.releases) == (0, 1)


def test_a_call_in_a_transaction_uses_the_reference_up(runtime: Runtime) -> None:
    transaction = runtime.begin(OWNER)
    claim = Counted()

    assert isinstance(call(runtime, claim, transaction=transaction), Committed)
    assert (claim.uses, claim.releases) == (1, 0)
    runtime.rollback(OWNER, transaction)
    assert data(runtime) == "start,1\n"


# ---------------------------------------------------------------- In tests


def test_a_test_gives_a_file_as_bytes_or_a_path(tmp_path: Path) -> None:
    world = given(rows, {"rows.csv": ""}, now=NOW)
    owner = world.connect()
    file = tmp_path / "mars.csv"
    file.write_text("tea,4\n")

    by_path = owner.call(import_rows, statement=file).expect_ok()
    by_bytes = owner.call(import_rows, statement=cast(Any, b"rice,2\n")).expect_ok()

    assert by_path == Imported(rows=1, read_from=f"{INPUTS}/mars.csv")
    assert by_bytes.read_from == f"{INPUTS}/file"
    assert owner.call(maybe_import).expect_ok() == 2
    owner.call(import_rows, statement=cast(Any, b"no amount\n")).expect_refused(by=two_fields)
    with pytest.raises(TypeError, match="statement is a file: pass its bytes, or a Path"):
        owner.call(import_rows, statement=cast(Any, "tea,4\n"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_test_sees_the_file_read_only() -> None:
    owner = given(rows, {}, now=NOW).connect()
    with pytest.raises(AppFailed, match="Permission"):
        owner.call(write_beside, statement=cast(Any, b"x"))
    assert owner.call(look, statement=cast(Any, b"four")).expect_ok() == 4


# ---------------------------------------------------------------- Over MCP


@dataclass
class Store:
    """References kept in memory, as a server keeps them: each used once, by its person."""

    files: dict[str, tuple[str, str, str, Input | None]] = field(default_factory=dict)
    used: set[str] = field(default_factory=set)

    def ask(self, person: str, action: str, argument: str) -> Asked:
        reference = f"r-{len(self.files) + 1}"
        self.files[reference] = (person, action, argument, None)
        return Asked(reference, f"https://app.example/uploads/{reference}", NOW + timedelta(1))

    def upload(self, reference: str, content: bytes) -> None:
        person, action, argument, _ = self.files[reference]
        self.files[reference] = (person, action, argument, Input("mars.csv", content))

    def claim(self, person: str, action: str, argument: str, reference: str) -> Claim:
        store = self

        class One:
            def claim(self) -> Input:
                found = store.files.get(reference)
                if (
                    found is None
                    or reference in store.used
                    or found[:3]
                    != (
                        person,
                        action,
                        argument,
                    )
                ):
                    raise Refused(f"There is no file {reference} for you.")
                if found[3] is None:
                    raise Refused(f"The file for {reference} isn't uploaded yet.")
                return found[3]

            def use(self) -> None:
                store.used.add(reference)

            def release(self) -> None:
                pass

        return One()


def mcp_call(server: AppServer, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    return server.call(name, arguments)


def everything(result: types.CallToolResult) -> str:
    """All an agent reads of an answer: its text and its structured content."""
    texts = [one.text for one in result.content if isinstance(one, types.TextContent)]
    return json.dumps([texts, result.structured_content])


def test_the_file_s_contents_never_reach_the_agent(runtime: Runtime) -> None:
    store = Store()
    server = AppServer(runtime, OWNER, files=store)
    tools = {tool.name: tool for tool in server.tools}
    assert ASK_FOR_FILE in tools
    assert tools["import_rows"].input_schema["properties"]["statement"]["format"] == FILE_FORMAT

    asked = mcp_call(server, ASK_FOR_FILE, {"action": "import_rows"})
    reference = asked.structured_content["reference"]
    assert asked.structured_content["link"].endswith(reference)
    early = mcp_call(server, "import_rows", {"statement": reference, "app_remark": "March"})
    assert early.is_error and "isn't uploaded yet" in everything(early)

    store.upload(reference, CSV.encode())
    begun = mcp_call(server, "app_begin_transaction", {})
    transaction = begun.structured_content["transaction"]
    done = mcp_call(
        server,
        "import_rows",
        {"statement": reference, "app_remark": "March", "app_transaction": transaction},
    )
    calls = mcp_call(server, "app_transaction_calls", {"app_transaction": transaction})
    kept = mcp_call(server, "app_commit_transaction", {"app_transaction": transaction})
    history = mcp_call(server, "app_history", {})
    twice = mcp_call(server, "import_rows", {"statement": reference})

    assert not done.is_error and done.structured_content["result"]["rows"] == 2
    assert not kept.is_error
    assert MARKER in data(runtime)
    for answer in (asked, early, done, calls, kept, history, twice):
        assert MARKER not in everything(answer)
    # The transaction keeps the reference as the call's argument: never the file.
    assert reference in everything(calls)
    assert twice.is_error and f"no file {reference}" in everything(twice)


def test_a_reference_is_asked_for_an_action_that_takes_a_file(runtime: Runtime) -> None:
    server = AppServer(runtime, OWNER, files=Store())

    wrong = mcp_call(server, ASK_FOR_FILE, {"action": "listed"})
    other = mcp_call(server, ASK_FOR_FILE, {"action": "import_rows", "argument": "rows"})
    named = mcp_call(server, ASK_FOR_FILE, {"action": "look", "argument": "statement"})

    assert wrong.is_error and "listed takes no file" in everything(wrong)
    assert other.is_error and "its only one" in everything(other)
    assert not named.is_error


def test_a_server_without_a_store_takes_no_file(runtime: Runtime) -> None:
    server = AppServer(runtime, OWNER)

    asked = mcp_call(server, ASK_FOR_FILE, {"action": "import_rows"})
    called = mcp_call(server, "import_rows", {"statement": "r-1"})

    assert asked.is_error and NO_FILES in everything(asked)
    assert called.is_error and NO_FILES in everything(called)


def test_a_role_without_a_file_action_gets_no_ask_for_file(tmp_path: Path) -> None:
    source = (Path(__file__).parent / "tally_app.py").read_text()
    runtime = make_app(tmp_path / "tally", source, {"amounts.txt": ""}, owner=EVERYONE)
    try:
        server = AppServer(runtime, OWNER, files=Store())
        assert ASK_FOR_FILE not in {tool.name for tool in server.tools}
    finally:
        runtime.close()


def test_the_file_travels_to_a_worker_process(runtime: Runtime) -> None:
    from cliquet.worker import Worker, handle

    worker = Worker(FILES_APP, f"cliquet_files_{next(_names)}")
    request = {
        "op": "call",
        "name": "import_rows",
        "caller": {"id": "sam", "role": "owner"},
        "now": NOW.isoformat(),
        "arguments": {"statement": "r-1"},
        "files": {},
        "inputs": {"statement": {"name": "../mars.csv", "content": "YSwxCg=="}},
    }
    answer = handle(worker, FILES_APP.parent, request)

    assert answer["result"] == {"rows": 1, "read_from": f"{INPUTS}/mars.csv"}
    assert list(answer["changes"]) == ["rows.csv"]
    worker.close()
