"""The token tools on the person's endpoint: create_token, list_tokens and revoke_token, over
HTTP, for a sign-in and for a token for everywhere. A limited token reaches none of them."""

import json
import re
from datetime import datetime, timedelta
from typing import Any

import anyio
import httpx
import mcp_types as types
import pytest
from starlette.applications import Starlette

from cliquet_server.access_tools import NOT_EVERYWHERE, NOT_FOR_GOOD
from cliquet_server.app_auth import limited_elsewhere
from cliquet_server.builder.panel import token_panel_html, token_panel_uri
from cliquet_server.builder.server import APP_STARTING
from cliquet_server.db import TokenLimit

from .server_helpers import (
    APPS,
    NOW,
    app_client,
    app_status,
    app_token,
    blocked_build,
    failing_build,
    published_before_roles_were_kept,
    signed_in,
    text,
)
from .test_server_protocol_versions import DRAWS

ME = "/me/mcp"
SHOP = "/apps/etienne/shop/mcp"
STAFF = [{"app": "etienne/shop", "role": "staff"}]
SECRET = re.compile(r"cliquet_[A-Za-z0-9_-]{20,}")


def everywhere(
    server: Starlette, name: str = "Builder", days: int | None = 90, made: datetime = NOW
) -> tuple[str, str]:
    """A token for everywhere of etienne's, the app's owner, made at `made` for `days`: its id
    and its secret."""
    database = server.state.apps.database
    etienne = database.person_by_login("etienne")
    info, token = database.create_token(etienne.id, name, made, days=days)
    return info.public_id, token


def calls(
    address: str,
    token: str,
    *made: tuple[str, dict[str, Any]],
    mode: str = "auto",
    draws: bool = False,
) -> list[types.CallToolResult]:
    """Call the person's endpoint's tools in turn, with a bearer token, `dev` for a sign-in,
    from a client that draws MCP Apps or not."""
    extensions = [DRAWS] if draws else []

    async def main() -> list[types.CallToolResult]:
        async with app_client(address, ME, token, mode=mode, extensions=extensions) as client:
            # Listed first: the client checks each answer against its tool's schema, and a
            # token revoked by a call can't list them after it.
            await client.list_tools()
            return [await client.call_tool(name, arguments) for name, arguments in made]

    return anyio.run(main)


def at_the_app(address: str, token: str) -> int | set[str]:
    """The tools a token gets at the shop's own address, or the status that refuses it."""
    status = app_status(address, token)
    if status in (401, 403):
        return status

    async def names() -> set[str]:
        async with app_client(address, SHOP, token) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    return anyio.run(names)


def test_a_limited_token_makes_no_token(server: Starlette, address: str) -> None:
    token = app_token(server, "staff")
    asked = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "create_token", "arguments": {"name": "Worker", "apps": STAFF}},
    }
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}

    refused = httpx.post(f"{address}{ME}", json=asked, headers=headers)

    assert refused.status_code == 403
    staff = (TokenLimit("etienne", "shop", "staff"),)
    assert refused.json() == {"error": limited_elsewhere("http://127.0.0.1:8800", staff)}
    database = server.state.apps.database
    assert len(database.tokens(database.person_by_login("etienne").id)) == 1


def test_no_token_for_everywhere_is_made_here(server: Starlette, address: str) -> None:
    _, builder = everywhere(server)

    for token in (builder, "dev"):
        (refused,) = calls(address, token, ("create_token", {"name": "Wide", "apps": []}))
        assert refused.is_error is True
        assert text(refused) == NOT_EVERYWHERE
    database = server.state.apps.database
    assert [one.name for one in database.tokens(database.person_by_login("etienne").id)] == [
        "Builder"
    ]


def test_a_token_for_everywhere_gets_the_secret_and_its_revoke_takes_it_back(
    server: Starlette, address: str
) -> None:
    builder_id, builder = everywhere(server)

    made, listed = calls(
        address,
        builder,
        ("create_token", {"name": "Worker", "apps": [{"app": "shop", "role": "staff"}]}),
        ("list_tokens", {}),
    )

    assert not made.is_error, text(made)
    answer = made.structured_content
    secret = answer["token"]
    assert SECRET.findall(text(made)) == [secret]
    assert "This is the only time its secret is shown" in text(made)
    assert "Authorization: Bearer <secret>" in text(made)
    assert answer["name"] == "Worker" and answer["token_id"].startswith("tok_")
    assert answer["where"] == {
        "label": "Works only for etienne/shop as staff",
        "addresses": [{"app": "etienne/shop", "address": f"http://127.0.0.1:8800{SHOP}"}],
    }
    # The new token works at the app's address, as staff.
    tools = at_the_app(address, secret)
    assert isinstance(tools, set) and "record_sale" in tools and "set_price" not in tools
    # The list names its maker, marks the token that calls, and holds no secret.
    rows = {row["name"]: row for row in listed.structured_content["tokens"]}
    assert rows["Worker"]["made_by"] == "Builder" and rows["Worker"]["current"] is False
    assert rows["Builder"]["made_by"] is None and rows["Builder"]["current"] is True
    assert rows["Builder"]["token_id"] == builder_id
    assert rows["Worker"]["where"] == answer["where"]
    assert rows["Worker"]["last_used_at"] is None and rows["Builder"]["last_used_at"]
    assert "cliquet_" not in text(listed)

    # With no session to close: the token that would close it is gone.
    revoke = ("revoke_token", {"token_id": builder_id})
    (revoked,) = calls(address, builder, revoke, mode="2026-07-28")

    assert not revoked.is_error, text(revoked)
    assert revoked.structured_content == {
        "token_id": builder_id,
        "name": "Builder",
        "went_with_it": ["Worker"],
    }
    assert "The tokens it made went with it: Worker." in text(revoked)
    assert "your next call with it is refused" in text(revoked)
    # The token it made stops at once, and so does the one that called.
    assert at_the_app(address, secret) == 401
    gone = httpx.post(f"{address}{ME}", json={}, headers={"Authorization": f"Bearer {builder}"})
    assert gone.status_code == 401


def test_a_token_made_by_a_token_ends_with_it_at_the_latest(
    server: Starlette, address: str
) -> None:
    _, month = everywhere(server, "Month", days=30)
    # Made 25 days before the server's clock: 5 days left.
    _, ending = everywhere(server, "Ending", days=30, made=NOW - timedelta(days=25))

    ends, week, long, never, odd = calls(
        address,
        month,
        ("create_token", {"name": "Ends", "apps": STAFF}),
        ("create_token", {"name": "Week", "apps": STAFF, "days": 7}),
        ("create_token", {"name": "Long", "apps": STAFF, "days": 90}),
        ("create_token", {"name": "Never", "apps": STAFF, "days": 0}),
        ("create_token", {"name": "Odd", "apps": STAFF, "days": 45}),
    )
    late, listed = calls(
        address, ending, ("create_token", {"name": "Late", "apps": STAFF}), ("list_tokens", {})
    )

    assert not ends.is_error, text(ends)
    assert ends.structured_content["expires_at"] == "2026-12-11T12:00:00+00:00"
    assert "until 11 Dec 2026, 12:00 UTC" in text(ends)
    assert week.structured_content["expires_at"] == "2026-11-18T12:00:00+00:00"
    assert long.is_error and text(long) == (
        "The token you call with expires on 11 Dec 2026, 12:00 UTC, and a token it makes never "
        "outlives it: choose fewer days, or leave days out to end with it."
    )
    # A token made here always expires.
    assert never.is_error and text(never) == NOT_FOR_GOOD
    # Any other lifetime is answered with the ones it may have.
    assert odd.is_error and text(odd) == (
        "A token made here works for 7, 30, 90 or 365 days: choose one in days."
    )
    assert late.is_error and text(late) == (
        "The token you call with expires on 16 Nov 2026, 12:00 UTC, sooner than the shortest "
        "lifetime of 7 days: make a new token at http://127.0.0.1:8800/tokens, and call with it."
    )
    rows = {row["name"]: row for row in listed.structured_content["tokens"]}
    assert set(rows) == {"Month", "Ending", "Ends", "Week"}
    assert rows["Ends"]["expires_at"] == rows["Month"]["expires_at"]
    assert rows["Ending"]["expires_at"] == "2026-11-16T12:00:00+00:00"
    assert not any(row["expired"] for row in rows.values())


def test_list_tokens_says_which_expired_and_which_never_do(server: Starlette, address: str) -> None:
    everywhere(server, "Forever", days=None)
    everywhere(server, "Old", days=7, made=NOW - timedelta(days=8))

    (listed,) = calls(address, "dev", ("list_tokens", {}))

    rows = {row["name"]: row for row in listed.structured_content["tokens"]}
    assert rows["Forever"]["expires_at"] is None and rows["Forever"]["expired"] is False
    assert rows["Old"]["expires_at"] == "2026-11-10T12:00:00+00:00"
    assert rows["Old"]["expired"] is True
    assert "`expires_at` null one that never expires" in text(listed)


def test_a_sign_in_gets_a_link_to_a_filled_in_form(server: Starlette, address: str) -> None:
    server.state.apps.import_folder(APPS / "stock", "etienne", "cafe")
    wanted = [{"app": "shop", "role": "staff"}, {"app": "etienne/cafe", "role": "owner"}]

    asked, week = calls(
        address,
        "dev",
        ("create_token", {"name": "Till import", "apps": wanted}),
        ("create_token", {"name": "Week", "apps": wanted, "days": 7}),
    )

    assert not asked.is_error, text(asked)
    link = asked.structured_content["link"]
    assert link == (
        "http://127.0.0.1:8800/tokens?name=Till+import"
        "&limit=etienne/shop/staff&limit=etienne/cafe/owner&days=90"
    )
    assert link in text(asked) and "its secret never reaches you" in text(asked)
    assert "as owner, for 90 days." in text(asked)
    assert week.structured_content["link"].endswith("&days=7")
    assert asked.structured_content["where"]["label"] == (
        "Works only for etienne/shop as staff and etienne/cafe as owner"
    )
    database = server.state.apps.database
    assert database.tokens(database.person_by_login("etienne").id) == []
    # The person opens it, signed in: the form is filled in.
    with signed_in(server) as client:
        page = client.get(link.removeprefix("http://127.0.0.1:8800")).text
    assert 'value="Till import">' in page
    assert '<option value="etienne/shop/staff" selected>' in page
    assert '<option value="etienne/cafe/owner" selected>' in page
    assert '<option value="90" selected>90 days</option>' in page


def test_create_token_says_what_is_wrong_with_its_apps(server: Starlette, address: str) -> None:
    database = server.state.apps.database
    etienne = database.person_by_login("etienne")
    database.create_token(etienne.id, "Taken", NOW)

    def refused(name: str, apps: list[dict[str, Any]]) -> str:
        (answer,) = calls(address, "dev", ("create_token", {"name": name, "apps": apps}))
        assert answer.is_error is True
        return text(answer)

    assert (
        refused("W", [{"app": "shop"}])
        == "Name your role in etienne/shop: you hold owner and staff."
    )
    assert refused("W", [{"app": "nope"}]) == (
        "There is no app nope for you. Call list_apps to see the apps you may use."
    )
    assert refused("W", [{"app": "shop", "role": "boss"}]) == (
        "You don't hold the role boss in etienne/shop. You hold owner and staff."
    )
    assert refused("Taken", STAFF) == "You already have a token named 'Taken': choose another name."
    assert refused("", STAFF).startswith("A token's name is one line")
    assert "one role in each app" in refused("W", [*STAFF, {"app": "shop", "role": "owner"}])
    assert [one.name for one in database.tokens(etienne.id)] == ["Taken"]


def test_create_token_takes_its_apps_as_json_text(server: Starlette, address: str) -> None:
    _, token = everywhere(server)

    # As models often send a list.
    (made,) = calls(address, token, ("create_token", {"name": "Till", "apps": json.dumps(STAFF)}))

    assert not made.is_error, text(made)
    assert made.structured_content is not None
    assert made.structured_content["where"]["label"] == "Works only for etienne/shop as staff"


def test_create_token_reads_the_roles_of_the_apps_it_names_only(
    server: Starlette, address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    apps = server.state.apps
    # Two apps of etienne's published before their roles were kept: their starts read them.
    apps.import_folder(APPS / "stock", "etienne", "cafe")
    for name in ("shop", "cafe"):
        published_before_roles_were_kept(apps.database, "etienne", name)
        apps.forget("etienne", name)
    _, token = everywhere(server)

    with failing_build(apps, "cafe", monkeypatch) as failed:
        (made,) = calls(address, token, ("create_token", {"name": "Till", "apps": STAFF}))
        assert not made.is_error, text(made)
        # The cafe isn't named, so it isn't started.
        assert failed == []
        (broken,) = calls(
            address, token, ("create_token", {"name": "Cafe", "apps": [{"app": "cafe"}]})
        )
    assert text(broken) == "etienne/cafe can't be started: the sandbox is away"

    published_before_roles_were_kept(apps.database, "etienne", "shop")
    apps.forget("etienne", "shop")
    apps.start_wait = 0
    with blocked_build(apps, "shop", monkeypatch):
        (starting,) = calls(address, token, ("create_token", {"name": "Late", "apps": STAFF}))
    # The owner's own app is only starting: it is there, and the call can be made again.
    assert starting.is_error and text(starting) == APP_STARTING
    assert [
        one.name for one in apps.database.tokens(apps.database.person_by_login("etienne").id)
    ] == [
        "Till",
        "Builder",
    ]


def test_revoke_token_takes_an_id_of_the_person_s(server: Starlette, address: str) -> None:
    builder_id, builder = everywhere(server)

    pasted, unknown = calls(
        address,
        "dev",
        ("revoke_token", {"token_id": builder}),
        ("revoke_token", {"token_id": "tok_0000000000000000"}),
    )

    # A secret pasted by mistake is refused, and never repeated.
    assert pasted.is_error is True
    assert builder not in text(pasted) and builder[8:20] not in text(pasted)
    assert "pass its tok_ id, from list_tokens" in text(pasted)
    assert unknown.is_error is True
    assert text(unknown) == (
        "The person has no token with the id 'tok_0000000000000000': call list_tokens for their "
        "tokens' ids."
    )
    database = server.state.apps.database
    assert [one.public_id for one in database.tokens(database.person_by_login("etienne").id)] == [
        builder_id
    ]


def test_a_sign_in_lists_its_tokens_and_revokes_one(server: Starlette, address: str) -> None:
    (empty,) = calls(address, "dev", ("list_tokens", {}))
    assert text(empty) == "The person has no API token. create_token makes one."
    builder_id, _ = everywhere(server)

    listed, revoked = calls(
        address, "dev", ("list_tokens", {}), ("revoke_token", {"token_id": builder_id})
    )

    (row,) = listed.structured_content["tokens"]
    assert row["current"] is False and row["last_used_at"] is None
    assert text(revoked) == "Revoked Builder: it works nowhere any more."


def test_a_sign_in_that_draws_gets_the_panel_and_no_secret(server: Starlette, address: str) -> None:
    async def main() -> tuple[Any, ...]:
        async with app_client(address, ME, "dev", extensions=[DRAWS]) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            panel = await client.read_resource(token_panel_uri())
            asked = await client.call_tool("create_token", {"name": "Worker", "apps": STAFF})
            return tools, panel, asked

    tools, panel, asked = anyio.run(main)

    assert tools["create_token"].meta["ui"]["resourceUri"] == token_panel_uri()
    assert tools["issue_token"].meta["ui"]["visibility"] == ["app"]
    (page,) = panel.contents
    assert page.text == token_panel_html()
    # Like the upload panel, it loads nothing from the network.
    assert "https://" not in page.text and "http://" not in page.text.replace("http://www.w3", "")
    assert not asked.is_error, text(asked)
    assert asked.structured_content == {
        "name": "Worker",
        "apps": STAFF,
        "days": 90,
        "where": {
            "label": "Works only for etienne/shop as staff",
            "addresses": [{"app": "etienne/shop", "address": f"http://127.0.0.1:8800{SHOP}"}],
        },
        "link": "http://127.0.0.1:8800/tokens?name=Worker&limit=etienne/shop/staff&days=90",
    }
    assert text(asked).startswith("The person now sees a panel in the conversation")
    assert "you never do" in text(asked)
    # Nothing is made until the person clicks Create, and no secret is anywhere.
    assert "cliquet_" not in asked.model_dump_json() and not SECRET.search(token_panel_html())
    database = server.state.apps.database
    assert database.tokens(database.person_by_login("etienne").id) == []


def test_the_panel_s_tool_makes_the_token_for_a_sign_in_only(
    server: Starlette, address: str
) -> None:
    _, builder = everywhere(server)
    asked = ("issue_token", {"name": "Worker", "apps": STAFF})

    (made,) = calls(address, "dev", asked, draws=True)
    (blind,) = calls(address, "dev", ("issue_token", {"name": "Other", "apps": STAFF}))
    (with_a_token,) = calls(address, builder, asked, draws=True)

    assert not made.is_error, text(made)
    answer = made.structured_content
    assert answer["name"] == "Worker" and answer["where"]["label"].endswith("as staff")
    # The secret is the panel's alone: the text a host may give its model holds none.
    assert SECRET.fullmatch(answer["token"]) and not SECRET.search(text(made))
    tools = at_the_app(address, answer["token"])
    assert isinstance(tools, set) and "record_sale" in tools
    database = server.state.apps.database
    (worker, _) = database.tokens(database.person_by_login("etienne").id)
    assert worker.name == "Worker" and worker.made_by is None
    assert worker.public_id == answer["token_id"]
    assert answer["expires_at"] == "2027-02-09T12:00:00+00:00"
    # Only a host that draws the panel, for a sign-in.
    assert blind.is_error is True
    assert text(blind) == (
        "issue_token is for the panel create_token shows. Give the person the link from "
        "create_token instead."
    )
    assert with_a_token.is_error is True
    assert "issue_token is for a person who signed in" in text(with_a_token)
    assert len(database.tokens(database.person_by_login("etienne").id)) == 2
