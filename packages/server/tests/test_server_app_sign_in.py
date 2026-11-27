import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path

import anyio
import httpx
import pytest
from mcp.shared.exceptions import MCPError

from cliquet_server.db import TokenLimit
from cliquet_server.executor import LocalExecutor

from .server_helpers import (
    APPS,
    AUTH0_ISSUER,
    NOW,
    app_client,
    member,
    oidc_server,
    rpc_refusal,
    running,
)


@pytest.fixture
def signed_server(tmp_path: Path, executor: LocalExecutor):
    app = oidc_server(tmp_path, executor)
    app.state.apps.import_folder(APPS / "stock", "etienne", "shop", email="etienne@example.com")
    database = app.state.apps.database
    sam = database.person_for(AUTH0_ISSUER, "auth0|sam", "sam@example.com", "sam", NOW)
    member(database, sam, "etienne", "shop", "staff")
    database.person_for(AUTH0_ISSUER, "auth0|other", "other@example.com", "other", NOW)
    return app


@pytest.fixture
def signed_address(signed_server) -> Iterator[str]:
    with running(signed_server) as address:
        yield address


def git(folder: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=folder, capture_output=True, text=True, check=True
    ).stdout


SHOP = "/apps/etienne/shop/mcp"
# The sentence every refusal of an app endpoint ends with, and what a limited token says
# everywhere else.
NEEDS = (
    "This address takes only a token limited to etienne/shop: make one at "
    "http://127.0.0.1:8800/tokens?app=etienne/shop, or connect an assistant at "
    "http://127.0.0.1:8800/me/mcp."
)
ELSEWHERE = (
    "This token is limited to etienne/shop as staff, at http://127.0.0.1:8800/apps/etienne/shop/mcp:"
    " connect an assistant at http://127.0.0.1:8800/me/mcp, or make another token at "
    "http://127.0.0.1:8800/tokens, to reach the rest."
)
# What a limited token is told once its person may no longer use the app.
NOT_SHARED = "There is no app etienne/shop for you. Check the address."


def limited(server, login: str, role: str, app: str = "shop") -> str:
    """A token of `login`'s, limited to etienne's `app` as `role`."""
    database = server.state.apps.database
    person = database.person_by_login(login)
    return database.create_token(
        person.id, f"{app} {role}", NOW, [TokenLimit("etienne", app, role)]
    )[1]


def post(address: str, path: str, token: str | None = None) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return httpx.post(f"{address}{path}", headers=headers)


def test_a_limited_token_gets_its_role_s_tools(signed_server, signed_address: str) -> None:
    staff = limited(signed_server, "sam", "staff")
    owner = limited(signed_server, "etienne", "owner")

    async def names(token: str) -> set[str]:
        async with app_client(signed_address, SHOP, token) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    sam = anyio.run(lambda: names(staff))
    assert "record_sale" in sam and "set_price" not in sam
    assert "set_price" in anyio.run(lambda: names(owner))


def test_a_limited_token_is_refused_everywhere_else(signed_server, signed_address: str) -> None:
    database = signed_server.state.apps.database
    signed_server.state.apps.import_folder(APPS / "stock", "etienne", "cafe")
    member(database, database.person_by_login("sam"), "etienne", "cafe", "staff")
    token = limited(signed_server, "sam", "staff")

    assert rpc_refusal(post(signed_address, "/apps/etienne/cafe/mcp", token), 403) == ELSEWHERE
    # The person's own address keeps its own answer.
    refused = post(signed_address, "/me/mcp", token)
    assert refused.status_code == 403
    assert refused.json() == {"error": ELSEWHERE}


def test_a_token_for_several_apps_works_at_each_as_its_role(
    signed_server, signed_address: str
) -> None:
    apps = signed_server.state.apps
    database = apps.database
    sam = database.person_by_login("sam")
    for name in ("cafe", "bar"):
        apps.import_folder(APPS / "stock", "etienne", name)
        member(database, sam, "etienne", name, "owner")
    shop, cafe = TokenLimit("etienne", "shop", "staff"), TokenLimit("etienne", "cafe", "owner")
    _, token = database.create_token(sam.id, "Routine", NOW, [shop, cafe])

    async def names(path: str) -> set[str]:
        async with app_client(signed_address, path, token) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    at_shop = anyio.run(lambda: names(SHOP))
    assert "record_sale" in at_shop and "set_price" not in at_shop
    assert "set_price" in anyio.run(lambda: names("/apps/etienne/cafe/mcp"))
    # Anywhere else, it says each app it works for, at its own address.
    elsewhere = (
        "This token is limited to etienne/cafe as owner and etienne/shop as staff, "
        "each at its own address, http://127.0.0.1:8800/apps/etienne/cafe/mcp, "
        "http://127.0.0.1:8800/apps/etienne/shop/mcp: connect an assistant at "
        "http://127.0.0.1:8800/me/mcp, or make another token at "
        "http://127.0.0.1:8800/tokens, to reach the rest."
    )
    assert rpc_refusal(post(signed_address, "/apps/etienne/bar/mcp", token), 403) == elsewhere
    refused = post(signed_address, "/me/mcp", token)
    assert refused.status_code == 403
    assert refused.json() == {"error": elsewhere}
    # Its role in one app goes with that share alone.
    database.remove_member("etienne", "cafe", sam.id)
    assert post(signed_address, "/apps/etienne/cafe/mcp", token).status_code == 404
    assert post(signed_address, SHOP, token).status_code != 403


def test_an_app_endpoint_takes_only_a_limited_token(signed_server, signed_address: str) -> None:
    database = signed_server.state.apps.database
    sam = database.person_by_login("sam")
    _, everywhere = database.create_token(sam.id, "home", NOW)
    signed_in = signed_server.state.fake_provider.token("sam")

    for token, status in ((None, 401), (signed_in, 401), ("not-a-token", 401), (everywhere, 403)):
        refused = post(signed_address, SHOP, token)
        assert rpc_refusal(refused, status) == NEEDS, token
        header = refused.headers.get("www-authenticate", "")
        # Nothing tells a client to sign in here, and no token is no invalid token.
        assert "resource_metadata" not in header and "example.eu" not in header, token
        assert ("invalid_token" in header) == (status == 401 and token is not None), token
    revoked = post(signed_address, SHOP, "cliquet_" + "x" * 43)
    assert rpc_refusal(revoked, 401) == "The token is invalid or was revoked. " + NEEDS
    assert revoked.headers["www-authenticate"] == (
        'Bearer error="invalid_token", error_description="The token is invalid or was '
        "revoked. " + NEEDS + '"'
    )
    assert post(signed_address, SHOP).headers["www-authenticate"] == "Bearer"


def test_no_app_endpoint_serves_protected_resource_metadata(
    signed_server, signed_address: str
) -> None:
    for path in (
        "/.well-known/oauth-protected-resource/apps/etienne/shop/mcp",
        "/.well-known/oauth-protected-resource/apps/etienne/shop/staff/mcp",
        "/.well-known/oauth-protected-resource",
    ):
        assert httpx.get(f"{signed_address}{path}").status_code == 404, path
    # The old addresses with a role are gone, and say where an agent connects now.
    signed_in = signed_server.state.fake_provider.token("sam")
    gone = post(signed_address, "/apps/etienne/shop/staff/mcp", signed_in)
    assert rpc_refusal(gone, 410) == (
        "This address is gone: connect an assistant at http://127.0.0.1:8800/me/mcp, or give "
        "an agent that runs on its own http://127.0.0.1:8800/apps/etienne/shop/mcp with a token "
        "limited to the app, made at http://127.0.0.1:8800/tokens?app=etienne/shop."
    )


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
def test_an_mcp_client_reads_why_the_app_s_address_refused(
    signed_server, signed_address: str, mode: str
) -> None:
    database = signed_server.state.apps.database
    sam = database.person_by_login("sam")
    signed_server.state.apps.import_folder(APPS / "stock", "etienne", "cafe")
    member(database, sam, "etienne", "cafe", "staff")
    token = limited(signed_server, "sam", "staff")
    _, everywhere = database.create_token(sam.id, "home", NOW)
    cafe = "/apps/etienne/cafe/mcp"

    async def why(path: str, token: str) -> str:
        """The message of the error the client raises, out of the groups its tasks wrap it in."""
        error: BaseException | None = None
        try:
            async with app_client(signed_address, path, token, mode=mode) as client:
                await client.list_tools()
        except* MCPError as raised:
            error = raised
        while isinstance(error, BaseExceptionGroup):
            (error,) = error.exceptions
        assert isinstance(error, MCPError), f"{path} took the token"
        return str(error)

    # Each sentence is Cliquet's, where the client would show its own text.
    assert anyio.run(why, cafe, token) == ELSEWHERE
    assert anyio.run(why, SHOP, everywhere) == NEEDS
    assert anyio.run(why, SHOP, "cliquet_" + "x" * 43) == (
        "The token is invalid or was revoked. " + NEEDS
    )
    database.remove_member("etienne", "shop", sam.id)
    assert anyio.run(why, SHOP, token) == NOT_SHARED
    # And each status is the one it was.
    assert post(signed_address, cafe, token).status_code == 403
    assert post(signed_address, SHOP, everywhere).status_code == 403
    assert post(signed_address, SHOP, token).status_code == 404


def test_a_limited_token_ends_with_the_role_then_the_app(signed_server, signed_address) -> None:
    apps = signed_server.state.apps
    database = apps.database
    sam = database.person_by_login("sam")
    token = limited(signed_server, "sam", "staff")
    assert post(signed_address, SHOP, token).status_code != 403

    # sam keeps another role: the token's own is gone.
    member(database, sam, "etienne", "shop", "owner")
    database.remove_member("etienne", "shop", sam.id)
    member(database, sam, "etienne", "shop", "owner")
    lost = post(signed_address, SHOP, token)
    assert rpc_refusal(lost, 403).startswith(
        "This token is limited to the role staff, which you no longer hold in etienne/shop"
    )
    # With no role left, the app isn't sam's to know of.
    database.remove_member("etienne", "shop", sam.id)
    missing = post(signed_address, SHOP, token)
    assert rpc_refusal(missing, 404) == NOT_SHARED
    # Shared again, and not accepted yet: the token is told how to accept it.
    assert sam.email is not None
    database.share("etienne", "shop", sam.email, "staff", NOW)
    waiting = post(signed_address, SHOP, token)
    assert rpc_refusal(waiting, 404) == (
        "etienne/shop waits for you to accept its share: accept it on the Apps page in the "
        "console, or ask your assistant to."
    )

    assert "staff" in database.accept_share(sam.id, "etienne", "shop", NOW)
    assert post(signed_address, SHOP, token).status_code != 403
    apps.delete("etienne", "shop")
    assert [one.limits for one in database.tokens(sam.id)] == [()]
    # An app made again with the same name never takes the old token.
    apps.import_folder(APPS / "stock", "etienne", "shop")
    member(database, sam, "etienne", "shop", "staff")
    gone = post(signed_address, SHOP, token)
    assert "invalid or was revoked" in rpc_refusal(gone, 401)


def test_the_caller_is_the_person(signed_server, signed_address: str) -> None:
    token = limited(signed_server, "sam", "staff")

    async def call() -> None:
        async with app_client(signed_address, SHOP, token) as client:
            done = await client.call_tool(
                "record_sale", {"sku": "TEA-SEN", "quantity": 2, "receipt": "R-0600"}
            )
            assert not done.is_error

    anyio.run(call)
    folder = signed_server.state.apps.repo("etienne", "shop").folder
    assert git(folder, "log", "-1", "--format=%an").strip() == "sam"
    assert "App-Role: staff" in git(folder, "log", "-1", "--format=%B")


def test_two_people_do_not_share_a_server(signed_server, signed_address: str) -> None:
    sam_token = limited(signed_server, "sam", "staff")
    owner_token = limited(signed_server, "etienne", "staff")

    async def connect() -> None:
        async with (
            app_client(signed_address, SHOP, sam_token) as sam,
            app_client(signed_address, SHOP, owner_token) as owner,
        ):
            assert "record_sale" in {t.name for t in (await sam.list_tools()).tools}
            assert "record_sale" in {t.name for t in (await owner.list_tools()).tools}

    anyio.run(connect)
    callers = {key[:4] for key in signed_server.state.registry._servers}
    assert ("etienne", "shop", "staff", "sam") in callers
    assert ("etienne", "shop", "staff", "etienne") in callers


def test_a_token_is_limited_only_to_a_role_its_person_holds(signed_server) -> None:
    database = signed_server.state.apps.database
    sam = database.person_by_login("sam")
    with pytest.raises(ValueError, match="You don't hold the role owner in etienne/shop"):
        database.create_token(sam.id, "boss", NOW, [TokenLimit("etienne", "shop", "owner")])
    with pytest.raises(ValueError, match="There is no app etienne/nope"):
        database.create_token(sam.id, "nope", NOW, [TokenLimit("etienne", "nope", "staff")])
    assert database.tokens(sam.id) == []


def test_a_limit_whose_app_is_gone_works_nowhere(signed_server) -> None:
    database = signed_server.state.apps.database
    token = limited(signed_server, "sam", "staff")
    # The app's row goes some other way than `delete_app`: its limit is left behind.
    with sqlite3.connect(signed_server.state.settings.data_dir / "server.db") as raw:
        raw.execute("DELETE FROM apps WHERE name = 'shop'")
    assert database.token_access(token, NOW) is None


def test_a_limited_token_without_limits_works_nowhere(signed_server, signed_address) -> None:
    database = signed_server.state.apps.database
    token = limited(signed_server, "sam", "staff")
    # Its limits go some other way than `delete_app`: the token is still limited.
    with sqlite3.connect(signed_server.state.settings.data_dir / "server.db") as raw:
        raw.execute("DELETE FROM token_limits")
    assert database.token_access(token, NOW) is None
    for path in (SHOP, "/me/mcp"):
        assert post(signed_address, path, token).status_code == 401, path
