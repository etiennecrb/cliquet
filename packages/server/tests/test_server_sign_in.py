import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path

import anyio
import httpx
import mcp_types as types
import pytest
from fastmcp.client.client import CallToolResult
from fastmcp.exceptions import ToolError
from fastmcp.utilities.tests import asgi_client
from joserfc import jwt
from joserfc.jwk import OctKey
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from cliquet_server.app import create_app
from cliquet_server.app_auth import CHOOSE_FIRST, AppAuth
from cliquet_server.apps import Apps
from cliquet_server.builder import builder_enabled
from cliquet_server.callers import EndpointAuth
from cliquet_server.db import Database, Right, may
from cliquet_server.executor import LocalExecutor
from cliquet_server.oidc import Provider
from cliquet_server.settings import Settings, SettingsError
from cliquet_server.sign_in import development_person

from .server_helpers import (
    APPS,
    AUTH0_ISSUER,
    ME,
    NOW,
    PEOPLE,
    REALM_ISSUER,
    FakeProvider,
    app_client,
    app_token,
    auth0_provider,
    me_server,
    oidc_env,
    oidc_server,
    oidc_settings,
    realm_provider,
    running,
)

SHAPES = {"auth0": auth0_provider, "realm": realm_provider}
# A test run against both shapes of provider, or the one whose tokens hold the email.
BOTH = pytest.mark.parametrize("provider", ["auth0", "realm"], indirect=True)
REALM = pytest.mark.parametrize("provider", ["realm"], indirect=True)


@pytest.fixture
def provider(request: pytest.FixtureRequest) -> FakeProvider:
    """The sign-in provider, shaped as Auth0 unless a test names another shape."""
    return SHAPES[getattr(request, "param", "auth0")]()


@pytest.fixture
def oidc_apps(tmp_path: Path, provider: FakeProvider, executor: LocalExecutor) -> Iterator[Apps]:
    """Apps whose settings sign in through `provider`, on the shared local sandbox."""
    settings = oidc_settings(tmp_path, provider.issuer)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    database = Database(settings.database_url)
    apps = Apps(settings, database, executor, clock=lambda: NOW)
    yield apps
    apps.close()
    database.close()


def signed_up(database: Database, issuer: str = AUTH0_ISSUER) -> None:
    """Give etienne and sam the logins they chose at their first sign-in, and the right to
    build."""
    for who in ("etienne", "sam"):
        database.person_for(issuer, PEOPLE[who][0], f"{who}@example.com", who, NOW)
        database.grant(who, Right.BUILD)


def signed(apps: Apps, provider: FakeProvider) -> EndpointAuth:
    """The sign-in of the person's endpoint, with `provider` signing its tokens."""
    checked = Provider(apps.settings, provider.transport)
    app_auth = AppAuth(apps.settings, apps.database, clock=lambda: NOW, provider=checked)
    return EndpointAuth(app_auth, "/me")


def owned(answer: CallToolResult | types.CallToolResult) -> list[str]:
    """The apps `list_apps` lists, as owner/name."""
    assert not answer.is_error, answer.content
    return [row["app"] for row in (answer.structured_content or {})["apps"]]


def test_settings_need_all_of_the_provider(tmp_path: Path) -> None:
    with pytest.raises(SettingsError) as partial:
        Settings.from_env({"CLIQUET_OIDC_ISSUER": AUTH0_ISSUER})
    assert "CLIQUET_OIDC_CLIENT_ID" in str(partial.value)
    assert "CLIQUET_OIDC_CLIENT_SECRET" in str(partial.value)
    assert "CLIQUET_SECRET_KEY" in str(partial.value)

    with pytest.raises(SettingsError, match="CLIQUET_SECRET_KEY"):
        oidc_settings(tmp_path, CLIQUET_SECRET_KEY="short")

    with pytest.raises(SettingsError, match="CLIQUET_DEV_LOGIN"):
        Settings.from_env(
            {**oidc_env(tmp_path), "CLIQUET_AUTH": "", "CLIQUET_DEV_LOGIN": "etienne"}
        )

    with pytest.raises(SettingsError, match="CLIQUET_AUTH"):
        oidc_settings(tmp_path, CLIQUET_AUTH="google")

    with pytest.raises(SettingsError, match="kept for Cliquet's own pages"):
        Settings.from_env({"CLIQUET_DEV_LOGIN": "me"})


def test_the_issuer_is_https_or_local(tmp_path: Path) -> None:
    for issuer in ("http://login.example.org/", "login.example.org", "https://x.example/?a=1"):
        with pytest.raises(SettingsError, match=r"CLIQUET_OIDC_ISSUER: .*https://"):
            oidc_settings(tmp_path, issuer)
    # Exactly as written: a slash is neither added nor taken away.
    assert oidc_settings(tmp_path, REALM_ISSUER).oidc_issuer == REALM_ISSUER
    assert oidc_settings(tmp_path, AUTH0_ISSUER).oidc_issuer == AUTH0_ISSUER
    local = "http://localhost:8080/realms/cliquet"
    assert oidc_settings(tmp_path, local).oidc_issuer == local


def test_the_builder_is_on_with_a_provider(tmp_path: Path) -> None:
    assert builder_enabled(oidc_settings(tmp_path)) is True
    assert builder_enabled(Settings.from_env({"CLIQUET_DEV_LOGIN": "etienne"})) is True
    assert builder_enabled(Settings.from_env({"CLIQUET_DATA_DIR": str(tmp_path / "off")})) is False


@BOTH
def test_a_signed_in_person_builds(oidc_apps: Apps, provider: FakeProvider) -> None:
    signed_up(oidc_apps.database, provider.issuer)
    mcp = me_server(oidc_apps, auth=signed(oidc_apps, provider))

    async def main() -> None:
        async with asgi_client(mcp, auth=provider.token("etienne")) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert "create_app" in names
            await client.call_tool("create_app", {"app": "reading"})

    anyio.run(main)
    assert oidc_apps.database.app("etienne", "reading") is not None
    person = oidc_apps.database.person_by_login("etienne")
    assert person is not None
    assert person.email == "etienne@example.com"
    assert person.subject == "auth0|etienne"
    assert person.issuer == provider.issuer


def test_a_token_without_email_finds_an_existing_person(
    oidc_apps: Apps, provider: FakeProvider
) -> None:
    oidc_apps.database.person_for(AUTH0_ISSUER, "auth0|email-less", "five@example.com", "five", NOW)
    oidc_apps.database.grant("five", Right.BUILD)
    mcp = me_server(oidc_apps, auth=signed(oidc_apps, provider))

    async def main() -> None:
        async with asgi_client(mcp, auth=provider.token("email-less")) as client:
            await client.call_tool("create_app", {"app": "reading"})
            assert owned(await client.call_tool("list_apps", {})) == ["five/reading"]

    anyio.run(main)


@BOTH
def test_a_person_with_no_login_yet_chooses_one_first(
    oidc_apps: Apps, provider: FakeProvider
) -> None:
    mcp = me_server(oidc_apps, auth=signed(oidc_apps, provider))

    async def main() -> None:
        async with asgi_client(mcp, auth=provider.token("collision")) as client:
            with pytest.raises(ToolError) as refused:
                await client.call_tool("list_apps", {})
            assert str(refused.value) == CHOOSE_FIRST

    anyio.run(main)
    assert oidc_apps.database.person_by_email("etienne@other.example.com") is None


@REALM
def test_an_unverified_email_claims_nobody(oidc_apps: Apps, provider: FakeProvider) -> None:
    # etienne's apps wait for the email the token names, unverified.
    oidc_apps.import_folder(APPS / "stock", "etienne", "shop", email="etienne@example.com")
    mcp = me_server(oidc_apps, auth=signed(oidc_apps, provider))

    async def main() -> None:
        async with asgi_client(mcp, auth=provider.token("unverified")) as client:
            with pytest.raises(ToolError) as refused:
                await client.call_tool("list_apps", {})
            assert str(refused.value) == CHOOSE_FIRST

    anyio.run(main)
    imported = oidc_apps.database.person_by_login("etienne")
    assert imported is not None and imported.subject is None


def test_no_token_is_refused(oidc_apps: Apps, provider: FakeProvider) -> None:
    mcp = me_server(oidc_apps, auth=signed(oidc_apps, provider))

    async def main() -> None:
        with pytest.raises(MCPError):
            async with asgi_client(mcp):
                pass

    anyio.run(main)


def test_owners_are_kept_apart(oidc_apps: Apps, provider: FakeProvider) -> None:
    signed_up(oidc_apps.database)
    mcp = me_server(oidc_apps, auth=signed(oidc_apps, provider))

    async def main() -> None:
        async with asgi_client(mcp, auth=provider.token("etienne")) as client:
            await client.call_tool("create_app", {"app": "reading"})
        async with asgi_client(mcp, auth=provider.token("sam")) as client:
            assert owned(await client.call_tool("list_apps", {})) == []
            with pytest.raises(ToolError, match="reading"):
                await client.call_tool("read_file", {"app": "reading", "path": "app.py"})

    anyio.run(main)


def test_the_app_mcp_needs_a_token(settings: Settings, executor: LocalExecutor) -> None:
    server = create_app(settings, executor=executor)
    server.state.apps.import_folder(APPS / "stock", "etienne", "shop")

    with running(server) as address:

        async def main() -> None:
            token = app_token(server, "staff")
            async with app_client(address, "/apps/etienne/shop/mcp", token) as client:
                names = {tool.name for tool in (await client.list_tools()).tools}
                assert "record_sale" in names

        anyio.run(main)
        no_token = httpx.post(f"{address}/apps/etienne/shop/mcp")
        assert no_token.status_code == 401
        with TestClient(server) as client:
            home = client.get("/", follow_redirects=False)
            page = client.get("/apps/etienne/shop", follow_redirects=False)
            assert home.status_code == 303 and home.headers["location"] == "/login?next=%2F"
            assert page.status_code == 303
            assert "Stock for a small tea shop" not in home.text + page.text


@REALM
def test_an_imported_owner_is_claimed_by_their_email(
    oidc_apps: Apps, provider: FakeProvider
) -> None:
    oidc_apps.import_folder(APPS / "stock", "the-shop", "shop", email="a@example.com")
    mcp = me_server(oidc_apps, auth=signed(oidc_apps, provider))

    async def other() -> None:
        async with asgi_client(mcp, auth=provider.token("other-a")) as client:
            with pytest.raises(ToolError, match="no login on Cliquet yet"):
                await client.call_tool("list_apps", {})

    anyio.run(other)
    waiting = oidc_apps.database.person_by_login("the-shop")
    assert waiting is not None
    assert waiting.issuer is None and waiting.subject is None
    assert waiting.email == "a@example.com"

    async def owner() -> None:
        async with asgi_client(mcp, auth=provider.token("a")) as client:
            assert owned(await client.call_tool("list_apps", {})) == ["the-shop/shop"]

    anyio.run(owner)
    claimed = oidc_apps.database.person_by_login("the-shop")
    assert claimed is not None
    assert claimed.id == waiting.id
    assert claimed.subject == "auth0|a" and claimed.issuer == REALM_ISSUER


def test_an_imported_owner_without_email_is_never_claimed(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    server = oidc_server(tmp_path, executor)
    server.state.apps.import_folder(APPS / "stock", "etienne", "shop")
    database = server.state.apps.database

    # The web: the person chooses a login, and the imported one is taken.
    with TestClient(server) as client:
        start = client.get("/login", follow_redirects=False)
        back = server.state.fake_provider.authorize(start.headers["location"], "etienne")
        web = client.get(back, follow_redirects=False)
        assert web.headers["location"] == "/auth/choose-login"
        assert 'session=""' in web.headers["set-cookie"]
        page = client.get("/auth/choose-login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
        assert csrf is not None
        taken = client.post(
            "/auth/choose-login", data={"csrf": csrf[1], "login": "etienne"}, follow_redirects=False
        )
    assert taken.status_code == 400
    assert "already has the login" in taken.text

    person = database.person_by_login("etienne")
    assert person is not None
    assert person.issuer is None and person.subject is None and person.email is None


@pytest.fixture
def oidc_running(
    tmp_path: Path, provider: FakeProvider, executor: LocalExecutor
) -> Iterator[Starlette]:
    """The whole server signing in through `provider`, running at `state.address`."""
    server = oidc_server(tmp_path, executor, provider)
    signed_up(server.state.apps.database, provider.issuer)
    with running(server) as address:
        server.state.address = address
        yield server


def test_the_builder_s_old_address_is_gone(oidc_running) -> None:
    address = oidc_running.state.address
    gone = [
        httpx.post(f"{address}/builder/mcp", json={}),
        httpx.get(f"{address}/builder/mcp"),
        httpx.delete(f"{address}/builder/mcp"),
        httpx.post(f"{address}/builder/mcp/messages", json={}),
        httpx.get(f"{address}/.well-known/oauth-protected-resource/builder/mcp"),
    ]
    for answer in gone:
        assert answer.status_code == 410
        # No challenge, so no client tries to sign in there.
        assert "www-authenticate" not in answer.headers
        assert answer.json() == {
            "error": "This address is gone: connect your assistant at "
            "http://127.0.0.1:8800/me/mcp, which serves the builder's tools to the people who "
            "may build."
        }


@BOTH
def test_me_is_the_one_resource_and_an_app_s_has_none(oidc_running, provider: FakeProvider) -> None:
    address = oidc_running.state.address
    path = "/.well-known/oauth-protected-resource/me/mcp"
    challenge = httpx.post(f"{address}/me/mcp", json={}).headers["www-authenticate"]
    # The challenge names the public address the settings give.
    assert f'resource_metadata="http://127.0.0.1:8800{path}"' in challenge
    document = httpx.get(f"{address}{path}").json()
    # The provider's issuer, exactly, and the endpoint's own address as the resource: claude.ai
    # refuses metadata that names another, and opens /authorize on Cliquet instead.
    assert document["resource"] == "http://127.0.0.1:8800/me/mcp"
    assert document["authorization_servers"] == [provider.issuer]
    assert document["scopes_supported"] == ["openid", "email"]
    # No authorization server of its own: nothing registers a client or issues a token.
    for path in (
        "/me/register",
        "/me/authorize",
        "/me/token",
        "/me/consent",
        "/.well-known/oauth-authorization-server/me",
        "/.well-known/oauth-authorization-server",
    ):
        assert httpx.get(f"{address}{path}").status_code in {401, 404, 405}, path
        assert httpx.post(f"{address}{path}").status_code in {401, 404, 405}, path
    assert not (oidc_running.state.settings.data_dir / "oauth").exists()
    # An app's endpoint takes only a token limited to it: no client is sent to sign in.
    app = "/apps/etienne/shop/mcp"
    challenge = httpx.post(f"{address}{app}", json={}).headers["www-authenticate"]
    assert "resource_metadata" not in challenge
    metadata = httpx.get(f"{address}/.well-known/oauth-protected-resource{app}")
    assert metadata.status_code == 404


def test_a_pathless_issuer_is_named_without_a_slash_added(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    provider = FakeProvider("https://login.example.org", "/oauth")
    server = oidc_server(tmp_path, executor, provider)
    with TestClient(server) as client:
        document = client.get("/.well-known/oauth-protected-resource/me/mcp").json()
    assert document["authorization_servers"] == ["https://login.example.org"]


def status_of(address: str, path: str, bearer: str) -> int:
    """The status of an empty MCP request to `path` with a bearer token."""
    headers = {"Authorization": f"Bearer {bearer}"}
    return httpx.post(f"{address}{path}", json={}, headers=headers).status_code


@BOTH
def test_me_takes_the_provider_s_tokens_for_its_own_address_only(
    oidc_running, provider: FakeProvider
) -> None:
    address = oidc_running.state.address
    # Past the sign-in, an empty body is a bad request. A third-party client may be granted
    # `offline_access` alone, without the `openid` and `email` it asked for.
    assert status_of(address, "/me/mcp", provider.token("etienne", scope="offline_access")) == 400
    assert status_of(address, "/me/mcp", provider.token("etienne", [ME, "other"])) == 400
    # The audience once shared by every endpoint, and the builder's old one, no longer.
    old = provider.token("etienne", "https://cliquet.example/api")
    assert status_of(address, "/me/mcp", old) == 401
    builder = provider.token("etienne", "http://127.0.0.1:8800/builder/mcp")
    assert status_of(address, "/me/mcp", builder) == 401
    # Another issuer, an expired token, a token signed with a shared secret or not at all.
    assert status_of(address, "/me/mcp", provider.token("etienne", iss="https://x.example/")) == 401
    expired = provider.token("etienne", exp=int(time.time()) - 60)
    assert status_of(address, "/me/mcp", expired) == 401
    shared = jwt.encode(
        {"alg": "HS256"},
        {"iss": provider.issuer, "sub": "auth0|etienne", "aud": ME},
        OctKey.import_key("a-secret-shared-with-nobody-at-all"),
    )
    assert status_of(address, "/me/mcp", shared) == 401
    unsigned = provider.token("etienne").rsplit(".", 1)[0] + "."
    assert status_of(address, "/me/mcp", unsigned) == 401
    # Another provider's key, with the same issuer and audience.
    other = realm_provider() if provider.algorithm == "RS256" else auth0_provider()
    forged = other.token("etienne", iss=provider.issuer)
    assert status_of(address, "/me/mcp", forged) == 401
    # An app's endpoint takes no token of the provider, whatever its audience.
    app = "/apps/etienne/shop/mcp"
    assert status_of(address, app, old) == 401
    assert status_of(address, app, provider.token("etienne")) == 401


def test_the_audience_can_be_set(tmp_path: Path, executor: LocalExecutor) -> None:
    provider = auth0_provider()
    server = oidc_server(
        tmp_path, executor, provider, CLIQUET_OIDC_AUDIENCE="https://api.example.com"
    )
    signed_up(server.state.apps.database)
    with running(server) as address:
        api = provider.token("etienne", "https://api.example.com")
        assert status_of(address, "/me/mcp", api) == 400
        assert status_of(address, "/me/mcp", provider.token("etienne")) == 401
        # The metadata still names the endpoint's own address, which clients ask for.
        path = "/.well-known/oauth-protected-resource/me/mcp"
        assert httpx.get(f"{address}{path}").json()["resource"] == ME


def test_an_issuer_the_provider_doesn_t_name_is_refused(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    # The settings name the tenant without its final slash: its tokens name it with one.
    provider = auth0_provider()
    server = oidc_server(tmp_path, executor, provider, CLIQUET_OIDC_ISSUER=AUTH0_ISSUER.rstrip("/"))
    signed_up(server.state.apps.database, AUTH0_ISSUER)
    with running(server) as address:
        assert status_of(address, "/me/mcp", provider.token("etienne")) == 401
        with httpx.Client(base_url=address) as client:
            refused = client.get("/login", follow_redirects=False)
    assert refused.status_code == 502
    assert (
        "The sign-in provider names its issuer &#39;https://example.eu.auth0.com/&#39;, not "
        "&#39;https://example.eu.auth0.com&#39;: set CLIQUET_OIDC_ISSUER to its issuer exactly"
    ) in refused.text


NO_REGISTRATION = (
    "The sign-in provider {} registers no MCP client: it offers neither dynamic client "
    "registration nor client ID metadata documents. Assistants can't sign in at "
    "http://127.0.0.1:8800/me/mcp; API tokens still work."
)


def provider_said(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What the sign-in provider's module logged."""
    return [one.getMessage() for one in caplog.records if one.name == "cliquet_server.oidc"]


def started(server: Starlette, caplog: pytest.LogCaptureFixture) -> list[str]:
    """What the sign-in provider's check logged as the server started, once it is done."""
    caplog.set_level(logging.WARNING, logger="cliquet_server")
    with TestClient(server) as client:
        assert server.state.provider.checked.wait(10)
        # The server answered meanwhile, and still does.
        assert client.get("/healthz").status_code == 200
    return provider_said(caplog)


@pytest.mark.parametrize(
    ("provider", "warned"),
    [("auth0", False), ("realm", True)],
    indirect=["provider"],
)
def test_the_server_says_at_start_when_assistants_can_t_sign_in(
    tmp_path: Path,
    executor: LocalExecutor,
    caplog: pytest.LogCaptureFixture,
    provider: FakeProvider,
    warned: bool,
) -> None:
    logged = started(oidc_server(tmp_path, executor, provider), caplog)
    assert logged == ([NO_REGISTRATION.format(provider.issuer)] if warned else [])


def test_client_id_metadata_documents_alone_are_enough(
    tmp_path: Path, executor: LocalExecutor, caplog: pytest.LogCaptureFixture
) -> None:
    provider = auth0_provider(registration_endpoint=None)
    assert started(oidc_server(tmp_path, executor, provider), caplog) == []


def test_an_unreachable_provider_is_read_once_it_answers(
    tmp_path: Path, executor: LocalExecutor, caplog: pytest.LogCaptureFixture
) -> None:
    provider = auth0_provider()
    provider.down = True
    server = oidc_server(tmp_path, executor, provider)
    signed_up(server.state.apps.database)
    caplog.set_level(logging.WARNING, logger="cliquet_server")
    with running(server) as address:
        assert server.state.provider.checked.wait(10)
        (logged,) = provider_said(caplog)
        assert logged.startswith(f"The sign-in provider {AUTH0_ISSUER} did not answer at ")
        assert logged.endswith("Cliquet reads it again when someone signs in.")
        assert status_of(address, "/me/mcp", provider.token("etienne")) == 401
        provider.down = False
        assert status_of(address, "/me/mcp", provider.token("etienne")) == 400


def test_a_provider_token_and_an_api_token_build(oidc_running, provider: FakeProvider) -> None:
    address = oidc_running.state.address
    database = oidc_running.state.apps.database

    async def apps_of(token: str) -> list[str]:
        async with app_client(address, "/me/mcp", token) as client:
            return owned(await client.call_tool("list_apps", {}))

    async def create(token: str, app: str) -> None:
        async with app_client(address, "/me/mcp", token) as client:
            made = await client.call_tool("create_app", {"app": app})
            assert not made.is_error, made.content

    anyio.run(lambda: create(provider.token("etienne"), "reading"))
    etienne = database.person_by_login("etienne")
    assert etienne is not None and etienne.subject == "auth0|etienne"
    info, token = database.create_token(etienne.id, "Laptop", NOW)
    assert anyio.run(lambda: apps_of(token)) == ["etienne/reading"]
    assert database.tokens(etienne.id)[0].last_used_at == NOW
    database.revoke_token(etienne.id, info.id)
    revoked = httpx.post(f"{address}/me/mcp", json={}, headers={"Authorization": f"Bearer {token}"})
    assert revoked.status_code == 401


def test_an_api_token_is_its_person_under_the_development_login(
    settings: Settings, executor: LocalExecutor
) -> None:
    server = create_app(settings, executor=executor, clock=lambda: NOW)
    database = server.state.apps.database
    sam = database.person_for(AUTH0_ISSUER, "auth0|sam", "s@x.example", "sam", NOW)
    database.grant("sam", Right.BUILD)
    _, token = database.create_token(sam.id, "home", NOW)

    with running(server) as address:

        async def owner(bearer: str) -> bool:
            async with app_client(address, "/me/mcp", bearer) as client:
                made = await client.call_tool("create_app", {"app": "notes"})
                return not made.is_error

        assert anyio.run(lambda: owner(token)) is True
        assert anyio.run(lambda: owner("dev")) is True
        wrong = httpx.post(
            f"{address}/me/mcp", json={}, headers={"Authorization": "Bearer cliquet_wrong"}
        )
        assert wrong.status_code == 401
    # The API token's person made one, and any other token is the development login.
    assert database.app("sam", "notes") is not None
    assert database.app("etienne", "notes") is not None


def test_the_development_login_builds_at_each_sign_in(
    database: Database, settings: Settings
) -> None:
    person = development_person(database, settings, NOW)
    assert may(person, Right.BUILD)
    database.withdraw("etienne", Right.BUILD)
    assert may(development_person(database, settings, NOW), Right.BUILD)


def test_keys_are_read_where_the_discovery_document_now_says(tmp_path: Path) -> None:
    provider = auth0_provider()
    checked = Provider(oidc_settings(tmp_path), provider.transport)
    token = provider.token("sam")

    async def verify() -> object:
        return await checked.verify(token)

    assert anyio.run(verify) is not None
    # The provider moves its keys, and the document is read again an hour later.
    provider.document["jwks_uri"] = f"{AUTH0_ISSUER}keys/moved"
    checked._read_at -= 2 * 60 * 60
    moved = len(provider.requests)
    assert anyio.run(verify) is not None
    assert str(provider.requests[-1].url) == f"{AUTH0_ISSUER}keys/moved"
    assert len(provider.requests) == moved + 2
