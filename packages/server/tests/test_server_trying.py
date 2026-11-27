import json
from pathlib import Path

import anyio
import pytest
from fastmcp import Client as BuilderClient

from cliquet import Caller, screenshot
from cliquet.tools import NoSuchRole
from cliquet_server.builder import Builder, BuilderError
from cliquet_server.builder.previews import PreviewEnded, Previews
from cliquet_server.builder.publishing import PREVIEW_ENDED
from cliquet_server.builder.server import _try_view_text
from cliquet_server.builder.trying import NOT_MADE_AGAIN
from cliquet_server.executor import SandboxError, Scripted

from .server_helpers import (
    MIGRATION,
    STAFF,
    draft_example,
    example_data,
    failing_build,
    me_server,
    names,
    published,
    stock_quantity,
)

pytestmark = [pytest.mark.browser, pytest.mark.slow]
TRANSACTION_TOOLS = {
    "app_begin_transaction",
    "app_transaction_calls",
    "app_commit_transaction",
    "app_rollback_transaction",
}

STEPS = [
    {"fill": "Receipt", "with": "R-0900"},
    {"choose": "Product", "option": "Sencha green tea 100 g"},
    {"click": "Add to receipt"},
    {"click": "Record receipt"},
]

needs_browser = pytest.mark.skipif(
    screenshot.find_browser() is None, reason="needs a Chrome, Chromium or Edge"
)


SALE = {"sku": "TEA-SEN", "quantity": 2, "receipt": "R-0900"}
NO_PREVIEW = "No preview is running for shop. Call start_preview first."
COUNT_QUERY = '''

@shop.query(roles=["owner", "staff"])
def movement_count(ctx: QueryContext) -> int:
    """How many stock movements there are."""
    return len(_read(MOVEMENTS))
'''

COUNT_ACTION = '''

class Counted(BaseModel):
    movements: int


@shop.action(roles=["owner", "staff"])
def count_movements(ctx: ActionContext) -> Counted:
    """Count the stock movements, changing nothing."""
    return Counted(movements=len(_read(MOVEMENTS)))
'''


def test_try_call_shows_an_action_result(shop, builder: Builder) -> None:
    source = builder.read_file("etienne", "shop", "app.py")["text"]
    builder.write_file("etienne", "shop", "app.py", source + COUNT_ACTION)
    builder.start_preview("etienne", "shop")
    movements = len(example_data()["movements.csv"].decode().strip().splitlines()) - 1

    counted = builder.try_call("etienne", "shop", "count_movements", role="staff")
    assert counted["ok"] is True, counted["text"]
    assert counted["result"] == {"commit": None, "files": [], "result": {"movements": movements}}
    assert counted["text"] == (
        f'Done. Nothing needed to change. The action answered: {{"movements": {movements}}}'
    )

    builder.try_call("etienne", "shop", "record_sale", SALE, role="staff")
    again = builder.try_call("etienne", "shop", "count_movements", role="owner")
    assert again["result"]["result"] == {"movements": movements + 1}


def test_trying_needs_a_preview(shop, builder: Builder) -> None:
    tries = [
        lambda: builder.try_call("etienne", "shop", "stock", role="staff"),
        lambda: builder.try_view("etienne", "shop", "till", STEPS, role="staff"),
        lambda: builder.show_preview("etienne", "shop", "till", role="staff"),
        lambda: builder.take_screenshots("etienne", "shop", role="staff"),
    ]
    for attempt in tries:
        with pytest.raises(BuilderError) as error:
            attempt()
        assert str(error.value) == NO_PREVIEW

    builder.start_preview("etienne", "shop")
    builder.stop_preview("etienne", "shop")
    with pytest.raises(BuilderError, match="start_preview"):
        builder.try_call("etienne", "shop", "stock", role="staff")


def test_start_preview_says_what_it_runs_on(shop, builder: Builder) -> None:
    started = builder.start_preview("etienne", "shop")

    sizes = {name: len(content) for name, content in example_data().items()}
    assert started["app"] == "shop"
    assert started["files"] == [
        {"path": f"data/{name}", "size": size} for name, size in sorted(sizes.items())
    ]
    assert started["link"] == "http://127.0.0.1:8800/build/shop/preview"
    assert "try_call" in started["next"] and "show_preview" in started["next"]


def test_a_preview_keeps_its_data(shop, builder: Builder) -> None:
    head = builder.apps.repo("etienne", "shop").head()
    builder.start_preview("etienne", "shop")

    sale = builder.try_call("etienne", "shop", "record_sale", SALE, role="staff")
    after = builder.try_call("etienne", "shop", "stock", role="staff")

    assert sale["ok"] is True
    assert "movements.csv" in sale["text"]
    assert sale["note"] == "On the preview: nothing is live."
    assert "notice" not in sale
    assert stock_quantity(after, "TEA-SEN") == 9
    assert builder.apps.repo("etienne", "shop").head() == head
    live = shop.runtime.query_json(STAFF, "stock", {})
    assert next(row["quantity"] for row in live if row["sku"] == "TEA-SEN") == 11


def test_a_preview_rehearses_a_transaction(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    session = builder.previews.find("etienne", "shop")
    assert session is not None

    begun = builder.try_call("etienne", "shop", "app_begin_transaction", role="staff")
    assert begun["ok"] is True, begun["text"]
    t = begun["result"]["transaction"]
    sale = builder.try_call(
        "etienne", "shop", "record_sale", {**SALE, "app_transaction": t}, "staff"
    )
    assert sale["ok"] is True and sale["text"].startswith(f"In transaction {t}: Done.")
    inside = builder.try_call("etienne", "shop", "stock", {"app_transaction": t}, role="staff")
    outside = builder.try_call("etienne", "shop", "stock", role="staff")
    assert (stock_quantity(inside, "TEA-SEN"), stock_quantity(outside, "TEA-SEN")) == (9, 11)
    # Every role and the preview's page call as "preview": one transaction for the preview.
    again = builder.preview_call(
        "etienne", session.id, session.build.app_py, "app_begin_transaction", {}, "owner"
    )
    assert again.is_error and t in again.content[0].text
    committed = builder.try_call(
        "etienne", "shop", "app_commit_transaction", {"app_transaction": t}, role="owner"
    )
    assert committed["ok"] is True, committed["text"]
    kept = builder.try_call("etienne", "shop", "stock", role="staff")
    assert stock_quantity(kept, "TEA-SEN") == 9
    with session.using("staff") as server:
        assert {tool.name for tool in server.tools} >= TRANSACTION_TOOLS

    # A change to the draft's code ends the transaction, as a publish would.
    clicked = builder.preview_call(
        "etienne", session.id, session.build.app_py, "app_begin_transaction", {}, "staff"
    )
    t2 = clicked.structured_content["transaction"]
    source = builder.read_file("etienne", "shop", "app.py")["text"]
    builder.write_file("etienne", "shop", "app.py", source + COUNT_QUERY)
    ended = builder.try_call("etienne", "shop", "stock", {"app_transaction": t2}, role="staff")
    assert ended["ok"] is False and "ended when a new version of the app" in ended["text"]
    assert (
        stock_quantity(builder.try_call("etienne", "shop", "stock", role="staff"), "TEA-SEN") == 9
    )


def test_the_preview_s_caller_is_the_owner(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    session = builder.previews.find("etienne", "shop")
    assert session is not None
    for role in ("owner", "staff"):
        assert session.runtime.is_owner(Caller("preview", role))
    # So the person building the app is told which commits were compared, in every role.
    asked = {"query": "stock", "since": "2020-01-01T00:00:00+00:00"}
    changed = builder.try_call("etienne", "shop", "app_changes_since", asked, role="staff")
    assert changed["ok"] is True, changed["text"]
    assert changed["result"]["now"] == session.runtime.store.head()


def test_every_role_shares_the_preview(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")

    builder.try_call("etienne", "shop", "record_sale", SALE, role="staff")
    seen = builder.try_call("etienne", "shop", "stock", role="owner")

    assert stock_quantity(seen, "TEA-SEN") == 9


def test_a_code_change_keeps_the_preview_data(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    builder.try_call("etienne", "shop", "record_sale", SALE, role="staff")
    movements = example_data()["movements.csv"].decode().strip().splitlines()
    source = builder.read_file("etienne", "shop", "app.py")["text"]

    builder.write_file("etienne", "shop", "app.py", source + COUNT_QUERY)
    count = builder.try_call("etienne", "shop", "movement_count", role="staff")
    stock = builder.try_call("etienne", "shop", "stock", role="staff")

    assert count["ok"] is True, count["text"]
    assert count["result"]["result"] == len(movements)
    assert stock_quantity(stock, "TEA-SEN") == 9


def test_a_preview_says_when_the_current_data_changed(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "data/items.csv", "name,note\nMilk,one\n")
    builder.start_preview("etienne", "tasks")
    assert "notice" not in builder.try_call("etienne", "tasks", "items")

    builder.write_file("etienne", "tasks", "data/items.csv", "name,note\nMilk,two\n")
    after = builder.try_call("etienne", "tasks", "items")

    # Each preview tool's answer says it, from the lookup the tool made.
    assert after["notice"] == (
        "The current data changed since this preview started. "
        "Call start_preview to start again from it."
    )
    assert "one" in json.dumps(after["result"])

    async def main() -> None:
        async with BuilderClient(me_server(builder.apps, builder)) as client:
            tried = await client.call_tool("try_call", {"app": "tasks", "tool": "items"})
        assert tried.structured_content is not None
        assert tried.structured_content["notice"].startswith("The current data changed")

    anyio.run(main)
    builder.start_preview("etienne", "tasks")
    again = builder.try_call("etienne", "tasks", "items")
    assert "notice" not in again
    assert "two" in json.dumps(again["result"])


def test_the_preview_tools_say_when_the_current_data_changed(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    shop.runtime.call(STAFF, "record_sale", SALE)

    async def main() -> None:
        async with BuilderClient(me_server(builder.apps, builder)) as client:
            shown = await client.call_tool(
                "show_preview", {"app": "shop", "view": "till", "role": "staff"}
            )
            started = await client.call_tool("start_preview", {"app": "shop"})
            again = await client.call_tool(
                "show_preview", {"app": "shop", "view": "till", "role": "staff"}
            )
        assert "The current data changed" in shown.content[-1].text
        assert started.structured_content is not None and "notice" not in started.structured_content
        assert not any("current data changed" in part.text for part in again.content)

    anyio.run(main)


def test_an_unchanged_draft_is_not_read_again(shop, builder: Builder, monkeypatch) -> None:
    builder.start_preview("etienne", "shop")
    session = builder.previews.find("etienne", "shop")
    assert session is not None
    reads: list[str] = []
    code = builder.drafts.code

    def counted(login: str, app: str) -> dict[str, bytes]:
        reads.append(app)
        return code(login, app)

    monkeypatch.setattr(builder.drafts, "code", counted)
    # The preview's own build wrote the lock: no part of the code.
    for _ in range(2):
        assert builder.try_call("etienne", "shop", "stock", role="staff")["ok"]
    assert reads == []
    # The same code written again is read once, and not built again.
    version = session.version
    source = builder.read_file("etienne", "shop", "app.py")["text"]
    builder.write_file("etienne", "shop", "app.py", source)
    for _ in range(2):
        assert builder.try_call("etienne", "shop", "stock", role="staff")["ok"]
    assert reads == ["shop"] and session.version == version
    # New code is read once, and the preview runs it.
    builder.write_file("etienne", "shop", "app.py", source + COUNT_QUERY)
    for _ in range(2):
        assert builder.try_call("etienne", "shop", "movement_count", role="staff")["ok"]
    assert reads == ["shop", "shop"] and session.version != version


def test_a_draft_that_doesnt_build_is_built_once_until_written(
    shop, builder: Builder, monkeypatch
) -> None:
    builder.start_preview("etienne", "shop")
    info = builder.apps.database.app("etienne", "shop")
    assert info is not None
    client = builder.apps.executor.sandbox("etienne")
    real = type(client).put_version
    builds: list[str] = []

    def counted(self, login: str, app: str, version: str, files: dict[str, bytes]):
        builds.append(version)
        return real(self, login, app, version, files)

    monkeypatch.setattr(type(client), "put_version", counted)
    builder.write_file("etienne", "shop", "app.py", "this isn't Python\n")

    def follow_twice() -> None:
        for _ in range(2):
            with pytest.raises(BuilderError, match="can't be built"):
                builder.try_call("etienne", "shop", "stock", role="staff")
        # The draft's page says why from what the preview kept, and runs the last build.
        found = builder.draft_preview(info)
        assert found is not None and "can't be built" in (found[1] or "")

    follow_twice()
    assert len(builds) == 1
    # A write builds it again, once.
    builder.write_file("etienne", "shop", "app.py", "this isn't Python either\n")
    follow_twice()
    assert len(builds) == 2
    # So does check_app, and the preview's next follow after it.
    assert builder.check_app("etienne", "shop")["ok"] is False
    follow_twice()
    assert len(builds) == 4
    # And run_tests, which builds it for real too.
    assert builder.run_tests("etienne", "shop")["passed"] is False
    follow_twice()
    assert len(builds) == 6


def test_a_sandbox_that_failed_a_build_is_asked_again(shop, builder: Builder, monkeypatch) -> None:
    builder.start_preview("etienne", "shop")
    source = builder.read_file("etienne", "shop", "app.py")["text"]
    builder.write_file("etienne", "shop", "app.py", source + COUNT_QUERY)
    with failing_build(builder.apps, "shop", monkeypatch) as failed:
        for _ in range(2):
            with pytest.raises(BuilderError, match="the sandbox is away"):
                builder.try_call("etienne", "shop", "movement_count", role="staff")
        # Not the draft's fault: each follow asks the sandbox again.
        assert len(failed) == 2
    assert builder.try_call("etienne", "shop", "movement_count", role="staff")["ok"]


def test_start_preview_starts_again(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    builder.try_call("etienne", "shop", "record_sale", SALE, role="staff")

    builder.start_preview("etienne", "shop")
    fresh = builder.try_call("etienne", "shop", "stock", role="staff")

    assert stock_quantity(fresh, "TEA-SEN") == 11


@needs_browser
def test_try_view_records_a_sale_in_the_preview(shop, builder: Builder) -> None:
    before = shop.runtime.query_json(STAFF, "stock", {})
    head = builder.apps.repo("etienne", "shop").head()
    builder.start_preview("etienne", "shop")

    screen = builder.try_view("etienne", "shop", "till", STEPS, role="staff")

    assert screen.png[:8] == b"\x89PNG\r\n\x1a\n"
    tools = [call["tool"] for call in screen.calls]
    assert tools == ["till", "till", "record_receipt", "till"]  # laid out again around the sale
    assert screen.error is None
    after = shop.runtime.query_json(STAFF, "stock", {})
    assert after == before
    assert builder.apps.repo("etienne", "shop").head() == head


@needs_browser
def test_try_view_says_what_a_bad_step_found(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")

    screen = builder.try_view("etienne", "shop", "till", [{"click": "Sell"}], role="staff")

    assert screen.error is not None
    assert screen.error.startswith("Step 1")
    assert "record receipt" in screen.error


def test_try_view_checks_its_width(shop, builder: Builder) -> None:
    with pytest.raises(BuilderError, match="320 to 1280"):
        builder.try_view("etienne", "shop", "till", STEPS, role="staff", width=100)


@needs_browser
def test_try_view_over_mcp(builder: Builder) -> None:
    draft_example(builder, "shop")

    async def main() -> None:
        async with BuilderClient(me_server(builder.apps)) as client:
            await client.call_tool("start_preview", {"app": "shop"})
            # The steps sent as their JSON text, as models often send a list, are taken.
            result = await client.call_tool(
                "try_view",
                {"app": "shop", "view": "till", "steps": json.dumps(STEPS), "role": "staff"},
            )
        assert len(result.content) == 2
        assert result.content[0].type == "text"
        assert "record_receipt" in result.content[0].text
        assert "kept in the preview" in result.content[0].text
        assert result.content[1].type == "image"

    anyio.run(main)


GRID_VIEW = """

@shop.view(roles=["owner", "staff"])
def shelf_grid(ctx: QueryContext) -> Screen:
    \"\"\"Thirty shelves in a grid.\"\"\"
    grid = {
        "columns": [
            {"field": "shelf", "header": "Shelf"},
            {"field": "units", "header": "Units", "format": "integer", "total": "sum"},
        ],
        "rows": [{"shelf": f"Shelf {n}", "units": n} for n in range(30)],
    }
    return Screen("<div data-grid='{{ grid | tojson }}'></div>", grid=grid)
"""


@needs_browser
def test_try_view_sorts_filters_and_pages_a_grid_in_the_browser(shop, builder: Builder) -> None:
    source = builder.read_file("etienne", "shop", "app.py")["text"]
    builder.write_file("etienne", "shop", "app.py", source + GRID_VIEW)
    builder.start_preview("etienne", "shop")
    steps = [{"click": "Units"}, {"click": "Units"}, {"click": "Filters"}, {"click": "Next"}]

    screen = builder.try_view("etienne", "shop", "shelf_grid", steps, role="staff", width=390)

    assert screen.error is None
    # The grid's clicks call nothing: the screen was only laid out.
    assert {call["tool"] for call in screen.calls} == {"shelf_grid"}
    assert screen.png[:8] == b"\x89PNG\r\n\x1a\n"


def scripted(*calls: dict) -> list[dict]:
    """The calls a script's answer lists, done unless they say their error."""
    return [{"error": None, "result": None, **call} for call in calls]


def answering(monkeypatch, builder: Builder, calls: list[dict], sent: list | None = None) -> None:
    """Make the sandbox answer a script with these calls, and keep the data it was sent."""

    def script(self, login, app, version, role, files, view, steps, width=768, theme="light"):
        if sent is not None:
            sent.append(dict(files))
        return Scripted(png=b"png", calls=calls, error=None)

    monkeypatch.setattr(type(builder.apps.executor.sandbox("etienne")), "script", script)


def test_try_view_makes_its_actions_again_on_the_preview(
    shop, builder: Builder, monkeypatch
) -> None:
    builder.start_preview("etienne", "shop")
    session = builder.previews.find("etienne", "shop")
    assert session is not None
    sent: list[dict] = []
    calls = scripted(
        {"tool": "stock", "arguments": {}},
        {"tool": "record_sale", "arguments": SALE},
        {"tool": "record_sale", "arguments": {**SALE, "receipt": "R-0901"}, "error": "Refused"},
        {"tool": "record_sale", "arguments": {**SALE, "receipt": "R-0902"}},
    )
    answering(monkeypatch, builder, calls, sent)

    tried = builder.try_view("etienne", "shop", "till", [], role="staff")

    assert sent[0]["movements.csv"] == example_data()["movements.csv"]
    assert tried.calls == calls
    # The query changes nothing, and the sandbox refused the third call: neither is made again.
    assert [(one["call"], one["tool"], one["ok"]) for one in tried.kept] == [
        (2, "record_sale", True),
        (4, "record_sale", True),
    ]
    assert "movements.csv" in tried.kept[0]["text"]
    changes = session.runtime.history(STAFF)
    assert [(one.action, one.caller, one.role) for one in changes[:2]] == [
        ("record_sale", "preview", "staff"),
        ("record_sale", "preview", "staff"),
    ]
    after = builder.try_call("etienne", "shop", "stock", role="staff")
    assert stock_quantity(after, "TEA-SEN") == 7

    # A click on the preview while the script ran is kept too: the calls run on the data now.
    calls = scripted({"tool": "record_sale", "arguments": {**SALE, "receipt": "R-0903"}})

    def script(self, login, app, version, role, files, view, steps, width=768, theme="light"):
        session.call("staff", "record_sale", {**SALE, "receipt": "R-0904"})
        return Scripted(png=b"png", calls=calls, error=None)

    monkeypatch.setattr(type(builder.apps.executor.sandbox("etienne")), "script", script)
    raced = builder.try_view("etienne", "shop", "till", [], role="staff")
    assert [one["ok"] for one in raced.kept] == [True]
    after = builder.try_call("etienne", "shop", "stock", role="staff")
    assert stock_quantity(after, "TEA-SEN") == 3


def test_try_view_says_when_its_clicks_were_not_kept(shop, builder: Builder, monkeypatch) -> None:
    builder.start_preview("etienne", "shop")
    # The sandbox did it, but on the preview it breaks a rule.
    answering(
        monkeypatch,
        builder,
        scripted({"tool": "record_sale", "arguments": {**SALE, "quantity": 99}}),
    )

    tried = builder.try_view("etienne", "shop", "till", [], role="staff")

    assert tried.png == b"png"
    assert [one["ok"] for one in tried.kept] == [False]
    assert tried.kept[0]["text"].startswith("Refused by: Stock never goes below zero")
    after = builder.try_call("etienne", "shop", "stock", role="staff")
    assert stock_quantity(after, "TEA-SEN") == 11


def test_try_view_says_it_made_no_built_in_write_again(shop, builder: Builder, monkeypatch) -> None:
    builder.start_preview("etienne", "shop")
    session = builder.previews.find("etienne", "shop")
    assert session is not None
    head = session.runtime.store.head()
    # The commit and the transaction are the sandbox copy's: the preview has neither.
    calls = scripted(
        {"tool": "app_history", "arguments": {}},
        {"tool": "app_revert", "arguments": {"commit": "0123456789abcdef"}},
        {"tool": "app_begin_transaction", "arguments": {}},
        {"tool": "app_commit_transaction", "arguments": {"app_transaction": "t-0"}, "error": "x"},
        {"tool": "record_sale", "arguments": SALE},
    )
    answering(monkeypatch, builder, calls)

    tried = builder.try_view("etienne", "shop", "till", [], role="staff")

    # The query changes nothing, and the sandbox refused the commit: neither is listed.
    assert [(one["call"], one["tool"], one["made"]) for one in tried.kept] == [
        (2, "app_revert", False),
        (3, "app_begin_transaction", False),
        (5, "record_sale", True),
    ]
    assert tried.kept[0]["text"] == tried.kept[1]["text"] == NOT_MADE_AGAIN
    # Only the action was made again; no transaction was begun on the preview.
    changes = session.runtime.history(STAFF)
    assert [one.action for one in changes if one.commit != head][:1] == ["record_sale"]
    assert session.runtime.store.head() == changes[0].commit
    assert session.runtime.store.tips("refs/app/transactions/") == {}
    assert "app_revert: not made again: It wasn't made again" in _try_view_text(
        "till", "staff", 768, "light", [], tried
    )


def test_try_view_says_which_clicks_were_kept_when_the_preview_ends(
    shop, builder: Builder, monkeypatch
) -> None:
    builder.start_preview("etienne", "shop")
    session = builder.previews.find("etienne", "shop")
    assert session is not None
    calls = scripted(
        {"tool": "record_sale", "arguments": SALE},
        {"tool": "record_sale", "arguments": {**SALE, "receipt": "R-0901"}},
        {"tool": "record_sale", "arguments": {**SALE, "receipt": "R-0902"}},
    )
    answering(monkeypatch, builder, calls)
    call = session.call
    made: list[str] = []

    def stopping(role, name, arguments, **options):
        # The preview ends between the first replayed write and the second.
        if made:
            builder.stop_preview("etienne", "shop")
        made.append(arguments["receipt"])
        return call(role, name, arguments, **options)

    monkeypatch.setattr(session, "call", stopping)

    tried = builder.try_view("etienne", "shop", "till", [], role="staff")

    assert tried.png == b"png" and tried.calls == calls
    assert [(one["call"], one["ok"]) for one in tried.kept] == [(1, True), (2, False)]
    assert tried.kept[1]["text"] == f"It wasn't made: {NO_PREVIEW}"
    # The third call isn't made.
    assert made == ["R-0900", "R-0901"]


def test_try_view_makes_only_the_draft_s_actions_again(shop, builder: Builder, monkeypatch) -> None:
    builder.start_preview("etienne", "shop")
    session = builder.previews.find("etienne", "shop")
    assert session is not None
    before = session.data()
    calls = scripted(
        {"tool": "wipe_everything", "arguments": {}},
        {"tool": "till", "arguments": {}},
        {"tool": "app_history", "arguments": {}},
        {"tool": "app_begin_transaction", "arguments": {}},
    )
    answering(monkeypatch, builder, calls)

    tried = builder.try_view("etienne", "shop", "till", [], role="owner")

    # Nothing is made again: a built-in tool that writes is listed as not made.
    assert [(one["tool"], one["made"]) for one in tried.kept] == [("app_begin_transaction", False)]
    assert session.data() == before


def test_try_view_makes_its_actions_again_as_its_role(shop, builder: Builder, monkeypatch) -> None:
    builder.start_preview("etienne", "shop")
    answering(
        monkeypatch,
        builder,
        scripted({"tool": "set_price", "arguments": {"sku": "TEA-SEN", "price": "9.00"}}),
    )

    tried = builder.try_view("etienne", "shop", "till", [], role="staff")

    assert [one["ok"] for one in tried.kept] == [False]
    assert "set_price" in tried.kept[0]["text"]


def test_try_call_shows_refusals_as_the_app_would(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")

    result = builder.try_call(
        "etienne",
        "shop",
        "record_sale",
        {"sku": "POT-GLS", "quantity": 99, "receipt": "R-0900"},
        role="staff",
    )

    assert result["ok"] is False
    assert result["text"].startswith("Refused by: Stock never goes below zero")


def test_try_call_reaches_no_view_but_a_shown_view_lays_out_again(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    session = builder.previews.find("etienne", "shop")
    assert session is not None

    with pytest.raises(BuilderError, match="till is a view, not an action or a query"):
        builder.try_call("etienne", "shop", "till", role="staff")
    # A shown view lays itself out again through preview_call, after each click.
    result = builder.preview_call("etienne", session.id, session.build.app_py, "till", {}, "staff")
    assert not result.is_error and result.structured_content["view"] == "till"


def test_try_call_follows_roles(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")

    result = builder.try_call(
        "etienne", "shop", "set_price", {"sku": "TEA-SEN", "price": "9.00"}, role="staff"
    )

    assert result["ok"] is False
    assert "set_price" in result["text"]


def test_sessions_end(shop, builder: Builder) -> None:
    build = builder.workshop.build("etienne", "shop")
    data, _ = builder.trying.trial_data(build, "etienne", "shop")
    app_py = builder.drafts.folder("etienne", "shop") / "app.py"
    previews = Previews(idle_seconds=0)
    first = previews.start("etienne", "shop", build, data, app_py, "a")
    folder = first._folder.name

    assert previews.find("etienne", "shop") is None
    assert not Path(folder).exists()
    previews.close()

    limited = Previews(limit=1)
    one = limited.start("etienne", "shop", build, data, app_py, "a")
    one_folder = one._folder.name
    limited.start("etienne", "other", build, data, app_py, "a")
    assert not Path(one_folder).exists()
    assert limited.find("etienne", "shop") is None
    again = limited.start("etienne", "other", build, data, app_py, "a")
    assert limited.get(again.id) is again
    limited.close()


def test_a_preview_ends_at_publish_and_delete(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.start_preview("etienne", "tasks")

    first = builder.publish_app("etienne", "tasks", "First version")
    assert first["published"] is True
    assert builder.previews.find("etienne", "tasks") is None
    # The answer says so, and the person's open page can.
    assert first["next"].endswith(PREVIEW_ENDED)
    assert builder.previews.ended_by_publish("etienne", "tasks")

    # With no preview running, the answer says nothing of one, and the page still says why.
    again = builder.publish_app("etienne", "tasks", "Second version")
    assert again["published"] is True
    assert PREVIEW_ENDED not in again["next"]
    assert builder.previews.ended_by_publish("etienne", "tasks")

    builder.start_preview("etienne", "tasks")
    assert not builder.previews.ended_by_publish("etienne", "tasks")
    builder.delete_app("etienne", "tasks", "tasks")
    assert builder.previews.find("etienne", "tasks") is None
    assert not builder.previews.ended_by_publish("etienne", "tasks")


def test_try_call_on_a_draft_never_published(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "data/items.csv", "name,note\nMilk,one\n")
    builder.start_preview("etienne", "tasks")

    result = builder.try_call("etienne", "tasks", "items")

    assert result["ok"] is True
    assert "Milk" in json.dumps(result["result"])


def test_a_sandbox_error_is_said(shop, builder: Builder, monkeypatch) -> None:
    builder.start_preview("etienne", "shop")

    def script(self, *args, **kwargs):
        raise SandboxError("the sandbox answered a script without its files")

    client = builder.apps.executor.sandbox("etienne")
    monkeypatch.setattr(type(client), "script", script)

    with pytest.raises(BuilderError, match="without its files"):
        builder.try_view("etienne", "shop", "till", [], role="staff")


def test_a_new_version_runs_on_the_same_runtime(shop, builder: Builder, monkeypatch) -> None:
    builder.start_preview("etienne", "shop")
    session = builder.previews.find("etienne", "shop")
    assert session is not None
    runtime = session.runtime
    closed: list[str] = []
    monkeypatch.setattr(runtime, "close", lambda: closed.append("runtime"))
    source = builder.read_file("etienne", "shop", "app.py")["text"]

    with session.using("staff") as server:
        builder.write_file("etienne", "shop", "app.py", source + COUNT_QUERY)
        count = builder.try_call("etienne", "shop", "movement_count", role="staff")
        assert count["ok"] is True, count["text"]
        # A call that holds the old version's server still runs, on the new version.
        assert session.runtime is runtime and server.runtime is runtime
        assert server.call("stock", {}).is_error is False
    assert closed == []
    # The role's server is made again, with the new version's tools.
    with session.using("staff") as fresh:
        assert fresh is not server
        assert "movement_count" in {tool.name for tool in fresh.tools}

    folder = session._folder.name
    with session.using("staff"):
        builder.stop_preview("etienne", "shop")
        assert Path(folder).exists()
    assert not Path(folder).exists()
    assert closed == ["runtime"]


def test_start_preview_says_when_it_ran_the_migration(builder: Builder) -> None:
    published(builder)
    plain = builder.start_preview("etienne", "tasks")

    builder.write_file("etienne", "tasks", "migrate.py", MIGRATION)
    migrated = builder.start_preview("etienne", "tasks")

    assert "migration" not in plain
    assert migrated["migration"] == (
        "The draft's migrate.py ran on the preview's copy of the data. The migration "
        "rewrites: items.csv. The live data is unchanged: publish_app runs the migration on it."
    )
    assert "MILK" in json.dumps(builder.try_call("etienne", "tasks", "items")["result"])
    assert names(builder) == ["milk", "eggs"]


def test_a_preview_says_when_the_migration_changed(builder: Builder) -> None:
    published(builder)
    builder.start_preview("etienne", "tasks")

    builder.write_file(
        "etienne", "tasks", "migrate.py", 'def migrate() -> None:\n    """Nothing."""\n'
    )
    notice = builder.try_call("etienne", "tasks", "items")["notice"]

    assert "migrate.py changed" in notice
    assert "start_preview" in notice


def test_a_closed_preview_says_it_ended(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    session = builder.previews.find("etienne", "shop")
    assert session is not None
    with pytest.raises(NoSuchRole), session.using("boss"):
        pass
    with pytest.raises(NoSuchRole), session.using(None):
        pass

    builder.stop_preview("etienne", "shop")

    with pytest.raises(PreviewEnded):
        session.data()
    with pytest.raises(PreviewEnded):
        session.call("staff", "stock", {})
