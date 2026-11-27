import json
from typing import Any

import anyio
import pytest
from fastmcp import Client as BuilderClient

from cliquet import views
from cliquet.context import Caller
from cliquet.screens import text
from cliquet_server.builder import Builder, BuilderError
from cliquet_server.builder.previews import rewrite_calls
from cliquet_server.builder.trying import OUT_OF_DATE
from cliquet_server.engine import UnknownName
from cliquet_server.executor import SandboxAway, SandboxError, SandboxFault

from .server_helpers import STAFF, draft_example, me_server, stock_quantity

SCREEN: dict[str, Any] = {
    "screen": 1,
    "app": "shop",
    "view": "till",
    "arguments": {},
    "body": [["form", {"data-call": "record_receipt"}, ["button", {"class": "btn"}, "Record"]]],
    "tools": {
        "record_receipt": {"kind": "action", "input": {"type": "object"}},
        "till": {"kind": "view", "input": {"type": "object"}},
    },
    "accent": None,
    "transaction": None,
    "relay": None,
}
SENCHA = "Sencha green tea 100 g"


def preview_id(screen: dict[str, Any]) -> str:
    """The preview a shown screen's calls go to."""
    return screen["relay"]["fixed"]["preview"]


def click(
    builder: Builder,
    screen: dict[str, Any],
    tool: str,
    arguments: dict[str, Any] | None = None,
    login: str = "etienne",
) -> Any:
    """Call a tool from a shown screen as its renderer does, with what its relay fixes."""
    return builder.preview_call(login, tool=tool, arguments=arguments, **screen["relay"]["fixed"])


def test_rewrite_calls() -> None:
    original = json.loads(json.dumps(SCREEN))

    rewritten = rewrite_calls(SCREEN, "session-1", "a1", "staff")

    assert original == SCREEN
    # Every call, to an action or to lay a view out again, goes to preview_call, which names
    # the preview, the app.py that laid the screen out, the role and the app's tool.
    route = {"tool": "preview_call", "key": "tool"}
    assert rewritten["relay"] == {
        "routes": {"action": route, "view": route},
        "fixed": {"preview": "session-1", "app_py": "a1", "role": "staff"},
    }
    # The body isn't touched: the renderer reads the relay.
    assert rewritten["body"] == SCREEN["body"] and rewritten["tools"] == SCREEN["tools"]


def test_rewrite_calls_names_a_role_only_when_given() -> None:
    assert rewrite_calls(SCREEN, "s", "a1")["relay"]["fixed"] == {"preview": "s", "app_py": "a1"}


SALE = {"sku": "TEA-SEN", "quantity": 2, "receipt": "R-0900"}


def rows_of(data):
    if isinstance(data, dict) and "result" in data:
        data = data["result"]
    return data


def in_stock(screen: dict[str, Any]) -> dict[str, int]:
    """The units a shown shelves screen lists in its table, by product name."""
    rows: dict[str, int] = {}
    stack: list[Any] = list(screen["body"])
    while stack:
        node = stack.pop()
        if not isinstance(node, list):
            continue
        cells = [child for child in node[2:] if isinstance(child, list) and child[0] == "td"]
        if node[0] == "tr" and len(cells) == 2:
            rows[text([cells[0]])] = int(text([cells[1]]))
        stack += node[2:]
    return rows


def test_show_preview_returns_the_rewritten_view(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")

    screen, _ = builder.show_preview("etienne", "shop", "till", role="staff")

    assert screen["view"] == "till" and "record_receipt" in screen["tools"]
    routes = screen["relay"]["routes"]
    assert {route["tool"] for route in routes.values()} == {"preview_call"}
    assert screen["relay"]["fixed"]["role"] == "staff"
    assert preview_id(screen)


def test_show_preview_keeps_the_preview(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    builder.try_call("etienne", "shop", "record_sale", SALE, role="staff")

    shelves, _ = builder.show_preview("etienne", "shop", "shelves", role="staff")
    assert in_stock(shelves)[SENCHA] == 9

    screen, _ = builder.show_preview("etienne", "shop", "till", role="staff")
    sale = click(builder, screen, "record_sale", {**SALE, "receipt": "R-0901"})
    assert sale.is_error is False
    after = builder.try_call("etienne", "shop", "stock", role="owner")
    assert next(row["quantity"] for row in rows_of(after["result"]) if row["sku"] == "TEA-SEN") == 7
    live = shop.runtime.query_json(STAFF, "stock", {})
    assert next(row["quantity"] for row in live if row["sku"] == "TEA-SEN") == 11


@pytest.mark.parametrize(
    ("fault", "answer"),
    [
        # Cliquet's own words: the app isn't to blame, and the call can be made again.
        (
            SandboxAway("the sandbox can't be reached"),
            "Not done: the sandbox can't be reached. Nothing was changed. Try again in a minute.",
        ),
        # One that wouldn't pass if made again says no more.
        (
            SandboxFault("the sandbox refused the token"),
            "Not done: the sandbox refused the token. Nothing was changed.",
        ),
        (
            UnknownName("there is no action named stock"),
            "Not done: there is no action named stock. Nothing was changed.",
        ),
        # Any other fault of the agent may quote the app: it reads as the app's.
        (
            SandboxError("the sandbox is away"),
            "The app failed: SandboxError: the sandbox is away. Nothing was changed.",
        ),
    ],
)
def test_a_sandbox_that_cant_be_reached_is_an_error_answer(
    shop, builder: Builder, monkeypatch, fault: Exception, answer: str
) -> None:
    builder.start_preview("etienne", "shop")
    screen, _ = builder.show_preview("etienne", "shop", "till", role="staff")

    def run(self, *args, **kwargs):
        raise fault

    monkeypatch.setattr(type(builder.apps.executor.sandbox("etienne")), "run", run)

    tried = builder.try_call("etienne", "shop", "stock", role="staff")
    assert (tried["ok"], tried["text"], tried["result"]) == (False, answer, None)
    with pytest.raises(BuilderError) as shown:
        builder.show_preview("etienne", "shop", "till", role="staff")
    assert str(shown.value) == answer
    clicked = click(builder, screen, "record_sale", SALE)
    assert clicked.is_error is True
    assert [part.text for part in clicked.content] == [answer]


def test_preview_call_runs_in_the_session(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    screen, _ = builder.show_preview("etienne", "shop", "till", role="staff")

    sale = click(builder, screen, "record_sale", SALE)
    assert sale.is_error is False
    stock = click(builder, screen, "stock")
    data = rows_of(stock.structured_content)
    assert next(row["quantity"] for row in data if row["sku"] == "TEA-SEN") == 9
    live = shop.runtime.query_json(STAFF, "stock", {})
    assert next(row["quantity"] for row in live if row["sku"] == "TEA-SEN") == 11


def test_preview_call_lays_a_view_out_again(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    shelves, _ = builder.show_preview(
        "etienne", "shop", "shelves", role="staff", arguments={"search": "tea"}
    )
    assert shelves["arguments"] == {"search": "tea"} and len(in_stock(shelves)) == 3
    preview = preview_id(shelves)
    click(builder, shelves, "record_sale", SALE)

    # A form or a link to a view calls preview_call with the view's name and its arguments.
    found = click(builder, shelves, "shelves", {"search": "sencha"})

    assert found.is_error is False
    screen = found.structured_content
    assert screen["view"] == "shelves" and screen["arguments"] == {"search": "sencha"}
    # Laid out on the preview's data, and its calls go to the same preview again.
    assert in_stock(screen) == {SENCHA: 9}
    assert screen["relay"]["fixed"] == {
        "preview": preview,
        "app_py": shelves["relay"]["fixed"]["app_py"],
        "role": "staff",
    }
    assert screen["relay"]["routes"]["view"] == {"tool": "preview_call", "key": "tool"}
    wrong = click(builder, screen, "shelves", {"search": 3})
    assert wrong.is_error is True


def test_preview_call_passes_refusals_on(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    screen, _ = builder.show_preview("etienne", "shop", "till", role="staff")

    result = click(
        builder, screen, "record_sale", {"sku": "POT-GLS", "quantity": 99, "receipt": "R-0900"}
    )

    assert result.is_error is True
    assert result.content[0].text.startswith("Refused by:")


def test_a_preview_belongs_to_who_opened_it(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    screen, _ = builder.show_preview("etienne", "shop", "till", role="staff")
    ended = "This preview has ended. Show the view again with show_preview."

    with pytest.raises(BuilderError) as error:
        click(builder, screen, "stock", login="sam")
    assert str(error.value) == ended
    with pytest.raises(BuilderError, match="show_preview"):
        builder.preview_call("etienne", "unknown", screen["relay"]["fixed"]["app_py"], "stock")


def test_a_view_shown_before_a_new_preview_has_ended(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    first, _ = builder.show_preview("etienne", "shop", "till", role="staff")
    click(builder, first, "record_sale", SALE)

    builder.start_preview("etienne", "shop")
    second, _ = builder.show_preview("etienne", "shop", "till", role="staff")
    result = click(builder, second, "stock")

    data = rows_of(result.structured_content)
    assert next(row["quantity"] for row in data if row["sku"] == "TEA-SEN") == 11
    assert preview_id(second) != preview_id(first)
    with pytest.raises(BuilderError, match="has ended"):
        click(builder, first, "stock")
    builder.stop_preview("etienne", "shop")
    with pytest.raises(BuilderError, match="has ended"):
        click(builder, second, "stock")


def test_an_unknown_view(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")

    with pytest.raises(BuilderError):
        builder.show_preview("etienne", "shop", "nope", role="staff")


def test_the_tools_are_an_mcp_app(builder: Builder) -> None:
    async def main() -> None:
        async with BuilderClient(me_server(builder.apps)) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert tools["show_preview"].meta["ui"]["resourceUri"] == views.renderer_uri()
            assert tools["preview_call"].meta["ui"]["visibility"] == ["app"]
            contents = await client.read_resource(views.renderer_uri())
            assert contents[0].mime_type == views.MIME_TYPE
            assert contents[0].text == views.renderer_html()

    anyio.run(main)


def test_a_client_that_can_t_draw_gets_text(builder: Builder) -> None:
    draft_example(builder, "shop")

    async def main() -> None:
        async with BuilderClient(me_server(builder.apps)) as client:
            await client.call_tool("start_preview", {"app": "shop"})
            result = await client.call_tool(
                "show_preview", {"app": "shop", "view": "till", "role": "staff"}
            )
        assert len(result.content) == 1
        # Words, not a JSON list holding them.
        assert result.content[0].text.startswith("This assistant can't draw views.")
        assert "take_screenshots" in result.content[0].text
        assert result.structured_content is None

    anyio.run(main)


def test_a_shown_preview_is_not_live(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    screen, _ = builder.show_preview("etienne", "shop", "till", role="staff")
    click(builder, screen, "record_sale", SALE)

    live = shop.runtime.query_json(Caller("etienne", "owner"), "stock", {})

    assert next(row["quantity"] for row in live if row["sku"] == "TEA-SEN") == 11


# Every sale takes twice the units it names.
DOUBLE_SALE = (
    "    _sell(ctx, receipt, sku, quantity)\n",
    "    _sell(ctx, receipt, sku, quantity * 2)\n",
)


def sencha_left(builder: Builder) -> int:
    """How many units of sencha the preview holds."""
    return stock_quantity(builder.try_call("etienne", "shop", "stock", role="owner"), "TEA-SEN")


def test_a_click_after_an_edit_runs_the_draft_as_it_is_now(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    old, _ = builder.show_preview("etienne", "shop", "till", role="staff")
    source = builder.read_file("etienne", "shop", "app.py")["text"]
    assert DOUBLE_SALE[0] in source

    builder.write_file("etienne", "shop", "app.py", source.replace(*DOUBLE_SALE))

    # The screen was laid out by the old code: its action isn't made, and nothing changes.
    with pytest.raises(BuilderError) as error:
        click(builder, old, "record_sale", SALE)
    assert str(error.value) == OUT_OF_DATE
    assert sencha_left(builder) == 11
    # Laying a view out from it draws the draft as it is now, so the screen is shown again.
    again = click(builder, old, "till", {})
    assert again.is_error is False
    screen = again.structured_content
    assert screen["relay"]["fixed"]["app_py"] != old["relay"]["fixed"]["app_py"]
    sale = click(builder, screen, "record_sale", SALE)
    assert sale.is_error is False
    assert sencha_left(builder) == 7


def test_a_click_after_the_same_code_is_written_again_runs(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    screen, _ = builder.show_preview("etienne", "shop", "till", role="staff")
    source = builder.read_file("etienne", "shop", "app.py")["text"]

    builder.write_file("etienne", "shop", "app.py", source)
    sale = click(builder, screen, "record_sale", SALE)

    assert sale.is_error is False
    assert sencha_left(builder) == 9


def test_a_click_on_a_draft_that_doesn_t_build_runs_the_last_that_did(
    shop, builder: Builder
) -> None:
    builder.start_preview("etienne", "shop")
    screen, _ = builder.show_preview("etienne", "shop", "till", role="staff")

    builder.write_file("etienne", "shop", "app.py", "this isn't Python\n")
    sale = click(builder, screen, "record_sale", SALE)
    stock = click(builder, screen, "stock")

    # The person can't fix the draft: their clicks run what the screen was laid out by.
    assert sale.is_error is False
    assert stock_quantity({"result": stock.structured_content}, "TEA-SEN") == 9


def test_a_click_after_a_test_is_written_still_runs(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    screen, _ = builder.show_preview("etienne", "shop", "till", role="staff")

    builder.write_file(
        "etienne", "shop", "tests/test_more.py", "def test_more() -> None:\n    pass\n"
    )
    sale = click(builder, screen, "record_sale", SALE)

    # Only app.py lays a screen out: its tests and instructions don't make it out of date.
    assert sale.is_error is False
    assert sencha_left(builder) == 9


def test_a_click_as_a_role_the_draft_dropped_is_out_of_date(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    screen, _ = builder.show_preview("etienne", "shop", "till", role="staff")
    source = builder.read_file("etienne", "shop", "app.py")["text"]
    assert source.count('"staff"') > 1

    builder.write_file("etienne", "shop", "app.py", source.replace('"staff"', '"clerk"'))

    # Neither the action nor laying the view out again can run as a role that is gone.
    for tool, arguments in (("record_sale", SALE), ("till", {})):
        with pytest.raises(BuilderError) as error:
            click(builder, screen, tool, arguments)
        assert str(error.value) == OUT_OF_DATE


def test_a_click_through_the_tool_names_its_app_py(shop, builder: Builder) -> None:
    builder.start_preview("etienne", "shop")
    screen, _ = builder.show_preview("etienne", "shop", "till", role="staff")
    source = builder.read_file("etienne", "shop", "app.py")["text"]
    # What the renderer sends: what the relay fixes, the app's tool and its arguments.
    sent = {**screen["relay"]["fixed"], "tool": "record_sale", "arguments": SALE}

    async def main() -> None:
        async with BuilderClient(me_server(builder.apps, builder)) as client:
            sale = await client.call_tool("preview_call", sent)
            builder.write_file("etienne", "shop", "app.py", source.replace(*DOUBLE_SALE))
            stale = await client.call_tool("preview_call", sent, raise_on_error=False)
            builder.stop_preview("etienne", "shop")
            # A screen shown before servers named app.py: its preview ended long ago.
            unnamed = {key: value for key, value in sent.items() if key != "app_py"}
            ended = await client.call_tool("preview_call", unnamed, raise_on_error=False)
        assert sale.is_error is False
        assert stale.is_error is True
        assert stale.content[0].text == OUT_OF_DATE
        assert ended.is_error is True
        assert (
            ended.content[0].text
            == "This preview has ended. Show the view again with show_preview."
        )

    anyio.run(main)
