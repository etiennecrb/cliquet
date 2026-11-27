"""Both kinds of MCP client on the app endpoints and the person's endpoint: one that opens a
session with `initialize`, as before 2026-07-28, and one that speaks only 2026-07-28, where each
request carries its version and the client's capabilities, with no session."""

import json
from typing import Any

import anyio
import httpx
import mcp_types as types
import pytest
from mcp.client.extension import advertise
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette

from cliquet import views
from cliquet_server.me import DESCRIBE, LIST, READ, SHOW, WRITE

from .server_helpers import (
    app_client,
    app_token,
    in_session,
    open_session,
    publish_failing,
    session_gone,
    stock_quantity,
    text,
    tool_names,
)

SHOP = "/apps/etienne/shop/mcp"
ME = "/me/mcp"
# What a client that draws MCP Apps says about itself: once, in `initialize`, before
# 2026-07-28, and on every request from then on.
DRAWS = advertise(views.EXTENSION, {"mimeTypes": [views.MIME_TYPE]})
# A client's mode and the version it settles on.
VERSIONS = [
    pytest.param("legacy", "2025-11-25", id="session"),
    pytest.param("2026-07-28", "2026-07-28", id="2026-07-28"),
]
SALE = {"sku": "TEA-SEN", "quantity": 2}
# What a session at an app's address is told once a publish ended it.
PUBLISHED = (
    "etienne/shop was published again, so this session ended before your request ran. "
    "Connect again to get the new version's tools."
)


def tea(answer: types.CallToolResult) -> int:
    """The stock of the sencha in an answer of the `stock` query."""
    return stock_quantity(answer.structured_content, "TEA-SEN")


def shows_screen(answer: types.CallToolResult) -> bool:
    """Whether a view's answer carries its screen, laid out, or only says what it shows."""
    assert not answer.is_error, text(answer)
    if answer.structured_content is None:
        assert "this client doesn't draw MCP Apps" in text(answer)
        return False
    assert text(answer).startswith("The person now sees the screen 'till'")
    assert answer.structured_content["view"] == "till"
    return True


@pytest.mark.parametrize(("mode", "version"), VERSIONS)
def test_an_app_endpoint_for_each_version(
    server: Starlette, address: str, mode: str, version: str
) -> None:
    token = app_token(server, "staff")

    async def scenario() -> None:
        async with app_client(address, SHOP, token, mode=mode, extensions=[DRAWS]) as client:
            assert client.protocol_version == version
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert {"record_sale", "stock", "till", "app_begin_transaction"} <= names
            start = tea(await client.call_tool("stock"))
            sold = await client.call_tool("record_sale", {**SALE, "receipt": f"R-{mode}"})
            assert not sold.is_error, text(sold)
            assert tea(await client.call_tool("stock")) == start - 2
            assert shows_screen(await client.call_tool("till"))

            begun = await client.call_tool("app_begin_transaction")
            t = begun.structured_content["transaction"]
            inside = {**SALE, "receipt": f"R-{mode}-t", "app_transaction": t}
            assert not (await client.call_tool("record_sale", inside)).is_error
            assert tea(await client.call_tool("stock")) == start - 2
            committed = await client.call_tool("app_commit_transaction", {"app_transaction": t})
            assert not committed.is_error, text(committed)
            assert tea(await client.call_tool("stock")) == start - 4

        # A client that doesn't say it draws MCP Apps gets the view's words, not its screen.
        async with app_client(address, SHOP, token, mode=mode) as client:
            assert not shows_screen(await client.call_tool("till"))

    anyio.run(scenario)


@pytest.mark.parametrize(("mode", "version"), VERSIONS)
def test_the_person_s_endpoint_for_each_version(
    server: Starlette, address: str, mode: str, version: str
) -> None:
    shop = {"app": "etienne/shop", "role": "owner"}

    async def scenario() -> None:
        async with app_client(address, ME, mode=mode, extensions=[DRAWS]) as client:
            assert client.protocol_version == version

            async def use(name: str, **arguments: Any) -> types.CallToolResult:
                answer = await client.call_tool(name, {**shop, **arguments})
                assert not answer.is_error, text(answer)
                return answer

            names = [tool.name for tool in (await client.list_tools()).tools]
            assert {LIST, DESCRIBE, READ, WRITE, SHOW} <= set(names)
            listed = await client.call_tool(LIST)
            assert "etienne/shop" in text(listed)
            assert "record_sale" in text(await use(DESCRIBE))
            start = tea(await use(READ, tool="stock"))
            await use(WRITE, tool="record_sale", arguments={**SALE, "receipt": f"R-{mode}"})
            assert tea(await use(READ, tool="stock")) == start - 2
            assert shows_screen(await use(SHOW, view="till"))

            begun = await use(WRITE, tool="app_begin_transaction")
            t = begun.structured_content["transaction"]
            inside = {**SALE, "receipt": f"R-{mode}-t", "app_transaction": t}
            await use(WRITE, tool="record_sale", arguments=inside)
            assert tea(await use(READ, tool="stock")) == start - 2
            await use(WRITE, tool="app_commit_transaction", arguments={"app_transaction": t})
            assert tea(await use(READ, tool="stock")) == start - 4

        async with app_client(address, ME, mode=mode) as client:
            plain = await client.call_tool(SHOW, {**shop, "view": "till"})
            assert not shows_screen(plain)

    anyio.run(scenario)


def ask(address: str, path: str, token: str, tool: str, arguments: dict, draws: bool) -> dict:
    """The result of one 2026-07-28 request for `tool`, made by hand: no session and no client
    info, only the version and the capabilities, which say whether the client draws."""
    capabilities = {"extensions": {views.EXTENSION: {"mimeTypes": [views.MIME_TYPE]}}}
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": capabilities if draws else {},
    }
    params = {"name": tool, "arguments": arguments, "_meta": meta}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/call",
        "Mcp-Name": tool,
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    response = httpx.post(f"{address}{path}", json=body, headers=headers, timeout=30)
    assert response.status_code == 200, response.text
    assert "mcp-session-id" not in response.headers
    if response.headers["content-type"].startswith("text/event-stream"):
        lines = response.text.splitlines()
        events = [json.loads(line[5:]) for line in lines if line.startswith("data:")]
        # Notifications may come first: the answer is the message with the request's id.
        return next(event["result"] for event in events if event.get("id") == 1)
    return response.json()["result"]


def test_each_request_says_whether_the_client_draws(server: Starlette, address: str) -> None:
    calls = [
        (SHOP, app_token(server, "staff"), "till", {}),
        (ME, "dev", SHOW, {"app": "etienne/shop", "role": "owner", "view": "till"}),
    ]
    for path, token, tool, arguments in calls:
        drawn = ask(address, path, token, tool, arguments, draws=True)
        assert drawn["structuredContent"]["view"] == "till", drawn
        # The same caller doesn't say it draws on its next request: no screen.
        plain = ask(address, path, token, tool, arguments, draws=False)
        assert "structuredContent" not in plain, plain
        assert "this client doesn't draw MCP Apps" in plain["content"][0]["text"]


def test_discover_says_what_initialize_said(server: Starlette, address: str) -> None:
    token = app_token(server, "staff")
    shop = "Stock for a small tea shop: products, deliveries and sales, kept by an agent."

    async def scenario() -> None:
        for path, bearer, name, description, words in (
            (SHOP, token, "shop", shop, f'<app_instructions app="shop">\n{shop}'),
            (ME, "dev", "cliquet", None, "Cliquet keeps the person's apps"),
        ):
            # The usual client asks `server/discover`, as a 2026-07-28 client does.
            async with app_client(address, path, bearer) as found:
                assert found.protocol_version == "2026-07-28"
                info, instructions = found.server_info, found.instructions
                capabilities = found.server_capabilities
            async with app_client(address, path, bearer, mode="legacy") as began:
                assert (info, instructions) == (began.server_info, began.instructions)
            # An app endpoint names the app and its description: the person's, Cliquet.
            assert info is not None and (info.name, info.description) == (name, description)
            assert instructions is not None and instructions.startswith(words)
            # It serves tools, and screens to a client that draws them.
            assert capabilities.tools is not None
            assert views.EXTENSION in (capabilities.extensions or {})

    anyio.run(scenario)


def test_a_publish_ends_a_session_and_says_why(server: Starlette, address: str) -> None:
    token = app_token(server, "staff")

    async def scenario() -> None:
        async with app_client(address, SHOP, token, mode="legacy") as client:
            assert "secret" not in await tool_names(client)
            opened = open_session(address, SHOP, token)
            await anyio.to_thread.run_sync(publish_failing, server)
            # The request reached no app: the client reads why its session ended.
            with pytest.raises(MCPError, match="published again") as raised:
                await client.list_tools()
            assert str(raised.value) == PUBLISHED
            assert session_gone(in_session(address, SHOP, token, opened)) == PUBLISHED
        async with app_client(address, SHOP, token, mode="legacy") as client:
            assert "secret" in await tool_names(client)

    anyio.run(scenario)


def test_a_client_without_a_session_lists_the_new_tools(server: Starlette, address: str) -> None:
    token = app_token(server, "staff")

    async def scenario() -> None:
        async with app_client(address, SHOP, token, mode="2026-07-28") as client:
            assert "secret" not in await tool_names(client)
            await anyio.to_thread.run_sync(publish_failing, server)
            assert "secret" in await tool_names(client)

    anyio.run(scenario)


def test_a_new_session_says_again_whether_it_draws(server: Starlette, address: str) -> None:
    token = app_token(server, "staff")

    async def scenario() -> None:
        async with app_client(address, SHOP, token, mode="legacy", extensions=[DRAWS]) as client:
            assert shows_screen(await client.call_tool("till"))
            await anyio.to_thread.run_sync(publish_failing, server)
            with pytest.raises(MCPError, match="published again"):
                await client.call_tool("till")
        # Each session after the publish is read again: one that draws, then one that doesn't.
        async with app_client(address, SHOP, token, mode="legacy", extensions=[DRAWS]) as client:
            assert shows_screen(await client.call_tool("till"))
        async with app_client(address, SHOP, token, mode="legacy") as client:
            assert not shows_screen(await client.call_tool("till"))

    anyio.run(scenario)
