"""A file the person uploads for an action: the references that stand for it, the person's
tools that ask for it, and its upload page."""

import base64
import io
import json
import shutil
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
import pytest
from starlette.applications import Starlette

from cliquet import screenshot
from cliquet.errors import Refused
from cliquet.inputs import Asked, Input
from cliquet.limits import MAX_FILE_ARGUMENT
from cliquet.tools import ASK_FOR_FILE
from cliquet_server import me as me_module
from cliquet_server.builder import BuilderError
from cliquet_server.builder.panel import file_panel_html
from cliquet_server.builder.trying import _no_file
from cliquet_server.endpoints import Registry
from cliquet_server.inputs import LIFETIME, OPEN_REFERENCES, TOO_BIG, Pending, References
from cliquet_server.me import ASK_FILE, DESCRIBE, READ, UPLOAD_FILE, WRITE, Me, answered, make_me
from cliquet_server.parts import PART, PartRefused, Received
from cliquet_server.web.uploads import MAX_FILE_UPLOAD

from .server_helpers import (
    PANEL,
    SAID,
    a_shop_member,
    csrf_for,
    member,
    needs_browser,
    panel_host,
    signed_in,
    wait_for,
)

FILES_APP = Path(__file__).parents[2] / "sdk" / "tests" / "files_app.py"
# A string of the file that must never reach an agent.
MARKER = "MARKER-7f3a"
CSV = f"{MARKER},12\nbread,3\n".encode()
BASE = "http://testserver"


class Clock:
    """A clock a test moves on."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def store(tmp_path: Path) -> References:
    return References(tmp_path / "inputs", BASE, Clock())


def asked(store: References, person: str = "etienne", action: str = "import_rows") -> str:
    return store.ask("etienne", "rows", person, action, "statement").reference


def claimed(store: References, reference: str, person: str = "etienne", **bound: str) -> Input:
    found = {"login": "etienne", "name": "rows", "action": "import_rows", "argument": "statement"}
    found.update(bound)
    return store.claim(
        found["login"], found["name"], person, found["action"], found["argument"], reference
    )


def sent(store: References, reference: str, content: bytes = CSV, person: str = "etienne") -> Any:
    return store.receive(person, reference, "mars.csv", io.BytesIO(content), len(content))


def files_on_disk(store: References) -> list[str]:
    folder = store.root / "files"
    return sorted(one.name for one in folder.iterdir()) if folder.is_dir() else []


# ---------------------------------------------------------------- The references


def test_a_reference_is_used_once(store: References) -> None:
    reference = asked(store)
    assert store.ask("etienne", "rows", "etienne", "import_rows", "statement").link.startswith(
        f"{BASE}/uploads/"
    )
    with pytest.raises(Refused, match="hasn't uploaded the file"):
        claimed(store, reference)

    pending = sent(store, reference)
    assert (pending.file_name, pending.size) == ("mars.csv", len(CSV))
    assert claimed(store, reference) == Input("mars.csv", CSV)
    # While a call holds it, no other call takes it, and no upload replaces it.
    with pytest.raises(Refused, match="Another call uses"):
        claimed(store, reference)
    with pytest.raises(PartRefused, match="A call uses this file now"):
        sent(store, reference)
    store.release(reference)
    assert claimed(store, reference).content == CSV
    store.use(reference)

    with pytest.raises(Refused, match="There is no file"):
        claimed(store, reference)
    assert store.find("etienne", reference) is None
    assert files_on_disk(store) == []


def test_a_reference_is_missing_for_anyone_else(store: References) -> None:
    reference = asked(store)
    sent(store, reference)

    for wrong in ({"person": "sam"}, {"action": "look"}, {"argument": "rows"}, {"name": "shop"}):
        person = wrong.pop("person", "etienne")
        with pytest.raises(Refused, match="There is no file") as refused:
            claimed(store, reference, person, **wrong)
        assert "There is no file" in str(refused.value)
    assert store.find("sam", reference) is None
    with pytest.raises(PartRefused, match="ended, or isn't yours"):
        sent(store, reference, person="sam")
    # Its own person still has it, whole.
    assert claimed(store, reference).content == CSV


def test_a_reference_ends_after_its_time(store: References) -> None:
    clock = store.clock
    assert isinstance(clock, Clock)
    waiting = asked(store)
    arrived = asked(store)
    clock.now += LIFETIME - 10
    sent(store, arrived)

    # An hour after it was asked for, with no file: it ends.
    clock.now += 10
    assert store.find("etienne", waiting) is None
    # An hour after its file came: it ends, with its file.
    assert claimed(store, arrived).content == CSV
    store.release(arrived)
    clock.now += LIFETIME
    with pytest.raises(Refused, match="There is no file"):
        claimed(store, arrived)
    assert files_on_disk(store) == []


def test_a_new_upload_replaces_the_file(store: References) -> None:
    reference = asked(store)
    sent(store, reference, b"first,1\n")
    sent(store, reference, b"second,2\n")

    assert claimed(store, reference).content == b"second,2\n"
    assert len(files_on_disk(store)) == 1


def test_a_person_has_a_few_references_open_at_most(store: References) -> None:
    clock = store.clock
    assert isinstance(clock, Clock)
    made = []
    for _ in range(OPEN_REFERENCES + 1):
        made.append(asked(store))
        clock.now += 1
    other = asked(store, person="sam")

    assert store.find("etienne", made[0]) is None
    assert all(store.find("etienne", one) is not None for one in made[1:])
    assert store.find("sam", other) is not None

    # One a call uses now isn't the one that ends: the oldest that no call uses is.
    sent(store, made[1])
    assert claimed(store, made[1]).content == CSV
    made.append(asked(store))
    assert store.find("etienne", made[1]) is not None
    assert store.find("etienne", made[2]) is None


def test_a_file_arrives_in_parts_and_over_the_cap_is_refused(store: References) -> None:
    reference = asked(store)
    content = bytes(range(256)) * (PART // 256) + b"end"

    def part(offset: int) -> str:
        return base64.b64encode(content[offset : offset + PART]).decode()

    first = store.receive_part("etienne", reference, "big.bin", len(content), 0, part(0), None)
    assert isinstance(first, Received) and first.received == PART
    waiting = store.find("etienne", reference)
    assert waiting is not None and waiting.file_name is None
    last = store.receive_part(
        "etienne", reference, "big.bin", len(content), PART, part(PART), first.upload
    )

    assert isinstance(last, Pending)
    assert last.file_name == "big.bin" and last.size == len(content)
    assert claimed(store, reference).content == content
    with pytest.raises(PartRefused) as refused:
        store.receive_part("etienne", reference, "big.bin", MAX_FILE_ARGUMENT + 1, 0, part(0), None)
    assert str(refused.value) == TOO_BIG
    assert "at most 5 MB (5,242,880 bytes)" in TOO_BIG


def test_a_restart_and_a_deleted_account_end_the_references(tmp_path: Path) -> None:
    store = References(tmp_path / "inputs", BASE)
    reference = asked(store)
    sent(store, reference)
    store.drop("etienne")
    assert store.find("etienne", reference) is None

    kept = asked(store)
    sent(store, kept)
    again = References(tmp_path / "inputs", BASE)
    assert again.find("etienne", kept) is None
    assert files_on_disk(again) == []


# ---------------------------------------------------------------- On the person's endpoint


@pytest.fixture
def rows(server: Starlette, tmp_path: Path) -> Starlette:
    """The server, with etienne's rows app, whose action takes a file."""
    folder = tmp_path / "rows"
    (folder / "data").mkdir(parents=True)
    shutil.copy(FILES_APP, folder / "app.py")
    (folder / "data" / "rows.csv").write_text("start,1\n")
    server.state.apps.import_folder(folder, "etienne", "rows")
    return server


def me(server: Starlette, login: str, tool: str, /, **arguments: Any) -> types.CallToolResult:
    """Call one of the person's tools as `login`."""
    state = server.state
    core = Me(state.apps, state.registry, state.settings.base_url, state.builder)
    person = state.apps.database.person_by_login(login)
    method = getattr(core, tool)

    async def run() -> types.CallToolResult:
        async def called() -> types.CallToolResult:
            found = method(person, **arguments)
            return await found if not isinstance(found, types.CallToolResult) else found

        return await answered(tool, called())

    return anyio.run(run)


def everything(result: types.CallToolResult) -> str:
    texts = [one.text for one in result.content if isinstance(one, types.TextContent)]
    return json.dumps([texts, result.structured_content])


def test_the_person_asks_for_a_file_and_the_action_reads_it(rows: Starlette) -> None:
    linked = me(rows, "etienne", ASK_FILE, app="etienne/rows", action="import_rows")
    shown = me(
        rows, "etienne", ASK_FILE, app="etienne/rows", action="import_rows", draws_screens=True
    )
    reference = linked.structured_content["reference"]

    assert not linked.is_error
    base = rows.state.settings.base_url
    assert linked.structured_content["link"] == f"{base}/uploads/{reference}"
    assert "Give the person this link" in everything(linked)
    assert "The person now sees a panel" in everything(shown)
    early = me(
        rows,
        "etienne",
        WRITE,
        app="etienne/rows",
        tool="import_rows",
        arguments={"statement": reference, "app_remark": "March"},
    )
    assert early.is_error and "hasn't uploaded the file" in everything(early)

    data = base64.b64encode(CSV).decode()
    upload = me(rows, "etienne", UPLOAD_FILE, reference=reference, name="mars.csv", size=len(CSV),
                offset=0, data=data)  # fmt: skip
    done = me(
        rows,
        "etienne",
        WRITE,
        app="etienne/rows",
        tool="import_rows",
        arguments={"statement": reference, "app_remark": "March"},
    )
    again = me(
        rows,
        "etienne",
        WRITE,
        app="etienne/rows",
        tool="import_rows",
        arguments={"statement": reference},
    )
    history = me(rows, "etienne", READ, app="etienne/rows", tool="app_history")

    assert upload.structured_content == {
        "reference": reference,
        "name": "mars.csv",
        "size": len(CSV),
        "done": True,
    }
    assert not done.is_error and done.structured_content["result"]["rows"] == 2
    assert again.is_error and "There is no file" in everything(again)
    for answer in (linked, shown, early, upload, done, again, history):
        assert MARKER not in everything(answer)
    repo = rows.state.apps.repo("etienne", "rows")
    assert MARKER.encode() in repo.data(["rows.csv"], repo.head())["rows.csv"]


def test_describe_app_names_the_tool_that_asks_for_a_file(rows: Starlette) -> None:
    answer = me(rows, "etienne", DESCRIBE, app="etienne/rows")
    described = answer.structured_content
    listed = {tool["name"]: tool for tool in described["tools"]}
    assert f"call {ASK_FILE} itself" in everything(answer)

    assert listed[ASK_FOR_FILE]["call_with"] == ASK_FILE
    assert listed["import_rows"]["example"] == {"statement": f"<the reference {ASK_FILE} gives>"}
    assert described["example_call"][WRITE]["arguments"]["statement"].startswith("<the reference")
    wrong = me(rows, "etienne", READ, app="etienne/rows", tool=ASK_FOR_FILE)
    assert wrong.is_error and f"call {ASK_FILE}" in everything(wrong)


def test_the_panel_takes_parts_only_for_its_person(rows: Starlette) -> None:
    sam = a_shop_member(rows, None)
    member(rows.state.apps.database, sam, "etienne", "rows", "owner")
    reference = me(rows, "etienne", ASK_FILE, app="etienne/rows", action="import_rows")
    reference = reference.structured_content["reference"]
    data = base64.b64encode(CSV).decode()

    theirs = me(rows, "sam", UPLOAD_FILE, reference=reference, name="x.csv", size=len(CSV),
                offset=0, data=data)  # fmt: skip
    asked_by_sam = me(rows, "sam", ASK_FILE, app="etienne/rows", action="import_rows")
    used_by_sam = me(
        rows,
        "sam",
        WRITE,
        app="etienne/rows",
        tool="import_rows",
        arguments={"statement": reference},
    )

    assert theirs.is_error and "isn't yours" in everything(theirs)
    # A member asks for their own.
    assert not asked_by_sam.is_error
    assert used_by_sam.is_error and "There is no file" in everything(used_by_sam)


def test_the_app_s_own_address_always_answers_a_link(rows: Starlette) -> None:
    async def run() -> tuple[types.CallToolResult, list[types.Tool]]:
        server = await rows.state.registry.app_server("etienne", "rows", "owner", "etienne")
        return server.call(ASK_FOR_FILE, {"action": "import_rows"}), server.tools

    result, listed = anyio.run(run)
    tool = next(one for one in listed if one.name == ASK_FOR_FILE)

    assert not result.is_error
    assert result.structured_content["link"].startswith(f"{rows.state.settings.base_url}/uploads/")
    # It shows no panel: no host would count it as a view.
    assert tool.meta is None


def test_try_call_gives_an_action_no_file() -> None:
    manifest = {"actions": [{"name": "import_rows", "file_argument": "statement"}]}
    for name, arguments in ((ASK_FOR_FILE, {}), ("import_rows", {"statement": "r"})):
        with pytest.raises(BuilderError, match="in the app's tests instead"):
            _no_file(manifest, name, arguments)
    _no_file(manifest, "import_rows", {})


# ---------------------------------------------------------------- The upload page


def a_reference(server: Starlette, person: str = "etienne") -> Asked:
    return server.state.registry.inputs.ask("etienne", "rows", person, "import_rows", "statement")


def test_the_upload_page_takes_the_file(rows: Starlette) -> None:
    reference = a_reference(rows).reference
    with signed_in(rows) as client:
        page = client.get(f"/uploads/{reference}")
        assert page.status_code == 200
        assert "Upload a file for import_rows" in page.text
        sent = client.post(
            f"/uploads/{reference}",
            data={"csrf": csrf_for(rows, client)},
            files={"file": ("mars.csv", CSV, "text/csv")},
            follow_redirects=False,
        )
        assert sent.status_code == 303
        done = client.get(sent.headers["location"])

    assert "mars.csv is uploaded" in done.text
    assert MARKER not in done.text
    store = rows.state.registry.inputs
    assert store.claim("etienne", "rows", "etienne", "import_rows", "statement", reference)


def test_the_upload_page_is_its_person_s_alone(rows: Starlette) -> None:
    sams = a_reference(rows, "sam").reference
    with signed_in(rows) as client:
        assert client.get(f"/uploads/{sams}").status_code == 404
        assert client.get("/uploads/nope").status_code == 404
        refused = client.post(
            f"/uploads/{sams}",
            data={"csrf": csrf_for(rows, client)},
            files={"file": ("mars.csv", CSV, "text/csv")},
        )
        assert refused.status_code == 404
    with signed_in(rows) as client:
        client.cookies.clear()
        away = client.get(f"/uploads/{sams}", follow_redirects=False)
    assert away.status_code == 303 and away.headers["location"].startswith("/login")


def test_the_upload_page_refuses_a_file_too_big_or_a_form_without_its_token(
    rows: Starlette,
) -> None:
    reference = a_reference(rows).reference
    big = b"a,1\n" * (MAX_FILE_ARGUMENT // 4 + 1)
    with signed_in(rows) as client:
        csrf = csrf_for(rows, client)
        over = client.post(
            f"/uploads/{reference}", data={"csrf": csrf}, files={"file": ("big.csv", big)}
        )
        stated = client.post(
            f"/uploads/{reference}",
            content=b"x",
            headers={
                "content-type": "multipart/form-data; boundary=x",
                "content-length": str(MAX_FILE_UPLOAD + 1),
            },
        )
        forged = client.post(
            f"/uploads/{reference}", data={"csrf": "wrong"}, files={"file": ("a.csv", CSV)}
        )

    assert over.status_code == 413 and "at most 5 MB (5,242,880 bytes)" in over.text
    assert stated.status_code == 413 and "at most 5 MB" in stated.text
    assert forged.status_code == 403
    store = rows.state.registry.inputs
    pending = store.find("etienne", reference)
    assert pending is not None and pending.file_name is None


def test_a_published_app_says_its_file_argument_in_its_tools(rows: Starlette) -> None:
    live = rows.state.apps.live("etienne", "rows")
    (spec,) = [one for one in live.runtime.manifest["actions"] if one["name"] == "import_rows"]
    assert spec["file_argument"] == "statement"


# ---------------------------------------------------------------- The file panel in a browser


@pytest.fixture
def file_host(rows: Starlette, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Registry]]:
    """The host page and the file panel, served on this machine, with the person's endpoint
    behind them, and the registry that keeps the references."""
    monkeypatch.setattr(me_module, "draws_screens", lambda: True)
    state = rows.state
    registry = Registry(state.apps)
    mcp = make_me(state.apps, registry, state.settings)
    with panel_host(mcp, {"/file.html": file_panel_html().encode()}) as url:
        yield url, registry


@pytest.mark.browser
@pytest.mark.slow
@needs_browser
def test_a_file_chosen_in_the_panel_reaches_its_reference(
    file_host: tuple[str, Registry],
) -> None:
    url, registry = file_host
    content = f"{MARKER},12\n"
    path = screenshot.find_browser()
    assert path is not None
    with closing(screenshot.Browser(path)) as browser:
        browser.visit(url, 768)
        asked = json.dumps({"app": "etienne/rows", "action": "import_rows"})
        browser.evaluate(f"window.start({asked}, true, {json.dumps(ASK_FILE)}, 'file.html')")
        wait_for(browser, f"{PANEL}.getElementById('title')?.textContent.includes('import_rows')")

        browser.evaluate(f"window.choose({len(content)}, {json.dumps(content)})")
        said = wait_for(browser, f"!{SAID}.hidden && {SAID}.textContent")
        contexts = wait_for(browser, "window.contexts.length && window.contexts")
        messages = wait_for(browser, "window.messages.length && window.messages")
        reference = browser.evaluate("window.answered.structuredContent.reference")
        calls = browser.evaluate("window.calls.map((call) => call.tool)")

    assert said == f"ledger.beancount is uploaded: {len(content)} bytes."
    assert calls == [UPLOAD_FILE]
    found = registry.inputs.find("etienne", reference)
    assert found is not None and found.file_name == "ledger.beancount"
    # The assistant learns the reference, the file's name and its size: nothing from the file.
    assert contexts[0]["structuredContent"] == {
        "reference": reference,
        "name": "ledger.beancount",
        "size": len(content),
    }
    assert messages[0]["content"][0]["text"] == (
        f"I uploaded ledger.beancount for import_rows: {len(content)} bytes. "
        f'Call import_rows with statement "{reference}".'
    )
    for told in (contexts, messages):
        assert MARKER not in json.dumps(told)
