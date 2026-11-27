"""The upload panel: ask_for_file shows it, and upload_file takes the file in parts. In a
browser, the token panel too, which create_token shows."""

import base64
import json
import os
from collections.abc import Iterator
from contextlib import closing
from datetime import timedelta
from typing import Any

import anyio
import pytest
from fastmcp import Client as BuilderClient

from cliquet import screenshot, views
from cliquet.limits import MAX_DATA_FILE
from cliquet_server import access_tools
from cliquet_server.builder import Builder, BuilderError
from cliquet_server.builder import server as builder_server
from cliquet_server.builder.files import TOO_BIG
from cliquet_server.builder.panel import panel_html, panel_uri, token_panel_html
from cliquet_server.db import TokenLimit
from cliquet_server.parts import IDLE, OPEN_UPLOADS, PART

from .server_helpers import (
    NOW,
    PANEL,
    SAID,
    TELL,
    draft_files,
    me_server,
    needs_browser,
    panel_host,
    wait_for,
)

BASE = "http://127.0.0.1:8800"

PATH = "data/ledger.beancount"


def pattern(size: int) -> bytes:
    """Made-up bytes, the same as the browser's `pattern` makes, that no part repeats."""
    period = bytes((index * 7 + 3) % 251 for index in range(251))
    return (period * (size // 251 + 1))[:size]


def part(content: bytes, offset: int) -> str:
    return base64.b64encode(content[offset : offset + PART]).decode()


def parts_on_disk(builder: Builder) -> list[str]:
    root = builder.uploads.parts.root
    return sorted(file.name for file in root.iterdir()) if root.is_dir() else []


# ---------------------------------------------------------------- ask_for_file


def ask(builder: Builder, draws: bool, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Call ask_for_file over MCP, from a client that draws screens or not."""
    monkeypatch.setattr(builder_server, "draws_screens", lambda: draws)

    async def main() -> Any:
        async with BuilderClient(me_server(builder.apps, builder)) as client:
            return await client.call_tool("ask_for_file", {"app": "books", "path": PATH})

    return anyio.run(main)


def test_ask_for_file_names_the_panel(builder: Builder) -> None:
    async def main() -> None:
        async with BuilderClient(me_server(builder.apps)) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert tools["ask_for_file"].meta["ui"]["resourceUri"] == panel_uri()
            contents = await client.read_resource(panel_uri())
            assert contents[0].mime_type == views.MIME_TYPE
            assert contents[0].text == panel_html()

    anyio.run(main)
    assert panel_uri() != views.renderer_uri()
    # Nothing in the panel comes from the network.
    assert "http://" not in panel_html().replace("http://www.w3.org", "")
    assert "https://" not in panel_html()
    # The panel speaks to the host through the renderer's own bridge.
    assert views.bridge_js() in panel_html() and views.bridge_js() in views.renderer_html()


def test_a_client_that_draws_gets_the_panel_and_the_link(
    builder: Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder.create_app("etienne", "books")

    asked = ask(builder, True, monkeypatch)

    answer = asked.structured_content
    assert answer["link"] == f"{BASE}/build/books/upload?path={PATH}"
    assert answer["next"].startswith("The person now sees a panel in the conversation")
    assert "give them the link instead" in answer["next"]
    assert answer["path"] == PATH
    assert answer["limit"] == MAX_DATA_FILE
    assert answer["files"] == []
    # The model reads the answer as one JSON object, not as a list holding its text.
    assert [json.loads(part.text) for part in asked.content] == [answer]


def test_a_client_that_can_t_draw_gets_the_link(
    builder: Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder.create_app("etienne", "books")

    asked = ask(builder, False, monkeypatch)

    answer = asked.structured_content
    assert answer["link"] == f"{BASE}/build/books/upload?path={PATH}"
    assert answer["next"].startswith("Give the person this link.")
    assert "panel" not in answer["next"]


# ---------------------------------------------------------------- upload_file


def test_only_a_client_that_draws_the_panel_may_upload(
    builder: Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder.create_app("etienne", "books")
    monkeypatch.setattr(builder_server, "draws_screens", lambda: False)
    sent = {"app": "books", "path": PATH, "size": 4, "offset": 0, "data": "YSxiCg=="}

    async def main() -> Any:
        async with BuilderClient(me_server(builder.apps)) as client:
            return await client.call_tool("upload_file", sent, raise_on_error=False)

    refused = anyio.run(main)

    assert refused.is_error is True
    assert "Give the person the link from ask_for_file" in refused.content[0].text
    assert PATH not in draft_files(builder.drafts, "etienne", "books")


def test_an_uploaded_part_is_answered_in_words(
    builder: Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder.create_app("etienne", "books")
    monkeypatch.setattr(builder_server, "draws_screens", lambda: True)
    sent = {"app": "books", "path": PATH, "size": 4, "offset": 0, "data": "YSxiCg=="}

    async def main() -> Any:
        async with BuilderClient(me_server(builder.apps)) as client:
            return await client.call_tool("upload_file", sent)

    done = anyio.run(main)

    assert [part.text for part in done.content] == [f"{PATH} is in the draft: 4 bytes."]


def test_a_file_arrives_in_parts(builder: Builder) -> None:
    builder.create_app("etienne", "books")
    content = pattern(2 * PART + 123)

    first = builder.upload_file("etienne", "books", PATH, len(content), 0, part(content, 0))
    assert first == {"upload": first["upload"], "received": PART, "done": False}
    # Until the last part, the file is kept outside the draft.
    assert PATH not in draft_files(builder.drafts, "etienne", "books")
    assert len(parts_on_disk(builder)) == 1
    assert not builder.uploads.parts.root.is_relative_to(builder.drafts.root)
    second = builder.upload_file(
        "etienne", "books", PATH, len(content), PART, part(content, PART), first["upload"]
    )
    last = builder.upload_file(
        "etienne", "books", PATH, len(content), 2 * PART, part(content, 2 * PART), first["upload"]
    )

    assert second["received"] == 2 * PART
    assert last == {
        "path": PATH,
        "size": len(content),
        "done": True,
        "files": [{"path": PATH, "size": len(content)}],
    }
    assert draft_files(builder.drafts, "etienne", "books")[PATH] == content
    assert parts_on_disk(builder) == []


def test_an_upload_is_refused_as_the_upload_page_refuses_it(shop, builder: Builder) -> None:
    builder.create_app("etienne", "books")
    one = base64.b64encode(b"a,b\n").decode()

    refusals = {}
    for app, path in [("books", "app.py"), ("books", "../x"), ("shop", "data/products.csv")]:
        with pytest.raises(BuilderError) as refused:
            builder.upload_file("etienne", app, path, 4, 0, one)
        refusals[path] = str(refused.value)
    with pytest.raises(BuilderError, match="no app named nope"):
        builder.upload_file("etienne", "nope", PATH, 4, 0, one)
    with pytest.raises(BuilderError, match=f"{MAX_DATA_FILE:,} bytes"):
        builder.upload_file("etienne", "books", PATH, MAX_DATA_FILE + 1, 0, one)
    with pytest.raises(BuilderError, match="base64"):
        builder.upload_file("etienne", "books", PATH, 4, 0, "not base64!")
    with pytest.raises(BuilderError, match="at most"):
        too_long = base64.b64encode(b"x" * (PART + 1)).decode()
        builder.upload_file("etienne", "books", PATH, PART + 1, 0, too_long)
    with pytest.raises(BuilderError, match="empty"):
        builder.upload_file("etienne", "books", PATH, 0, 0, "")

    assert "isn't a data file" in refusals["app.py"]
    assert "live data" in refusals["data/products.csv"]
    assert PATH not in draft_files(builder.drafts, "etienne", "books")
    assert parts_on_disk(builder) == []


def test_a_file_bigger_than_it_said_is_refused(builder: Builder) -> None:
    builder.create_app("etienne", "books")
    content = pattern(PART + 10)

    first = builder.upload_file("etienne", "books", PATH, PART + 5, 0, part(content, 0))
    with pytest.raises(BuilderError, match="bigger than the"):
        builder.upload_file(
            "etienne", "books", PATH, PART + 5, PART, part(content, PART), first["upload"]
        )

    assert PATH not in draft_files(builder.drafts, "etienne", "books")
    assert parts_on_disk(builder) == []


def test_a_part_out_of_its_place_ends_the_upload(builder: Builder) -> None:
    builder.create_app("etienne", "books")
    content = pattern(2 * PART)
    first = builder.upload_file("etienne", "books", PATH, len(content), 0, part(content, 0))
    upload = first["upload"]

    # Someone else can't go on with it, and it goes on.
    with pytest.raises(BuilderError, match="has ended"):
        builder.uploads.receive(
            "sam", "books", PATH, len(content), PART, part(content, PART), upload
        )
    # A part at the wrong place ends it.
    with pytest.raises(BuilderError, match="has ended"):
        builder.upload_file("etienne", "books", PATH, len(content), 0, part(content, 0), upload)
    with pytest.raises(BuilderError, match="has ended"):
        builder.upload_file(
            "etienne", "books", PATH, len(content), PART, part(content, PART), upload
        )
    # A first part starts at the start.
    with pytest.raises(BuilderError, match="has ended"):
        builder.upload_file("etienne", "books", PATH, len(content), PART, part(content, PART))

    assert parts_on_disk(builder) == []


def test_the_draft_is_checked_again_at_the_last_part(builder: Builder) -> None:
    builder.create_app("etienne", "books")
    content = pattern(PART + 1)
    first = builder.upload_file("etienne", "books", PATH, len(content), 0, part(content, 0))
    builder.delete_app("etienne", "books", "books")

    with pytest.raises(BuilderError, match="no app named books"):
        builder.upload_file(
            "etienne", "books", PATH, len(content), PART, part(content, PART), first["upload"]
        )

    assert parts_on_disk(builder) == []


def test_uploads_nobody_finishes_are_dropped(builder: Builder) -> None:
    builder.create_app("etienne", "books")
    now = [1_000_000.0]
    builder.uploads.parts.clock = lambda: now[0]
    content = pattern(PART + 1)

    def start() -> str:
        return builder.upload_file("etienne", "books", PATH, len(content), 0, part(content, 0))[
            "upload"
        ]

    # An upload idle too long is refused at its next part.
    alone = start()
    now[0] += IDLE + 1
    with pytest.raises(BuilderError, match="has ended"):
        builder.upload_file(
            "etienne", "books", PATH, len(content), PART, part(content, PART), alone
        )
    assert parts_on_disk(builder) == []

    idle = start()
    now[0] += IDLE + 1
    # A part left on the disk by an earlier run, long ago.
    left = builder.uploads.parts.root / "left.part"
    left.write_bytes(b"x")
    os.utime(left, (now[0] - IDLE - 1, now[0] - IDLE - 1))
    started = [start() for _ in range(OPEN_UPLOADS + 1)]

    with pytest.raises(BuilderError, match="has ended"):
        builder.upload_file("etienne", "books", PATH, len(content), PART, part(content, PART), idle)
    # The oldest of those past the few a person may keep is dropped too.
    with pytest.raises(BuilderError, match="has ended"):
        builder.upload_file(
            "etienne", "books", PATH, len(content), PART, part(content, PART), started[0]
        )
    assert len(parts_on_disk(builder)) == OPEN_UPLOADS
    done = builder.upload_file(
        "etienne", "books", PATH, len(content), PART, part(content, PART), started[-1]
    )
    assert done["done"] is True


# ---------------------------------------------------------------- The panel in a browser


@pytest.fixture
def host(builder: Builder, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The host page and the builder's panels, served on this machine, with the builder behind
    them."""
    monkeypatch.setattr(builder_server, "draws_screens", lambda: True)
    monkeypatch.setattr(access_tools, "draws_screens", lambda: True)
    pages = {"/panel.html": panel_html().encode(), "/token.html": token_panel_html().encode()}
    with panel_host(me_server(builder.apps, builder), pages) as url:
        yield url


@pytest.fixture
def browser() -> Iterator[screenshot.Browser]:
    path = screenshot.find_browser()
    assert path is not None
    with closing(screenshot.Browser(path)) as opened:
        yield opened


def open_panel(
    browser: screenshot.Browser, host: str, app: str = "books", offers_message: bool = True
) -> None:
    """Open the host, which shows the panel ask_for_file answered with."""
    browser.visit(host, 768)
    asked = json.dumps({"app": app, "path": PATH})
    browser.evaluate(f"window.start({asked}, {json.dumps(offers_message)})")
    wait_for(browser, f"{PANEL}.getElementById('title')?.textContent.includes({json.dumps(app)})")


def uploads(browser: screenshot.Browser) -> list[dict[str, Any]]:
    return browser.evaluate("window.calls.filter((call) => call.tool === 'upload_file')")


@pytest.mark.browser
@pytest.mark.slow
@needs_browser
def test_a_file_chosen_in_the_panel_arrives_in_the_draft(
    builder: Builder, host: str, browser: screenshot.Browser
) -> None:
    builder.create_app("etienne", "books")
    content = '2026-01-01 * "Tea" ; a made-up line, never told\n'
    open_panel(browser, host)

    browser.evaluate(f"window.choose({len(content.encode())}, {json.dumps(content)})")
    said = wait_for(browser, f"!{SAID}.hidden && {SAID}.textContent")

    assert said == f"{PATH} is in the draft: {len(content.encode())} bytes."
    assert draft_files(builder.drafts, "etienne", "books")[PATH] == content.encode()
    assert len(uploads(browser)) == 1
    listed = browser.evaluate(f"{PANEL}.getElementById('files').textContent")
    assert PATH in listed
    # The assistant learns the path and the size, and nothing from the file.
    contexts = wait_for(browser, "window.contexts.length && window.contexts")
    assert contexts == [
        {
            "content": [
                {
                    "type": "text",
                    "text": f"The person uploaded {PATH} to the draft of books: "
                    f"{len(content.encode())} bytes.",
                }
            ],
            "structuredContent": {"app": "books", "path": PATH, "size": len(content.encode())},
        }
    ]
    # The person's click on Upload sends the assistant one message, at once, with its next
    # step.
    messages = wait_for(browser, "window.messages.length && window.messages")
    assert messages == [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"I uploaded {PATH} to the draft of books: "
                    f"{len(content.encode())} bytes. Read its first lines with read_file, "
                    "then call check_app to see that books reads it.",
                }
            ],
        }
    ]
    assert browser.evaluate(f"{TELL}.hidden") is True
    # Drawn again, the panel sends nothing more.
    browser.evaluate("window.notifyAgain()")
    assert browser.evaluate("window.messages.length") == 1
    assert browser.evaluate("window.contexts.length") == 1
    # The link to the upload page opens through the host.
    browser.evaluate(f"{PANEL}.getElementById('link').click()")
    links = wait_for(browser, "window.links.length && window.links")
    assert links == [f"{BASE}/build/books/upload?path={PATH}"]
    for told in (contexts, messages):
        assert "Tea" not in json.dumps(told)
        assert "made-up" not in json.dumps(told)


@pytest.mark.browser
@pytest.mark.slow
@needs_browser
def test_a_big_file_goes_in_several_parts(
    builder: Builder, host: str, browser: screenshot.Browser
) -> None:
    builder.create_app("etienne", "books")
    size = 2 * PART + 1_234
    open_panel(browser, host, offers_message=False)

    browser.evaluate(f"window.choose({size}, 'pattern')")
    wait_for(browser, f"!{SAID}.hidden && {SAID}.textContent.includes('is in the draft')")

    sent = uploads(browser)
    assert [call["offset"] for call in sent] == [0, PART, 2 * PART]
    # Each part's call is under the limit of an MCP message.
    assert all(call["size"] < 4 * 1024 * 1024 for call in sent)
    assert draft_files(builder.drafts, "etienne", "books")[PATH] == pattern(size)
    # A host that takes no message gets none, and the panel asks the person to say it.
    assert browser.evaluate("window.messages") == []
    assert browser.evaluate(f"{TELL}.hidden") is False
    assert browser.evaluate(f"{TELL}.textContent") == "Tell your assistant the file is in."
    assert len(browser.evaluate("window.contexts")) == 1


@pytest.mark.browser
@pytest.mark.slow
@needs_browser
def test_a_file_over_20_mb_is_never_sent(
    builder: Builder, host: str, browser: screenshot.Browser
) -> None:
    builder.create_app("etienne", "books")
    open_panel(browser, host)

    browser.evaluate(f"window.choose({MAX_DATA_FILE + 1}, '')")
    said = wait_for(browser, f"!{SAID}.hidden && {SAID}.textContent")

    assert said == TOO_BIG
    assert f"{MAX_DATA_FILE:,} bytes" in said
    assert uploads(browser) == []
    assert PATH not in draft_files(builder.drafts, "etienne", "books")
    assert browser.evaluate("window.contexts") == []
    assert browser.evaluate("window.messages") == []
    assert browser.evaluate(f"{TELL}.hidden") is True


@pytest.mark.browser
@pytest.mark.slow
@needs_browser
def test_a_refusal_shows_in_the_panel(
    shop, builder: Builder, host: str, browser: screenshot.Browser
) -> None:
    # The assistant asked for a published app's data: the panel says why, and offers nothing.
    browser.visit(host, 768)
    browser.evaluate(f"window.start({json.dumps({'app': 'shop', 'path': PATH})}, true)")
    lead = wait_for(
        browser,
        f"{PANEL}.getElementById('lead')?.textContent.includes('live data') &&"
        f" {PANEL}.getElementById('lead').textContent",
    )
    assert "Change it through the app's actions" in lead
    assert browser.evaluate(f"{PANEL}.getElementById('choose').hidden") is True

    # The draft went away after the panel opened: the server's words show where it was sent.
    builder.create_app("etienne", "books")
    open_panel(browser, host)
    builder.delete_app("etienne", "books", "books")
    browser.evaluate("window.choose(8, 'a,b\\n1,2\\n')")
    said = wait_for(browser, f"!{SAID}.hidden && {SAID}.textContent")
    assert said.startswith("You have no app named books.")
    assert browser.evaluate(f"{SAID}.classList.contains('alert-error')") is True
    assert browser.evaluate("window.contexts") == []
    # A failed upload tells the assistant nothing.
    assert browser.evaluate("window.messages") == []
    assert browser.evaluate(f"{TELL}.hidden") is True


@pytest.mark.browser
@pytest.mark.slow
@needs_browser
def test_a_host_that_refuses_the_message_leaves_it_to_the_person(
    builder: Builder, host: str, browser: screenshot.Browser
) -> None:
    builder.create_app("etienne", "books")
    browser.visit(host, 768)
    browser.evaluate("window.refusesMessage = true")
    browser.evaluate(f"window.start({json.dumps({'app': 'books', 'path': PATH})}, true)")
    wait_for(browser, f"{PANEL}.getElementById('title')?.textContent.includes('books')")

    browser.evaluate("window.choose(8, 'a,b\\n1,2\\n')")
    shown = wait_for(browser, f"!{TELL}.hidden && {TELL}.textContent")

    assert shown == "Tell your assistant the file is in."
    assert len(browser.evaluate("window.messages")) == 1
    assert len(browser.evaluate("window.contexts")) == 1
    assert draft_files(builder.drafts, "etienne", "books")[PATH] == b"a,b\n1,2\n"


@pytest.mark.browser
@pytest.mark.slow
@needs_browser
def test_a_host_that_lets_the_panel_call_no_tool_gets_the_link(
    builder: Builder, host: str, browser: screenshot.Browser
) -> None:
    builder.create_app("etienne", "books")
    browser.visit(host, 768)
    browser.evaluate("window.noTools = true")
    browser.evaluate(f"window.start({json.dumps({'app': 'books', 'path': PATH})}, true)")

    lead = wait_for(
        browser,
        f"{PANEL}.getElementById('lead')?.textContent.includes('upload page')"
        f" && {PANEL}.getElementById('lead').textContent",
    )

    assert lead == "This assistant can't send a file from here. Open the upload page."
    assert browser.evaluate(f"{PANEL}.getElementById('choose').hidden") is True
    assert browser.evaluate(f"{PANEL}.getElementById('elsewhere').hidden") is False
    href = browser.evaluate(f"{PANEL}.getElementById('link').href")
    assert href == f"{BASE}/build/books/upload?path={PATH}"


@pytest.mark.browser
@pytest.mark.slow
@needs_browser
def test_a_token_created_in_the_panel_shows_its_secret_there_alone(
    shop, builder: Builder, host: str, browser: screenshot.Browser
) -> None:
    browser.visit(host, 768)
    asked = {"name": "Till import", "apps": [{"app": "shop", "role": "staff"}]}
    browser.evaluate(f"window.start({json.dumps(asked)}, true, 'create_token', 'token.html')")
    ask = f"{PANEL}.getElementById('ask')"
    wait_for(browser, f"{ask} && !{ask}.hidden")
    assert browser.evaluate(f"{PANEL}.getElementById('name').textContent") == "Till import"
    assert "as these roles, for 90 days, unless you revoke it sooner" in browser.evaluate(
        f"{PANEL}.getElementById('lead').textContent"
    )
    row = f"{PANEL}.getElementById('apps').firstElementChild"
    assert browser.evaluate(f"[...{row}.children].map((part) => part.textContent)") == [
        "etienne/shop",
        "as staff",
    ]

    browser.evaluate(f"{PANEL}.getElementById('create').click()")
    secret = wait_for(
        browser,
        f"!{PANEL}.getElementById('made').hidden && {PANEL}.getElementById('secret').textContent",
    )

    database = builder.apps.database
    etienne = database.person_by_login("etienne")
    assert etienne is not None
    (made,) = database.tokens(etienne.id)
    assert made.name == "Till import" and made.made_by is None
    assert made.expires_at == NOW + timedelta(days=90)
    found = database.token_access(secret, NOW)
    assert found is not None and found[1] == (TokenLimit("etienne", "shop", "staff"),)
    assert [call["tool"] for call in browser.evaluate("window.calls")] == ["issue_token"]
    assert browser.evaluate(f"{PANEL}.getElementById('copy').textContent") == "Copy"
    # Copied, or selected for the person to copy, where the frame may not use the clipboard.
    browser.evaluate(f"{PANEL}.getElementById('copy').click()")
    copied = f"{PANEL}.getElementById('copy').textContent === 'Copied'"
    wait_for(browser, f"{copied} || frame.contentWindow.getSelection().toString() !== ''")
    assert "shown only this once" in browser.evaluate(f"{PANEL}.getElementById('made').textContent")
    tell = browser.evaluate(f"{PANEL}.getElementById('tell').textContent")
    assert tell == "Tell your assistant the token is made."
    # The assistant learns the token's name and id, never its secret.
    contexts = wait_for(browser, "window.contexts.length && window.contexts")
    assert made.public_id in json.dumps(contexts) and secret not in json.dumps(contexts)
    # Drawn again, the panel offers nothing more to make.
    browser.evaluate("window.notifyAgain()")
    assert browser.evaluate(f"{ask}.hidden") is True
    assert browser.evaluate(f"{PANEL}.getElementById('secret').textContent") == secret
