"""An app's code sees the id of the API token behind a call, through a real sandbox: so its
rules can tell one agent of a person from another. History and transactions stay the
person's."""

from typing import Any

import anyio
from starlette.applications import Starlette

from cliquet_server.db import TokenLimit
from cliquet_server.me import READ, WRITE

from .server_helpers import NOW, a_shop_member, app_client, signed_in

# Code added to the stock example: a query and an action that answer the token's id.
TOKEN_ID = b'''

@shop.query(roles=["owner", "staff"])
def token_id(ctx: QueryContext) -> str | None:
    """The id of the API token behind the call."""
    return ctx.caller.token_id


@shop.action(roles=["owner", "staff"])
def claim(ctx: ActionContext) -> str | None:
    """Change nothing, and answer the id of the API token behind the call."""
    return ctx.caller.token_id
'''
SHOP = {"app": "etienne/shop", "role": "staff"}
APP = "/apps/etienne/shop/mcp"


def _publish(server: Starlette) -> None:
    apps = server.state.apps
    code = apps.repo("etienne", "shop").code()
    code["app.py"] += TOKEN_ID
    apps.publish("etienne", "shop", code, note="Token ids")


def _result(answer: Any) -> Any:
    assert not answer.is_error, answer.content
    return answer.structured_content["result"]


async def _at_the_app(address: str, token: str) -> tuple[Any, Any]:
    """What the query and the action answer at the app's own address, with a limited token."""
    async with app_client(address, APP, token) as client:
        return _result(await client.call_tool("token_id")), _result(await client.call_tool("claim"))


async def _on_me(address: str, token: str, role: str = "staff") -> tuple[Any, Any]:
    """What the query and the action answer through the person's endpoint."""
    shop = {**SHOP, "role": role}
    async with app_client(address, "/me/mcp", token) as client:
        read = await client.call_tool(READ, {**shop, "tool": "token_id"})
        written = await client.call_tool(WRITE, {**shop, "tool": "claim"})
        return _result(read), _result(written)


def test_app_code_sees_the_token_behind_a_call(server: Starlette, address: str) -> None:
    _publish(server)
    database = server.state.apps.database
    sam = a_shop_member(server, "staff")
    limit = TokenLimit("etienne", "shop", "staff")
    limited, limited_token = database.create_token(sam.id, "Routine", NOW, [limit])
    everywhere, everywhere_token = database.create_token(sam.id, "Laptop", NOW)
    assert limited.public_id != everywhere.public_id

    seen = anyio.run(_at_the_app, address, limited_token)
    assert seen == (limited.public_id, limited.public_id)
    assert anyio.run(_on_me, address, everywhere_token) == (everywhere.public_id,) * 2
    # A sign-in, here the development login, has no token: the app sees None.
    assert anyio.run(_on_me, address, "dev", "owner") == (None, None)
    # Nor has the web.
    asked = {"method": "tools/call", "params": {"name": "token_id", "arguments": {}}}
    with signed_in(server) as client:
        answer = client.post("/apps/etienne/shop/owner/rpc", json=asked).json()
    assert answer["structuredContent"]["result"] is None


def test_a_token_made_again_is_a_new_id(server: Starlette, address: str) -> None:
    _publish(server)
    database = server.state.apps.database
    etienne = database.person_by_login("etienne")
    assert etienne is not None
    limits = [TokenLimit("etienne", "shop", "owner")]
    first, token = database.create_token(etienne.id, "Routine", NOW, limits)
    assert anyio.run(_at_the_app, address, token)[0] == first.public_id
    # Revoked and made again under the same name, it never gets the old one's id.
    assert database.revoke_token(etienne.id, first.id) == []
    again, token = database.create_token(etienne.id, "Routine", NOW, limits)
    assert again.public_id != first.public_id
    assert anyio.run(_at_the_app, address, token)[0] == again.public_id


def test_two_tokens_of_one_person_share_a_transaction(server: Starlette, address: str) -> None:
    database = server.state.apps.database
    sam = a_shop_member(server, "staff")
    first = database.create_token(sam.id, "Builder", NOW)[1]
    second = database.create_token(sam.id, "Worker", NOW)[1]

    async def scenario() -> None:
        async with (
            app_client(address, "/me/mcp", first) as one,
            app_client(address, "/me/mcp", second) as other,
        ):
            begun = await one.call_tool(WRITE, {**SHOP, "tool": "app_begin_transaction"})
            assert not begun.is_error, begun.content
            t = {"app_transaction": begun.structured_content["transaction"]}
            sale = {"sku": "TEA-SEN", "quantity": 1, "receipt": "R-0900", **t}
            sold = await other.call_tool(WRITE, {**SHOP, "tool": "record_sale", "arguments": sale})
            assert not sold.is_error, sold.content
            # The transaction is the person's: one token commits what another began.
            committed = await other.call_tool(
                WRITE, {**SHOP, "tool": "app_commit_transaction", "arguments": t}
            )
            assert not committed.is_error, committed.content
            assert len(committed.structured_content["commits"]) == 1

    anyio.run(scenario)
