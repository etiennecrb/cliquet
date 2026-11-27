import logging
import re
import subprocess
import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
import mcp_types as types
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from cliquet.context import Caller
from cliquet.runtime import Committed
from cliquet.tools import NoSuchRole
from cliquet_server import endpoints
from cliquet_server.apps import Apps
from cliquet_server.db import TokenLimit
from cliquet_server.endpoints import (
    NoRole,
    NotStarted,
    NotYours,
    Registry,
    ShareWaiting,
    caller_role,
    why_not_started,
)
from cliquet_server.executor import BuildProblems
from cliquet_server.me import DESCRIBE

from .server_helpers import (
    APPS,
    ERROR_ID,
    NOW,
    a_shop_member,
    act_as,
    app_client,
    app_token,
    blocked_build,
    call,
    failing_build,
    in_session,
    member,
    open_session,
    publish_failing,
    rpc_refusal,
    session_gone,
    signed_in,
    template_code,
    texts,
    tool_names,
)

ISSUER = "https://example.eu.auth0.com/"


def run(fn):
    return anyio.run(fn)


def text(answer) -> str:
    (content,) = answer.content
    assert isinstance(content, types.TextContent)
    return content.text


def git(folder: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=folder, capture_output=True, text=True, check=True
    ).stdout


def test_caller_role() -> None:
    with_roles = {"roles": ["owner", "staff"]}
    assert caller_role(with_roles, "staff") == "staff"
    with pytest.raises(NoSuchRole) as wrong:
        caller_role(with_roles, "boss")
    assert str(wrong.value) == "the app has no role boss. Its roles are: owner, staff"
    assert wrong.value.sentence == "The app has no role boss. Its roles are: owner, staff."
    without = {"roles": []}
    assert caller_role(without, "-") is None
    with pytest.raises(NoSuchRole, match="use -"):
        caller_role(without, "owner")


def test_tools_follow_the_role_in_the_token(server: Starlette, address: str) -> None:
    async def names(role: str) -> set[str]:
        async with app_client(address, "/apps/etienne/shop/mcp", app_token(server, role)) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    staff = run(lambda: names("staff"))
    owner = run(lambda: names("owner"))
    assert "record_sale" in staff
    assert "set_price" not in staff
    assert "set_price" in owner


def test_a_call_commits_as_the_person(server: Starlette, address: str) -> None:
    async def scenario() -> None:
        async with app_client(
            address, "/apps/etienne/shop/mcp", app_token(server, "staff")
        ) as client:
            done = await client.call_tool(
                "record_sale", {"sku": "TEA-SEN", "quantity": 2, "receipt": "R-0200"}
            )
            assert not done.is_error

    run(scenario)
    folder = server.state.apps.repo("etienne", "shop").folder
    assert git(folder, "log", "-1", "--format=%an %s").strip() == "etienne record_sale"
    assert "App-Role: staff" in git(folder, "log", "-1", "--format=%B")


def test_a_change_names_its_token_to_its_person_and_the_owner_only(
    server: Starlette, address: str
) -> None:
    database = server.state.apps.database
    sam = a_shop_member(server, "staff")
    limit = TokenLimit("etienne", "shop", "staff")
    mine, routine = database.create_token(sam.id, "Stock routine", NOW, [limit])
    pat = database.person_for(ISSUER, "auth0|pat", "pat@example.com", "pat", NOW)
    member(database, pat, "etienne", "shop", "staff")
    path = "/apps/etienne/shop/mcp"

    async def history(token: str) -> list[dict]:
        async with app_client(address, path, token) as client:
            answer = await client.call_tool("app_history", {"limit": 5})
            return answer.structured_content["changes"]

    async def scenario() -> None:
        async with app_client(address, path, routine) as client:
            done = await client.call_tool(
                "record_sale", {"sku": "TEA-SEN", "quantity": 1, "receipt": "R-0210"}
            )
            assert not done.is_error, text(done)
            # The token's person reads its name.
            answer = await client.call_tool("app_history", {"limit": 1})
            (own,) = answer.structured_content["changes"]
            assert (own["caller"], own["role"], own["agent"]) == ("sam", "staff", "Stock routine")
        # The owner reads it too, and another member doesn't see the change at all.
        (latest, *_) = await history(app_token(server, "owner"))
        assert (latest["caller"], latest["agent"]) == ("sam", "Stock routine")
        async with app_client(address, path, app_token(server, "staff", person=pat)) as client:
            done = await client.call_tool(
                "record_sale", {"sku": "TEA-SEN", "quantity": 1, "receipt": "R-0211"}
            )
            assert not done.is_error, text(done)
            answer = await client.call_tool("app_history", {"limit": 5})
            theirs = answer.structured_content["changes"]
        assert theirs and {change["caller"] for change in theirs} == {"pat"}
        assert "Stock routine" not in str(theirs)

    run(scenario)
    folder = server.state.apps.repo("etienne", "shop").folder
    assert "App-Agent: Stock routine" in git(folder, "log", "-1", "--skip=1", "--format=%B")
    # A revoked token keeps, in the history, the name it had when it wrote.
    assert database.revoke_token(sam.id, mine.id) == []
    runtime = server.state.apps.live("etienne", "shop").runtime
    pats, sams = runtime.history(Caller("etienne", "owner"), limit=2)
    assert (sams.caller, sams.agent) == ("sam", "Stock routine")
    assert pats.agent is not None and pats.agent != "Stock routine"
    with signed_in(server) as client:
        page = client.get("/manage/etienne/shop/history").text
        change = client.get(f"/manage/etienne/shop/history/{sams.commit}").text
    assert "with the token Stock routine" in page
    assert ", with the token Stock routine" in change
    # The builder is the owner's: its description of the app names the token too.
    latest = server.state.builder.read_app_overview("etienne", "shop")["latest_changes"]
    assert [change["agent"] for change in latest[:2]] == [pats.agent, "Stock routine"]


def test_a_transaction_through_an_app_endpoint(server: Starlette, address: str) -> None:
    folder = server.state.apps.repo("etienne", "shop").folder
    before = git(folder, "rev-parse", "HEAD").strip()

    def level(answer) -> int:
        rows = answer.structured_content["result"]
        return next(row["quantity"] for row in rows if row["sku"] == "TEA-SEN")

    async def scenario() -> None:
        async with app_client(
            address, "/apps/etienne/shop/mcp", app_token(server, "staff")
        ) as client:
            begun = await client.call_tool("app_begin_transaction")
            assert not begun.is_error
            t = begun.structured_content["transaction"]
            start = level(await client.call_tool("stock"))
            sale = {"sku": "TEA-SEN", "quantity": 2, "receipt": "R-0300", "app_transaction": t}
            done = await client.call_tool("record_sale", sale)
            assert not done.is_error and text(done).startswith(f"In transaction {t}: ")
            inside = await client.call_tool("stock", {"app_transaction": t})
            # The app's result, then Cliquet's own words with the version of the data.
            result_text, cliquet = texts(inside)
            assert result_text.startswith(f"In transaction {t}: ")
            # A transaction's version is its branch's commit, not the live one.
            assert cliquet.startswith("app_data_version: ") and before not in cliquet
            assert level(inside) == start - 2
            assert level(await client.call_tool("stock")) == start
            assert git(folder, "rev-parse", "HEAD").strip() == before
            runtime = server.state.apps.live("etienne", "shop").runtime
            screen = await anyio.to_thread.run_sync(
                lambda: runtime.view(Caller("etienne", "staff"), "till", transaction=t)
            )
            # The screen's calls carry the transaction, so its clicks stay in it.
            assert screen["transaction"] == t
            committed = await client.call_tool("app_commit_transaction", {"app_transaction": t})
            assert not committed.is_error, committed.content
            assert level(await client.call_tool("stock")) == start - 2

    run(scenario)
    # Kept by a commit of its own, whose second parent is the sale made in the transaction.
    assert (
        git(folder, "log", "-1", "--format=%an %s")
        .strip()
        .startswith("etienne Commit transaction t-")
    )
    assert "App-Transaction: " in git(folder, "log", "-1", "--format=%B", "HEAD^2")
    assert git(folder, "log", "-1", "--format=%an %s", "HEAD^2").strip() == "etienne record_sale"
    assert git(folder, "status", "--porcelain").strip() == ""


def test_the_instructions_are_the_app_s(server: Starlette, address: str) -> None:
    async def instructions() -> str | None:
        async with app_client(
            address, "/apps/etienne/shop/mcp", app_token(server, "staff")
        ) as client:
            # The usual test client finds the version with server/discover, and needs no
            # session: test_server_protocol_versions covers a client that opens one.
            assert client.protocol_version == "2026-07-28"
            return client.instructions

    text = run(instructions)
    assert text is not None
    assert "Tea shop stock" in text
    assert "You act as etienne, with the role staff" in text


def test_an_app_without_roles_is_served_to_its_token(
    server: Starlette, address: str, tmp_path: Path
) -> None:
    apps: Apps = server.state.apps
    apps.database.create_app("etienne", "list", NOW)
    apps.publish("etienne", "list", template_code(tmp_path / "list"), note="test", data={})

    async def names() -> set[str]:
        token = app_token(server, "-", app="list")
        async with app_client(address, "/apps/etienne/list/mcp", token) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    assert "add_item" in run(names)
    # A role the app doesn't have can't be given to a token, even by the owner: the tokens
    # page offers only the app's roles. Made by hand, it is refused.
    wrong = httpx.post(
        f"{address}/apps/etienne/list/mcp",
        headers={"Authorization": f"Bearer {app_token(server, 'owner', app='list')}"},
    )
    assert wrong.status_code == 404


def test_what_is_not_there_is_a_404(server: Starlette, address: str) -> None:
    wrong = httpx.post(
        f"{address}/apps/etienne/shop/mcp",
        headers={"Authorization": f"Bearer {app_token(server, 'boss')}"},
    )
    assert "owner, staff" in rpc_refusal(wrong, 404)


def test_a_new_version_drops_the_endpoints(server: Starlette, address: str) -> None:
    async def names() -> set[str]:
        async with app_client(
            address, "/apps/etienne/shop/mcp", app_token(server, "staff")
        ) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    async def scenario() -> None:
        async with app_client(
            address, "/apps/etienne/shop/mcp", app_token(server, "staff")
        ) as client:
            assert "record_sale" in {t.name for t in (await client.list_tools()).tools}
            repo = server.state.apps.repo("etienne", "shop")
            code = repo.code()
            code["app.py"] += b"\n# another\n"
            await anyio.to_thread.run_sync(
                lambda: server.state.apps.publish("etienne", "shop", code, note="More")
            )
        assert "record_sale" in await names()

    run(scenario)
    managers = server.state.registry._managers
    assert sum(1 for key in managers if key[:2] == ("etienne", "shop")) == 1


SHOP = "/apps/etienne/shop/mcp"
ENDED = "This session ended before your request ran. Connect again."


def test_an_unknown_session_is_told_it_ended(server: Starlette, address: str) -> None:
    gone = in_session(address, SHOP, app_token(server, "staff"), "made-up")
    assert session_gone(gone) == ENDED


def test_a_publish_is_remembered_for_a_while(
    server: Starlette, address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only one session a publish ended is remembered: the one opened last.
    monkeypatch.setattr(endpoints, "_ENDED_MOST", 1)
    registry: Registry = server.state.registry
    now = [1000.0]
    registry.monotonic = lambda: now[0]
    token = app_token(server, "staff")
    first, last = open_session(address, SHOP, token), open_session(address, SHOP, token)
    publish_failing(server)
    assert "published again" in session_gone(in_session(address, SHOP, token, last))
    assert session_gone(in_session(address, SHOP, token, first)) == ENDED
    # Another caller never learns of the publish.
    owner = app_token(server, "owner")
    assert session_gone(in_session(address, SHOP, owner, last)) == ENDED
    now[0] += endpoints._ENDED_FOR - 1
    assert "published again" in session_gone(in_session(address, SHOP, token, last))
    now[0] += 1
    assert session_gone(in_session(address, SHOP, token, last)) == ENDED


def fake_clock(registry: Registry) -> list[float]:
    """The registry's clock, set to a time a test moves on by hand."""
    now = [1000.0]
    registry.monotonic = lambda: now[0]
    return now


def settled(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """A wait, with a deadline, until no request at an app's address holds its manager: a
    client reads an answer before the request that sent it has ended on the server."""
    changed = threading.Condition()
    left = registry.left

    def leaving(served: Any) -> None:
        left(served)
        with changed:
            changed.notify_all()

    def wait() -> None:
        with changed:
            idle = changed.wait_for(
                lambda: not any(one.busy for one in list(registry._managers.values())), 10
            )
        assert idle, "a request didn't end in time"

    monkeypatch.setattr(registry, "left", leaving)
    return wait


def test_an_idle_session_is_told_it_ended(
    server: Starlette, address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry: Registry = server.state.registry
    now = fake_clock(registry)
    wait = settled(registry, monkeypatch)
    token = app_token(server, "staff")
    # Opened by hand, with no GET stream, which would keep the entry as it keeps the session.
    session = open_session(address, SHOP, token)
    assert in_session(address, SHOP, token, session).status_code == 200
    wait()
    now[0] += endpoints.IDLE_FOR
    # Another caller's lookup drops the entry: no publish is remembered for it.
    in_session(address, SHOP, app_token(server, "owner"), "made-up")
    assert not any(key[2:4] == ("staff", "etienne") for key in registry._servers)
    assert session_gone(in_session(address, SHOP, token, session)) == ENDED
    assert in_session(address, SHOP, token, open_session(address, SHOP, token)).status_code == 200


def test_a_client_without_a_session_outlives_an_idle_drop(
    server: Starlette, address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry: Registry = server.state.registry
    now = fake_clock(registry)
    wait = settled(registry, monkeypatch)

    async def scenario() -> None:
        async with app_client(address, SHOP, app_token(server, "staff"), mode="2026-07-28") as c:
            assert "record_sale" in await tool_names(c)
            await anyio.to_thread.run_sync(wait)
            served = dict(registry._managers)
            now[0] += endpoints.IDLE_FOR
            assert "record_sale" in await tool_names(c)
            # Its lookup dropped the idle entry, and made it again.
            assert served.keys() == registry._managers.keys()
            assert all(registry._managers[key] is not served[key] for key in served)

    run(scenario)


def ended_on(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> anyio.Event:
    """An event set once the task of a session manager the registry makes has ended."""
    ended = anyio.Event()
    serve = registry._serve

    async def serving(server, *, task_status) -> None:
        try:
            await serve(server, task_status=task_status)
        finally:
            ended.set()

    monkeypatch.setattr(registry, "_serve", serving)
    return ended


SHOP_OWNER = ("etienne", "shop", "owner", "etienne")
SHOP_STAFF = ("etienne", "shop", "staff", "sam")


def test_an_unused_manager_is_dropped_after_a_while(
    shop, apps: Apps, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry(apps)
    now = fake_clock(registry)
    ended = ended_on(registry, monkeypatch)
    key = (*SHOP_OWNER, None)

    async def main() -> None:
        async with registry.running():
            served = await registry.manager(*SHOP_OWNER)
            registry.left(served)
            now[0] += endpoints.IDLE_FOR - 1
            await registry.app_server(*SHOP_STAFF)
            assert registry._managers[key] is served
            now[0] += 1
            await registry.app_server(*SHOP_STAFF)
            assert key not in registry._servers and key not in registry._managers
            assert key not in registry._used
            with anyio.fail_after(10):
                await ended.wait()

    anyio.run(main)


def test_a_request_in_flight_keeps_its_manager(
    shop, apps: Apps, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry(apps)
    now = fake_clock(registry)
    key = (*SHOP_OWNER, None)

    async def main() -> None:
        async with registry.running():
            served = await registry.manager(*SHOP_OWNER)
            now[0] += 2 * endpoints.IDLE_FOR
            await registry.app_server(*SHOP_STAFF)
            assert registry._managers[key] is served
            # Its end is a use: the time starts again from there.
            registry.left(served)
            now[0] += endpoints.IDLE_FOR - 1
            await registry.app_server(*SHOP_STAFF)
            assert registry._managers[key] is served
            now[0] += 1
            await registry.app_server(*SHOP_STAFF)
            assert key not in registry._managers

    anyio.run(main)


def test_an_unused_server_is_made_again(shop, apps: Apps) -> None:
    registry = Registry(apps)
    now = fake_clock(registry)

    async def main() -> None:
        async with registry.running():
            # As the app's pages and /me make it: a server with no manager.
            server = await registry.app_server(*SHOP_OWNER)
            now[0] += endpoints.IDLE_FOR
            await registry.app_server(*SHOP_STAFF)
            assert (*SHOP_OWNER, None) not in registry._servers
            assert await registry.app_server(*SHOP_OWNER) is not server

    anyio.run(main)


def test_two_roles_are_served_at_once(server: Starlette, address: str) -> None:
    async def scenario() -> tuple[set[str], set[str]]:
        async with (
            app_client(address, "/apps/etienne/shop/mcp", app_token(server, "staff")) as staff,
            app_client(address, "/apps/etienne/shop/mcp", app_token(server, "owner")) as owner,
        ):
            staff_tools = {t.name for t in (await staff.list_tools()).tools}
            owner_tools = {t.name for t in (await owner.list_tools()).tools}
            return staff_tools, owner_tools

    staff, owner = run(scenario)
    assert "record_sale" in staff and "set_price" not in staff
    assert "set_price" in owner


def test_an_endpoint_answers_behind_a_proxy(server: Starlette) -> None:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    with TestClient(server) as client:
        response = client.post(
            "/apps/etienne/shop/mcp",
            json=body,
            headers={
                "Host": "cliquet-internal.example",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {app_token(server, 'staff')}",
            },
        )
    assert response.status_code not in (400, 421)


# What a caller sees


def test_every_live_runtime_has_an_owner(server: Starlette, tmp_path: Path) -> None:
    apps: Apps = server.state.apps
    # Imported.
    runtime = apps.live("etienne", "shop").runtime
    assert runtime.owner == "etienne"
    # Published again, and made again after it was forgotten.
    code = apps.repo("etienne", "shop").code()
    code["app.py"] += b"\n# another\n"
    assert apps.publish("etienne", "shop", code, note="More").runtime.owner == "etienne"
    apps.forget("etienne", "shop")
    runtime = apps.live("etienne", "shop").runtime
    assert runtime.owner == "etienne"
    # Created and published.
    apps.database.create_app("etienne", "list", NOW)
    listed = apps.publish("etienne", "list", template_code(tmp_path / "list"), note="t", data={})
    assert listed.runtime.owner == "etienne"
    # A transaction runs on the live runtime: a member's is theirs, and the owner sees it.
    member = Caller("sam", "owner")
    assert runtime.is_owner(Caller("etienne", "staff")) and not runtime.is_owner(member)
    t = runtime.begin(member)
    sale = {"sku": "TEA-SEN", "quantity": 1, "receipt": "R-0500"}
    done = runtime.call(member, "record_sale", sale, transaction=t)
    assert isinstance(done, Committed) and done.commit is not None
    committed = runtime.commit_transaction(member, t)
    assert isinstance(committed, Committed) and committed.commit is not None
    assert [e.commit for e in runtime.history(member)] == committed.commits == [done.commit]
    assert runtime.history(Caller("etienne", "staff"))[0].commit == done.commit
    assert len(runtime.history(Caller("etienne", "staff"))) > 1


def test_a_member_sees_their_own_history(
    server: Starlette, address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sam = a_shop_member(server)
    folder = server.state.apps.repo("etienne", "shop").folder

    async def sale(client, receipt: str) -> str:
        done = await client.call_tool(
            "record_sale", {"sku": "TEA-SEN", "quantity": 1, "receipt": receipt}
        )
        assert not done.is_error, done.content
        return done.structured_content["commit"]

    async def scenario() -> None:
        async with (
            app_client(address, "/apps/etienne/shop/mcp", app_token(server, "staff")) as owner,
            app_client(
                address, "/apps/etienne/shop/mcp", app_token(server, "owner", person=sam)
            ) as member,
        ):
            theirs = await sale(owner, "R-0601")
            later = await sale(owner, "R-0602")
            # The latest, so that the owner can undo it.
            mine = await sale(member, "R-0603")

            own = (await member.call_tool("app_history")).structured_content["changes"]
            assert [change["commit"] for change in own] == [mine]
            every = (await owner.call_tool("app_history")).structured_content["changes"]
            assert [change["commit"] for change in every][:3] == [mine, later, theirs]
            assert {change["caller"] for change in every} >= {"etienne", "sam"}

            # Every change here is made at NOW, the first, the publish, too.
            earlier = (NOW - timedelta(days=1)).isoformat()
            own_since = await member.call_tool("app_history", {"since": earlier})
            assert [c["commit"] for c in own_since.structured_content["changes"]] == [mine]
            # A change made in the second of `since` counts as made since.
            same = await member.call_tool("app_history", {"since": NOW.isoformat()})
            assert [c["commit"] for c in same.structured_content["changes"]] == [mine]
            later_on = (NOW + timedelta(seconds=1)).isoformat()
            none_since = await member.call_tool("app_history", {"since": later_on})
            assert none_since.structured_content["changes"] == []
            # What changed, only through a query: here, nothing since the second after NOW.
            asked = {"query": "stock", "since": later_on}
            stock = (await member.call_tool("app_changes_since", asked)).structured_content
            assert (stock["added"], stock["removed"]) == ([], [])
            # A member is told the commits compared, as the owner is.
            assert (stock["then"], stock["now"]) == (mine, mine)
            owned = (await owner.call_tool("app_changes_since", asked)).structured_content
            assert (owned["then"], owned["now"]) == (mine, mine)
            # Before the first commit there is no data: every product is added.
            empty = await member.call_tool("app_changes_since", {**asked, "since": earlier})
            before = empty.structured_content
            assert before["removed"] == []
            assert {"sku": "TEA-SEN", "name": "Sencha green tea 100 g", "quantity": 8} in (
                before["added"]
            )
            # A version of the data is a commit's full id: anything else git reads is refused.
            since_theirs = await member.call_tool("app_changes_since", {**asked, "since": theirs})
            assert not since_theirs.is_error, since_theirs.content
            for commit in (theirs[:7], "HEAD"):
                refused = await member.call_tool("app_changes_since", {**asked, "since": commit})
                assert refused.is_error and text(refused).startswith("since must be a time")
            undone = await member.call_tool("app_revert", {"commit": theirs})
            assert undone.is_error
            assert text(undone) == "Denied: You can only undo your own changes."
            reverted = await owner.call_tool("app_revert", {"commit": mine})
            assert not reverted.is_error, reverted.content

    run(scenario)
    # Stored as Cliquet always stored an undo; its trailer makes it read as app_revert.
    assert git(folder, "log", "-1", "--format=%an %s").strip() == "etienne revert"


def test_a_member_gets_no_error_text(
    server: Starlette, address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sam = a_shop_member(server)
    publish_failing(server)

    async def answer(token: str) -> str:
        async with app_client(address, "/apps/etienne/shop/mcp", token) as client:
            failed = await client.call_tool("secret")
            assert failed.is_error
            return text(failed)

    member = run(lambda: answer(app_token(server, "owner", person=sam)))
    assert "Sencha" not in member and "4.20" not in member
    assert re.fullmatch(
        rf"The app failed with a KeyError\. Nothing was changed\. Error {ERROR_ID} has the "
        r"details: the app's owner can read it in the app's console\.",
        member,
    )
    owner = run(lambda: answer(app_token(server, "staff")))
    assert "KeyError: 'Sencha costs 4.20'" in owner


def test_a_failure_to_start_is_logged_once_for_a_while(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(endpoints, "_now", lambda: clock[0])
    monkeypatch.setattr(endpoints, "_logged_ids", {})
    monkeypatch.setattr(endpoints, "_LOGGED_MOST", 2)
    try:
        raise BuildProblems(["app.py: KeyError: 'Sencha costs 4.20'"])
    except BuildProblems as caught:
        error = caught

    first = why_not_started(error, "sam", "etienne", "shop")
    assert "Sencha" not in first
    # The app's own failure: a warning with its traceback, never unexpected.
    assert "Sencha costs 4.20" in caplog.text and "Traceback" in caplog.text
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    caplog.clear()
    assert why_not_started(error, "sam", "etienne", "shop") == first
    assert caplog.text == ""
    assert "Sencha costs 4.20" in why_not_started(error, "etienne", "etienne", "shop")
    # After ten minutes, a new id and a new line in the log.
    clock[0] += 601
    later = why_not_started(error, "sam", "etienne", "shop")
    assert later != first and "Sencha" in caplog.text
    # A few failures are kept, and their texts only as hashes.
    for name in ("a", "b", "c"):
        why_not_started(error, "sam", "etienne", name)
    assert len(endpoints._logged_ids) == 2
    assert all("Sencha" not in "".join(key) for key in endpoints._logged_ids)


def test_an_app_that_starts_blocks_no_other(shop, apps: Apps, monkeypatch) -> None:
    store = apps.import_folder(APPS / "stock", "etienne", "store")
    apps.forget("etienne", "shop")
    registry = Registry(apps)

    async def main() -> None:
        async with registry.running():
            managers: list[object] = []

            async def first() -> None:
                managers.append(await registry.manager("etienne", "shop", "owner", "etienne"))

            with blocked_build(apps, "shop", monkeypatch) as blocked:
                async with anyio.create_task_group() as group:
                    group.start_soon(first)
                    group.start_soon(first)
                    assert await anyio.to_thread.run_sync(blocked.building.wait, 30)
                    # Another app answers while this one's build is held.
                    with anyio.fail_after(10):
                        other = await registry.app_server("etienne", "store", "owner", "etienne")
                    assert other.runtime is store.runtime
                    blocked.release()
            # Two first calls of one app: one build, one manager.
            assert len(managers) == 2 and managers[0] is managers[1]
            assert blocked.builds == 1

    anyio.run(main)


def test_an_app_changed_while_it_starts_is_served_again(shop, apps: Apps, monkeypatch) -> None:
    apps.forget("etienne", "shop")
    registry = Registry(apps)

    async def main() -> None:
        async with registry.running():
            servers: list[object] = []

            async def first() -> None:
                servers.append(await registry.app_server("etienne", "shop", "owner", "etienne"))

            with blocked_build(apps, "shop", monkeypatch) as blocked:
                async with anyio.create_task_group() as group:
                    group.start_soon(first)
                    assert await anyio.to_thread.run_sync(blocked.building.wait, 30)
                    # The app is forgotten while it starts, as a delete or a publish does: the
                    # server made from this start is never served again.
                    forgetting = threading.Thread(target=apps.forget, args=("etienne", "shop"))
                    forgetting.start()
                    blocked.release()
                await anyio.to_thread.run_sync(forgetting.join, 60)
            again = await registry.app_server("etienne", "shop", "owner", "etienne")
            assert len(servers) == 1 and again is not servers[0]

    anyio.run(main)


def test_a_manager_is_made_only_for_the_server_served_now(shop, apps: Apps, monkeypatch) -> None:
    registry = Registry(apps)
    key = ("etienne", "shop", "owner", "etienne")

    async def main() -> None:
        async with registry.running():
            old = await registry.app_server(*key)
            real = registry.app_server
            calls: list[int] = []

            async def stale_once(*args: Any) -> object:
                calls.append(1)
                if len(calls) > 1:
                    return await real(*args)
                # Once this call has found the old server, the app is published again and
                # another call sweeps its entries, before this one takes the lock.
                apps.forget("etienne", "shop")
                async with registry._lock:
                    registry._sweep()
                return old

            monkeypatch.setattr(registry, "app_server", stale_once)
            served = await registry.manager(*key)

            # A sign-in: no token name.
            assert registry._managers[(*key, None)] is served
            assert registry._servers[(*key, None)] is not old

    anyio.run(main)


def test_who_may_open_an_app_is_decided_in_one_place(
    shop, apps: Apps, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = apps.database
    registry = Registry(apps)
    owner = database.person_by_login("etienne")
    assert owner is not None

    def person(login: str):
        return database.person_for("cliquet://dev", login, f"{login}@example.com", login, NOW)

    staff = member(database, person("till"), "etienne", "shop", "staff")
    stranger = person("kim")
    invited = person("pat")
    database.share("etienne", "shop", "pat@example.com", "staff", NOW)

    async def opened(who, role: str | None, *, only: bool = False):
        return await registry.open_for(who, "etienne", "shop", role, only=only)

    async def main() -> None:
        # The owner, with a role or the first one, and the member with theirs.
        mine = await opened(owner, "staff")
        assert (mine.address, mine.role, mine.held) == ("staff", "staff", ["owner", "staff"])
        assert mine.server.caller == Caller("etienne", "staff")
        assert (await opened(owner, None)).address == "owner"
        assert (await opened(staff, None, only=True)).address == "staff"
        # Not yours answers as missing, and a share that waits says only its roles.
        with pytest.raises(NotYours) as missing:
            await opened(stranger, "staff")
        assert not isinstance(missing.value, ShareWaiting)
        with pytest.raises(ShareWaiting) as waiting:
            await opened(invited, "staff")
        assert (waiting.value.login, waiting.value.name, waiting.value.roles) == (
            "etienne",
            "shop",
            ["staff"],
        )
        # A role the person doesn't hold says the ones they do, and only the owner reads the
        # roles of an app that has no such role.
        with pytest.raises(NoRole) as not_held:
            await opened(staff, "owner")
        assert (not_held.value.role, not_held.value.held) == ("owner", ["staff"])
        with pytest.raises(NoRole) as several:
            await opened(owner, None, only=True)
        assert (several.value.role, several.value.held) == (None, ["owner", "staff"])
        with pytest.raises(endpoints.UnknownRole) as hidden:
            await opened(staff, "boss")
        assert hidden.value.listed is None and str(hidden.value) == "there is no role boss"
        with pytest.raises(endpoints.UnknownRole) as told:
            await opened(owner, "boss")
        assert str(told.value) == "the app has no role boss. Its roles are: owner, staff"
        # An app that can't be started says why, as the person may read it.
        apps.forget("etienne", "shop")
        with failing_build(apps, "shop", monkeypatch), pytest.raises(NotStarted) as failed:
            await opened(staff, "staff")
        assert re.fullmatch(rf"Error {ERROR_ID} has the details: .*", failed.value.why)

    anyio.run(main)


def test_a_role_an_app_without_roles_dropped_is_refused_alike(
    server: Starlette, address: str, tmp_path: Path
) -> None:
    apps: Apps = server.state.apps
    apps.database.create_app("etienne", "list", NOW)
    apps.publish("etienne", "list", template_code(tmp_path / "list"), note="test", data={})
    sam = member(apps.database, a_shop_member(server, None), "etienne", "list", "-")
    token = app_token(server, "-", app="list", person=sam)
    # Then sam's only role is one the app doesn't have, as after a publish that dropped it.
    apps.database.remove_member("etienne", "list", sam.id)
    member(apps.database, sam, "etienne", "list", "staff")
    answer = httpx.post(
        f"{address}/apps/etienne/list/mcp", headers={"Authorization": f"Bearer {token}"}
    )
    # The app has the role `-`, so it is refused as one sam no longer holds, as on its page.
    assert "which you no longer hold in etienne/list" in rpc_refusal(answer, 403)
    with TestClient(server) as client:
        act_as(server, client, sam)
        page = client.get("/apps/etienne/list/-")
        assert page.status_code == 403 and "That role isn&#39;t yours" in page.text
        called = client.post(
            "/apps/etienne/list/-/rpc", json={}, headers={"Origin": "http://testserver"}
        )
        assert called.status_code == 403, called.text
        # Opened with no role named, it is refused the same way.
        opened = client.get("/apps/etienne/list")
        assert opened.status_code == 403 and "You don&#39;t have a role here." in opened.text
    refused = call(server, "sam", DESCRIBE, {"app": "etienne/list"})
    assert (
        refused.is_error
        and "You hold no role in etienne/list: ask its owner to share it again." in text(refused)
    )
