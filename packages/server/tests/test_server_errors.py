"""The errors of a published app's code, in its calls and in its start, kept for its owner: what
is kept, its bounds, and who reads it, in the console and with the builder's `read_error`."""

import re
from concurrent.futures import Future
from datetime import timedelta
from html import unescape
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from cliquet.context import Caller
from cliquet_server.app_errors import keeper, summary
from cliquet_server.apps import Apps, Live
from cliquet_server.builder import Builder, BuilderError
from cliquet_server.db import (
    ERROR_START,
    ERRORS_KEPT,
    MAX_ERROR_TEXT,
    SAME_START_FOR,
    STARTS_KEPT,
    Database,
    ErrorInfo,
    PersonInfo,
)
from cliquet_server.executor import SandboxError, SandboxFault
from cliquet_server.me import READ, WRITE
from cliquet_server.repos import AppRepo

from .server_helpers import (
    ERROR_ID,
    NOW,
    a_shop_member,
    act_as,
    call,
    failing_build,
    one_unexpected,
    publish_failing,
    signed_in,
    text,
)

# What a caller who isn't the owner reads of an error in the app's code.
SENTENCE = (
    rf"The app failed with a KeyError\. Nothing was changed\. Error ({ERROR_ID}) has the "
    r"details: the app's owner can read it in the app's console\."
)


# What anyone but the owner reads of why the app can't be started.
NOT_STARTED = (
    rf"Error ({ERROR_ID}) has the details: the app's owner can read it in the app's console\."
)


def kept(server: Starlette, app: str = "shop") -> list[Any]:
    """The errors kept for etienne's app, newest first."""
    return server.state.apps.database.errors("etienne", app, NOW)


def rpc(server: Starlette, person: PersonInfo | None, role: str, name: str) -> dict[str, Any]:
    """A screen's call to etienne/shop as `role`: by `person`, or by etienne, its owner."""
    body = {"method": "tools/call", "params": {"name": name, "arguments": {}}}
    if person is None:
        with signed_in(server) as client:
            return client.post(f"/apps/etienne/shop/{role}/rpc", json=body).json()
    with TestClient(server) as client:
        act_as(server, client, person)
        return client.post(f"/apps/etienne/shop/{role}/rpc", json=body).json()


def test_a_member_s_failed_call_is_kept_for_the_owner(server: Starlette) -> None:
    sam = a_shop_member(server)
    publish_failing(server)

    # From the person's endpoint, and from a screen: the member reads where the details are.
    asked = call(server, "sam", READ, {"app": "etienne/shop", "tool": "secret"})
    told = re.fullmatch(SENTENCE, text(asked))
    assert asked.is_error and told is not None
    screen = rpc(server, sam, "owner", "broken")
    drawn = re.fullmatch(SENTENCE, screen["content"][0]["text"])
    assert screen["isError"] and drawn is not None
    # The owner's own failed call is kept too: a routine of theirs may be the one that fails.
    own = rpc(server, None, "staff", "secret")
    assert "KeyError: 'Sencha costs 4.20'" in own["content"][0]["text"]

    errors = kept(server)
    assert [(one.caller, one.role, one.tool) for one in errors] == [
        ("etienne", "staff", "secret"),
        ("sam", "owner", "broken"),
        ("sam", "owner", "secret"),
    ]
    assert [one.id for one in errors[1:]] == [drawn.group(1), told.group(1)]
    # A list reads the start of each text. One error read by its id is whole: what the
    # owner reads, then the traceback, down to the line of app.py that raised.
    first = server.state.apps.database.error("etienne", "shop", errors[-1].id, NOW)
    assert first is not None and first.at == NOW
    assert first.text.startswith(errors[-1].text)
    assert first.text.startswith(
        "The app failed: KeyError: 'Sencha costs 4.20'. Nothing was changed.\n\nTraceback"
    )
    assert "In the sandbox:" in first.text and "app.py" in first.text
    assert 'raise KeyError("Sencha costs 4.20")' in first.text

    # The owner finds it by its id: in the console's Errors tab, on its own page, and with
    # the builder's read_error.
    with signed_in(server) as client:
        overview = client.get("/manage/etienne/shop")
        tab = client.get("/manage/etienne/shop/errors")
        page = client.get(f"/manage/etienne/shop/errors/{first.id}")
        missing = client.get("/manage/etienne/shop/errors/e-000000000000")
    assert 'href="/manage/etienne/shop/errors"' in overview.text
    assert tab.status_code == 200
    for one in errors:
        assert f'href="/manage/etienne/shop/errors/{one.id}"' in tab.text
    assert "Sencha costs 4.20" in tab.text and "Traceback" not in tab.text
    assert tab.text.index(errors[0].id) < tab.text.index(first.id)
    assert page.status_code == 200
    assert first.id in page.text and "Traceback" in page.text and "sam" in page.text
    assert "raise KeyError(&#34;Sencha costs 4.20&#34;)" in page.text
    assert missing.status_code == 404 and "kept 30 days" in missing.text

    builder: Builder = server.state.builder
    assert builder.read_error("etienne", "shop", first.id) == {
        "id": first.id,
        "when": NOW.isoformat(),
        "during": "call",
        "caller": "sam",
        "role": "owner",
        "tool": "secret",
        "text": first.text,
    }
    described = builder.read_app_overview("etienne", "shop")["errors"]
    assert described["count"] == 3
    assert [one["id"] for one in described["latest"]] == [one.id for one in errors]
    assert all("Traceback" not in str(one) for one in described["latest"])
    assert described["latest"][-1] == {
        "id": first.id,
        "when": NOW.isoformat(),
        "during": "call",
        "caller": "sam",
        "role": "owner",
        "tool": "secret",
        "line": "The app failed: KeyError: 'Sencha costs 4.20'. Nothing was changed.",
    }
    assert "read_error(app, error)" in described["note"]
    with pytest.raises(BuilderError, match="call read_app_overview to see the latest ones"):
        builder.read_error("etienne", "shop", "e-000000000000")


def test_only_an_error_of_the_published_app_s_code_is_kept(
    server: Starlette, monkeypatch: pytest.MonkeyPatch
) -> None:
    sam = a_shop_member(server)
    publish_failing(server)
    apps: Apps = server.state.apps
    builder: Builder = server.state.builder

    # A refusal is the app's own answer.
    sale = {"sku": "POT-GLS", "quantity": 99, "receipt": "R-0900"}
    refused = call(
        server, "sam", WRITE, {"app": "etienne/shop", "tool": "record_sale", "arguments": sale}
    )
    assert refused.is_error and text(refused).startswith("Refused by: Stock never")
    # A call on the draft's preview: the owner's agent reads its error in full already.
    builder.start_preview("etienne", "shop")
    tried = builder.try_call("etienne", "shop", "secret", role="staff")
    assert "Sencha costs 4.20" in str(tried)
    builder.stop_preview("etienne", "shop")

    client = apps.executor.sandbox("etienne")

    def unknown(self: object, login: str, app: str, version: str, request: dict) -> dict:
        return {"error": {"kind": "unknown", "message": "Sencha costs 4.20"}}

    def away(self: object, login: str, app: str, version: str, request: dict) -> dict:
        raise SandboxFault("the sandbox can't be reached")

    # Cliquet's own fixed words, which every caller reads, and a fault of the sandbox.
    with monkeypatch.context() as patched:
        patched.setattr(type(client), "run", unknown)
        safe = rpc(server, sam, "owner", "secret")
        patched.setattr(type(client), "run", away)
        fault = rpc(server, sam, "owner", "secret")
    assert "there is no query named secret" in safe["content"][0]["text"]
    assert "the sandbox can't be reached" in fault["content"][0]["text"]

    # A start that fails in Cliquet's own words, which the member reads too: the sandbox's,
    # or the server's own. The builder says to try again, not to fix the app.
    def broken(login: str, name: str) -> object:
        raise RuntimeError("Sencha costs 4.20")

    apps.forget("etienne", "shop")
    with monkeypatch.context() as patched:
        patched.setattr(type(client), "put_version", away)
        cant = call(server, "sam", READ, {"app": "etienne/shop", "tool": "secret"})
        described = builder.read_app_overview("etienne", "shop")
        apps.forget("etienne", "shop")
        patched.setattr(apps, "_start", broken)
        failed = call(server, "sam", READ, {"app": "etienne/shop", "tool": "secret"})
    assert text(cant) == "etienne/shop can't be started: the sandbox can't be reached"
    assert described["problem"].endswith("the sandbox can't be reached")
    assert described["next"] == "Call read_app_overview again in a minute."
    assert text(failed) == (
        "etienne/shop can't be started: the server failed to start it (RuntimeError): try "
        "again in a minute"
    )

    assert kept(server) == []
    assert builder.read_app_overview("etienne", "shop")["errors"]["count"] == 0
    with signed_in(server) as owner:
        assert "No error in the last 30 days." in owner.get("/manage/etienne/shop/errors").text


def test_a_failed_start_is_kept_for_the_owner(
    server: Starlette, monkeypatch: pytest.MonkeyPatch
) -> None:
    apps: Apps = server.state.apps
    builder: Builder = server.state.builder
    sam = a_shop_member(server)
    apps.forget("etienne", "shop")
    code = AppRepo.code

    def unloadable(self: AppRepo) -> dict[str, bytes]:
        files = code(self)
        return {**files, "app.py": files["app.py"] + b'\nraise KeyError("Sencha costs 4.20")\n'}

    # The live code can't be loaded any more, as when a new SDK refuses it.
    monkeypatch.setattr(AppRepo, "code", unloadable)
    # A member reads an id in place of why: on the app's page, and from the person's endpoint.
    with TestClient(server) as client:
        act_as(server, client, sam)
        page = client.get("/apps/etienne/shop/owner")
    asked = call(server, "sam", READ, {"app": "etienne/shop", "tool": "stock"})
    assert page.status_code == 503 and "Sencha" not in page.text
    # Without Retry-After: a page that waited for the start stops asking, and shows this.
    assert "retry-after" not in page.headers
    shown = re.search(NOT_STARTED, unescape(page.text))
    told = re.fullmatch(rf"etienne/shop can't be started: {NOT_STARTED}", text(asked))
    assert shown is not None and told is not None and "Sencha" not in text(asked)

    # Kept once, under the id they read, however often it is tried again: by the server's
    # warm-up, by requests once a minute, and at once by the builder.
    apps.failed_for = 0
    call(server, "sam", READ, {"app": "etienne/shop", "tool": "stock"})
    described = builder.read_app_overview("etienne", "shop")
    (error,) = kept(server)
    assert error.id == shown.group(1) == told.group(1)
    assert error.start and (error.caller, error.role, error.tool) == (None, None, None)
    whole = apps.database.error("etienne", "shop", error.id, NOW)
    assert whole is not None and whole.text.startswith("The app can't be started: ")
    # Where the app's code raised, as the sandbox says it, once: never the server's own frames.
    assert whole.text.count('raise KeyError("Sencha costs 4.20")') == 1 and "app.py" in whole.text
    assert "cliquet_server" not in whole.text

    # The owner reads it while the app can't be started: in the Errors tab and on its page.
    # Another tab says why it can't be shown, and where the error is.
    with signed_in(server) as client:
        tab = client.get("/manage/etienne/shop/errors")
        one = client.get(f"/manage/etienne/shop/errors/{error.id}")
        tabs = [client.get("/manage/etienne/shop"), client.get("/manage/etienne/shop/history")]
    listed, read = unescape(tab.text), unescape(one.text)
    assert tab.status_code == 200 and '<td colspan="2">The app\'s start</td>' in listed
    assert f'href="/manage/etienne/shop/errors/{error.id}"' in listed
    assert "The app can't be started: " in listed and 'raise KeyError("' not in listed
    assert one.status_code == 200 and "the app couldn't be started" in read
    assert 'raise KeyError("Sencha costs 4.20")' in read
    for name, other in zip(("Overview", "History"), tabs, strict=True):
        page = unescape(other.text)
        assert other.status_code == 503
        assert "This needs the live app, which can't be started now." in page
        assert "Sencha costs 4.20" in page
        assert f'href="/manage/etienne/shop/errors/{error.id}"' in page
        assert 'href="/manage/etienne/shop/errors">Open the Errors tab' in page
        assert f'aria-current="page">{name}</a>' in page

    # The builder says why it can't be started, with its errors, and reads each one.
    assert described["problem"].startswith("The live version can't be started: ")
    assert "Sencha costs 4.20" in described["problem"]
    assert f"read_error(app, '{error.id}')" in described["next"]
    # The owner can still download the data and its history.
    assert described["download_url"].endswith("/manage/etienne/shop/download")
    assert described["errors"]["count"] == 1
    line = summary(error)["line"]
    assert line.startswith("The app can't be started: ")
    assert described["errors"]["latest"] == [
        {"id": error.id, "when": NOW.isoformat(), "during": "start", "line": line}
    ]
    assert builder.read_error("etienne", "shop", error.id) == {
        "id": error.id,
        "when": NOW.isoformat(),
        "during": "start",
        "text": whole.text,
    }

    # Another failure is another error, newest first.
    with failing_build(apps, "shop", monkeypatch):
        builder.read_app_overview("etienne", "shop")
    newest, *older = kept(server)
    assert [one.id for one in older] == [error.id]
    assert newest.start and newest.text == "The app can't be started: the sandbox is away"


def test_a_failed_start_is_kept_once_while_it_fails_the_same_way(database: Database) -> None:
    database.create_app("etienne", "shop", NOW)
    ids = (f"start-{number}" for number in range(100))

    def again(text: str, now: Any = NOW) -> str | None:
        return database.keep_start_error("etienne", "shop", now, text, lambda: next(ids))

    assert again("It can't load.") == "start-0"
    # The same failure again: its id, and no new id is asked for.
    assert again("It can't load.") == "start-0"
    assert again("It can't reach its sandbox.") == "start-1"
    # The first failure again is a new one: it is no longer the latest.
    assert again("It can't load.") == "start-2"
    # So is one after a failed call, and one more than a day after the latest, as the app
    # may well have started in between.
    keep(database, 99)
    assert again("It can't load.") == "start-3"
    later = NOW + SAME_START_FOR + timedelta(seconds=1)
    assert again("It can't load.", later) == "start-4"
    assert database.keep_start_error("etienne", "nothing", NOW, "No.", lambda: "e-1") is None
    starts = [one.id for one in database.errors("etienne", "shop", later) if one.start]
    assert starts == [f"start-{number}" for number in (4, 3, 2, 1, 0)]

    # An app keeps its last 20 failed starts, whatever their text, and its calls' errors.
    for number in range(STARTS_KEPT):
        again(f"It can't load: try {number}.", later)
    errors = database.errors("etienne", "shop", later)
    assert [one.id for one in errors if one.start] == [f"start-{n}" for n in range(24, 4, -1)]
    assert [one.id for one in errors if not one.start] == [f"e-{99:012x}"]
    assert STARTS_KEPT == 20 and SAME_START_FOR.days == 1


def test_a_failed_start_that_a_publish_dropped_keeps_nothing(server: Starlette) -> None:
    apps: Apps = server.state.apps
    # It failed as a publish made a version live: it is no longer the app's start.
    dropped: Future[Live] = Future()
    apps._failed(dropped, "etienne", "shop", SandboxError("app.py: KeyError: 'Sencha'"))

    assert isinstance(dropped.exception(), SandboxError)
    assert kept(server) == []


def keep(database: Database, number: int, text: str = "It failed.", **changes: Any) -> None:
    """Keep the error `number` of etienne/shop, called by sam as staff."""
    given: dict[str, Any] = {"now": NOW, "caller": "sam", "role": "staff", "tool": "stock"}
    given.update(changes)
    assert database.keep_error("etienne", "shop", f"e-{number:012x}", text=text, **given)


def test_what_is_kept_is_bounded(database: Database) -> None:
    database.create_app("etienne", "shop", NOW)
    database.create_app("etienne", "other", NOW)
    assert database.keep_error("etienne", "other", "e-1", NOW, "sam", None, "stock", "Kept.")
    assert not database.keep_error("etienne", "nothing", "e-1", NOW, "sam", None, "stock", "No.")

    # The 201st error drops the oldest, of this app only.
    for number in range(ERRORS_KEPT):
        keep(database, number)
    assert database.error_count("etienne", "shop", NOW) == ERRORS_KEPT
    keep(database, ERRORS_KEPT)
    ids = [one.id for one in database.errors("etienne", "shop", NOW)]
    assert len(ids) == ERRORS_KEPT
    assert ids[0] == f"e-{ERRORS_KEPT:012x}" and ids[-1] == f"e-{1:012x}"
    assert database.error("etienne", "shop", f"e-{0:012x}", NOW) is None
    assert [one.id for one in database.errors("etienne", "shop", NOW, 2)] == ids[:2]
    assert database.error_count("etienne", "other", NOW) == 1

    # An error older than 30 days is gone: no longer read, then removed when one is added.
    later = NOW + timedelta(days=30, seconds=1)
    assert database.errors("etienne", "shop", later) == []
    assert database.error("etienne", "shop", ids[0], later) is None
    assert database.error_count("etienne", "shop", later) == 0
    keep(database, 1000, now=later)
    assert [one.id for one in database.errors("etienne", "shop", NOW)] == [f"e-{1000:012x}"]

    # A long text is cut to 16 KB, between two characters: it keeps its start, which says
    # what failed, and its end, where the traceback says where. A list reads only its start.
    text = f"It failed.\n{'é' * MAX_ERROR_TEXT}\nKeyError: 'x'"
    keep(database, 1001, text=text, now=later)
    long = database.error("etienne", "shop", f"e-{1001:012x}", later)
    assert long is not None and MAX_ERROR_TEXT - 4 < len(long.text.encode()) <= MAX_ERROR_TEXT
    assert long.text.startswith("It failed.\nééé") and long.text.endswith("ééé\nKeyError: 'x'")
    assert long.text.count("é\n[cut to 16 KB]\né") == 1
    listed = database.errors("etienne", "shop", later, 1)[0]
    assert listed.id == long.id and listed.text == long.text[:ERROR_START]
    assert MAX_ERROR_TEXT == 16 * 1024 and ERRORS_KEPT == 200

    # An app that fails no more loses its old errors too, when the server starts.
    assert database.error_count("etienne", "other", NOW) == 1
    database.drop_old_errors(later)
    assert database.errors("etienne", "other", NOW) == []
    assert database.error_count("etienne", "shop", later) == 2


def test_a_list_s_line_says_what_failed() -> None:
    def line(text: str) -> str:
        return summary(ErrorInfo("e-1", NOW, "sam", None, "count", text))["line"]

    assert line("The app failed: KeyError: 'x'. Nothing was changed.\n\nTraceback") == (
        "The app failed: KeyError: 'x'. Nothing was changed."
    )
    # A result that doesn't fit is a heading, then its problems: the first one says what.
    assert line("The query 'count' failed:\n- count returned 'none'\n- more\n\nTraceback") == (
        "The query 'count' failed: count returned 'none'"
    )
    assert len(line("x" * 1000)) == 300


def test_an_app_s_errors_are_deleted_with_it(server: Starlette) -> None:
    apps: Apps = server.state.apps
    publish_failing(server)
    assert rpc(server, None, "staff", "secret")["isError"]
    assert len(kept(server)) == 1

    apps.delete("etienne", "shop")
    # An app made later with the same name starts with none.
    apps.database.create_app("etienne", "shop", NOW)

    assert kept(server) == []


def test_the_errors_tab_needs_no_start(server: Starlette, monkeypatch: pytest.MonkeyPatch) -> None:
    apps: Apps = server.state.apps
    publish_failing(server)
    assert rpc(server, None, "staff", "secret")["isError"]
    (error,) = kept(server)
    apps.forget("etienne", "shop")

    # While the app can't be started, its errors are read from the database, and any other
    # tab says why it can't be shown and points to them.
    with failing_build(apps, "shop", monkeypatch), signed_in(server) as client:
        tab = client.get("/manage/etienne/shop/errors")
        one = client.get(f"/manage/etienne/shop/errors/{error.id}")
        tools = client.get("/manage/etienne/shop/tools")

    assert tab.status_code == 200 and error.id in tab.text
    assert one.status_code == 200 and "Sencha costs 4.20" in one.text
    page = unescape(tools.text)
    assert tools.status_code == 503 and "the sandbox is away" in page
    assert 'href="/manage/etienne/shop/errors">Open the Errors tab' in page
    assert 'aria-current="page">Tools and rules</a>' in page
    # The app's header and tabs, from its record.
    assert "<h1>Shop</h1>" in page and "<h1>Shop</h1>" in tab.text


def test_only_the_owner_reads_an_error(server: Starlette) -> None:
    database: Database = server.state.apps.database
    builder: Builder = server.state.builder
    member = a_shop_member(server)
    stranger = database.person_for(
        "https://example.eu.auth0.com/", "auth0|kim", "kim@example.com", "kim", NOW
    )
    # As an app's code may write it: its text and the tool's name are escaped on a page.
    markup = "KeyError: '<script>alert(1)</script>'\n\nTraceback <b>bold</b>"
    keep(database, 7, text=markup, tool="<i>stock</i>")
    error_id = f"e-{7:012x}"
    pages = ["/manage/etienne/shop/errors", f"/manage/etienne/shop/errors/{error_id}"]

    with TestClient(server) as client:
        anonymous = [client.get(page, follow_redirects=False) for page in pages]
        refused = []
        for person in (member, stranger):
            act_as(server, client, person)
            refused += [client.get(page) for page in pages]
    with signed_in(server) as client:
        shown = [client.get(page) for page in pages]

    for page in anonymous:
        assert page.status_code == 303 and page.headers["location"].startswith("/login?next=")
    for page in refused:
        assert page.status_code == 404
        assert "There is no app etienne/shop here." in page.text
        assert "alert(1)" not in page.text and error_id not in page.text
    for page in shown:
        assert page.status_code == 200
        assert "<script>alert" not in page.text and "<i>stock" not in page.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
        assert "&lt;i&gt;stock&lt;/i&gt;" in page.text
    assert "<b>bold" not in shown[1].text and "&lt;b&gt;bold&lt;/b&gt;" in shown[1].text

    # The builder's tools see only the caller's own apps.
    for login in ("sam", "kim"):
        with pytest.raises(BuilderError, match="You have no app named shop"):
            builder.read_error(login, "shop", error_id)
    # A member's own app of the same name has its own errors, never this one.
    database.create_app("sam", "shop", NOW)
    with pytest.raises(BuilderError, match="shop has no error with the id"):
        builder.read_error("sam", "shop", error_id)
    assert builder.read_error("etienne", "shop", error_id)["text"] == markup


def test_an_error_that_can_t_be_kept_is_logged_as_unexpected(
    apps: Apps, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*args: object) -> None:
        raise OSError("the disk is full")

    monkeypatch.setattr(apps.database, "keep_error", broken)
    keep = keeper(apps, "etienne", "shop")
    # The call still answers, with no sentence: the log has the error under its id.
    assert keep(Caller("sam", "owner"), "secret", "e-0123456789ab", "KeyError") is None
    record = one_unexpected(caplog, OSError)
    assert record.getMessage() == "unexpected: etienne/shop: the error e-0123456789ab wasn't kept"


def test_the_overview_warns_of_recent_errors(server: Starlette) -> None:
    database: Database = server.state.apps.database
    for number in range(2):
        database.keep_error(
            "etienne", "shop", f"e{number}", NOW, "sam", "staff", "record_sale", f"Boom {number}"
        )
    with signed_in(server) as client:
        text = client.get("/manage/etienne/shop").text

    warning = text.split('id="overview-errors"')[1].split("</div>\n</div>")[0]
    assert "2 errors in the last" in warning and "Boom 1" in warning
    assert 'href="/manage/etienne/shop/errors"' in warning
