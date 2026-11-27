import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

import anyio
import httpx
import httpx2
import pytest
from starlette.testclient import TestClient

from cliquet_server.db import PersonInfo
from cliquet_server.executor import LocalExecutor
from cliquet_server.oidc import Provider
from cliquet_server.settings import AUTHORIZE_PARAMS, Settings, SettingsError
from cliquet_server.web_sign_in import _authorize_params

from .server_helpers import (
    APPS,
    NOW,
    FakeProvider,
    act_as,
    auth0_provider,
    member,
    oidc_callback,
    oidc_server,
    oidc_settings,
    realm_provider,
    signed_in,
)


def git(folder: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=folder, capture_output=True, text=True, check=True
    ).stdout


def a_member(server, login: str = "sam", email: str = "sam@example.com") -> PersonInfo:
    """A person who may use etienne/shop as staff."""
    database = server.state.apps.database
    person = database.person_for(
        "https://example.eu.auth0.com/", f"auth0|{login}", email, login, NOW
    )
    return member(database, person, "etienne", "shop", "staff")


def hosted(tmp_path: Path, executor: LocalExecutor, provider: FakeProvider | None = None):
    """The server signing in through `provider`, Auth0's shape by default, at a public HTTPS
    address."""
    return oidc_server(
        tmp_path,
        executor,
        provider,
        CLIQUET_BASE_URL="https://cliquet.example",
        CLIQUET_ALLOW_REMOTE_LOCAL_EXECUTOR="1",
    )


def test_an_anonymous_visitor_is_sent_to_sign_in(server) -> None:
    with TestClient(server) as client:
        home = client.get("/", follow_redirects=False)
        page = client.get("/apps/etienne/shop", follow_redirects=False)

    # The home page says nothing of its own: the website says what Cliquet is.
    assert home.status_code == 303
    assert home.headers["location"] == "/login?next=%2F"
    assert page.status_code == 303
    assert page.headers["location"].startswith("/login?next=")
    assert "Stock for a small tea shop" not in page.text


def test_a_signed_in_owner_sees_their_apps(server) -> None:
    with signed_in(server) as client:
        home = client.get("/")
        page = client.get("/apps/etienne/shop")

    assert home.status_code == 200
    assert 'href="/manage/etienne/shop"' in home.text
    assert "etienne" in home.text
    assert "Sign out" in home.text
    assert page.status_code == 200
    assert page.url.path == "/apps/etienne/shop/owner"


def test_a_member_sees_only_their_apps(server) -> None:
    database = server.state.apps.database
    database.create_app("other", "hidden", NOW)
    database.mark_published("other", "hidden", NOW, "Someone else's app")
    person = a_member(server)

    with TestClient(server) as client:
        act_as(server, client, person)
        home = client.get("/")

    assert home.status_code == 200
    assert 'href="/apps/etienne/shop"' in home.text
    assert "/hidden" not in home.text


def test_a_member_can_open_only_their_role(server) -> None:
    person = a_member(server)

    with TestClient(server) as client:
        act_as(server, client, person)
        page = client.get("/apps/etienne/shop")
        staff = client.get("/apps/etienne/shop/staff")
        owner = client.get("/apps/etienne/shop/owner")

    assert page.status_code == 200
    assert page.url.path == "/apps/etienne/shop/staff"
    assert 'href="/apps/etienne/shop/owner"' not in page.text
    assert staff.status_code == 200
    assert owner.status_code == 403
    assert "etienne/shop" not in owner.text


def test_the_web_caller_is_the_person(server) -> None:
    person = a_member(server)

    with TestClient(server) as client:
        act_as(server, client, person)
        done = client.post(
            "/apps/etienne/shop/staff/rpc",
            json={
                "method": "tools/call",
                "params": {
                    "name": "record_sale",
                    "arguments": {"sku": "TEA-SEN", "quantity": 2, "receipt": "R-0500"},
                },
            },
        ).json()

    assert not done.get("isError")
    folder = server.state.apps.repo("etienne", "shop").folder
    assert git(folder, "log", "-1", "--format=%an").strip() == "sam"
    body = git(folder, "log", "-1", "--format=%B")
    # A change made on the web names no token.
    assert "App-Role: staff" in body and "App-Agent" not in body


def test_the_dev_login_still_opens_a_local_page(server) -> None:
    with TestClient(server) as client:
        login = client.get("/login?next=/apps/etienne/shop", follow_redirects=False)
        page = client.get("/apps/etienne/shop")
        home = client.get("/")

    assert login.status_code == 303
    assert login.headers["location"] == "/apps/etienne/shop"
    assert page.status_code == 200
    assert "etienne" in home.text
    assert "Sign out" in home.text


def test_sign_out_is_a_post_with_a_token(server) -> None:
    with signed_in(server) as client:
        token = server.state.web_auth.csrf_for_session(client.cookies.get("session"))
        assert token
        by_get = client.get("/logout", follow_redirects=False)
        still = client.get("/", follow_redirects=False)
        no_token = client.post("/logout", follow_redirects=False)
        wrong = client.post("/logout", data={"csrf": "not-the-token"}, follow_redirects=False)
        kept = client.get("/", follow_redirects=False)
        logout = client.post("/logout", data={"csrf": token}, follow_redirects=False)
        home = client.get("/", follow_redirects=False)

    assert by_get.status_code == 405
    assert "Sign in" not in still.text
    assert no_token.status_code == 403
    assert wrong.status_code == 403
    assert "Sign in" not in kept.text
    # With the development login, a page says so: the home page would sign them in again.
    assert logout.status_code == 200 and "You are signed out of Cliquet." in logout.text
    assert 'href="/login"' in logout.text and 'session=""' in logout.headers["set-cookie"]
    assert home.status_code == 303 and home.headers["location"] == "/login?next=%2F"


def test_the_session_cookie_is_hard_to_read_and_send_elsewhere(server) -> None:
    with TestClient(server) as client:
        response = client.get("/login", follow_redirects=False)

    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie
    assert "Secure" not in cookie


@pytest.mark.parametrize("shape", [auth0_provider, realm_provider])
def test_the_login_goes_to_the_discovered_endpoint_with_pkce(
    tmp_path: Path, executor: LocalExecutor, shape: Callable[[], FakeProvider]
) -> None:
    provider = shape()
    app = hosted(tmp_path, executor, provider)
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/login?next=/apps/etienne/shop", follow_redirects=False)
        sign_up = client.get("/signup?next=/apps/etienne/shop", follow_redirects=False)

    # Without CLIQUET_SIGN_UP_PARAMS, signing up is signing in.
    signed_up = dict(httpx.URL(sign_up.headers["location"]).params)
    assert signed_up.keys() == dict(httpx.URL(response.headers["location"]).params).keys()
    assert response.status_code == 302
    location = httpx.URL(response.headers["location"])
    assert str(location.copy_with(query=None)) == provider.document["authorization_endpoint"]
    query = dict(location.params)
    assert query["response_type"] == "code"
    assert query["client_id"] == "the-client-id"
    assert query["redirect_uri"] == "https://cliquet.example/auth/callback"
    assert query["scope"] == "openid email profile"
    assert query["code_challenge_method"] == "S256"
    assert len(query["code_challenge"]) == 43 and len(query["state"]) >= 43
    # The token serves only to read who the person is: no audience, no resource.
    assert "audience" not in query and "resource" not in query
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie


def test_sign_up_opens_the_provider_on_the_parameters_the_settings_name(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    app = oidc_server(tmp_path, executor, CLIQUET_SIGN_UP_PARAMS="screen_hint=signup&prompt=login")
    with TestClient(app) as client:
        login = client.get("/login", follow_redirects=False)
        sign_up = client.get("/signup", follow_redirects=False)

    def asked(response: httpx2.Response) -> dict[str, str]:
        assert response.status_code == 302
        return dict(httpx.URL(response.headers["location"]).params)

    assert "screen_hint" not in asked(login)
    query = asked(sign_up)
    assert query["screen_hint"] == "signup" and query["prompt"] == "login"
    assert query["client_id"] == "the-client-id" and query["code_challenge_method"] == "S256"


def test_no_setting_sets_a_parameter_of_every_sign_in() -> None:
    settings = Settings.from_env({})
    assert set(_authorize_params(settings, "state", "challenge")) == AUTHORIZE_PARAMS


@pytest.mark.parametrize(
    "value",
    [
        "screen_hint",
        "=signup",
        "client_id=someone-else",
        "state=fixed&screen_hint=signup",
        "screen_hint=signup&screen_hint=login",
    ],
)
def test_sign_up_parameters_are_a_query_that_changes_nothing_cliquet_sets(value: str) -> None:
    with pytest.raises(SettingsError, match="CLIQUET_SIGN_UP_PARAMS"):
        Settings.from_env({"CLIQUET_SIGN_UP_PARAMS": value})
    # A parameter with no value is one too.
    blank = Settings.from_env({"CLIQUET_SIGN_UP_PARAMS": "screen_hint=signup&ui_locales="})
    assert blank.sign_up_params == (("screen_hint", "signup"), ("ui_locales", ""))


def test_the_sign_up_parameters_are_read_from_the_server_s_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The server reads its settings from its own environment too, which a host such as Fly
    # sets: a query string there is not JSON.
    monkeypatch.setenv("CLIQUET_SIGN_UP_PARAMS", "screen_hint=signup")
    settings = Settings.from_env(dict(os.environ))
    assert settings.sign_up_params == (("screen_hint", "signup"),)
    assert Settings().sign_up_params == (("screen_hint", "signup"),)


def test_the_development_login_signs_up_as_it_signs_in(server) -> None:
    with TestClient(server) as client:
        response = client.get("/signup?next=/tokens", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/tokens"


@pytest.mark.parametrize("shape", [auth0_provider, realm_provider])
def test_the_callback_reads_the_person_from_userinfo_and_claims_invitations(
    tmp_path: Path, executor: LocalExecutor, shape: Callable[[], FakeProvider]
) -> None:
    provider = shape()
    app = hosted(tmp_path, executor, provider)
    database = app.state.apps.database
    app.state.apps.import_folder(APPS / "stock", "etienne", "shop")
    database.share("etienne", "shop", "sam@example.com", "staff", NOW)
    # sam signed in before, and was invited since.
    sam = database.person_for(provider.issuer, "auth0|sam", "sam@example.com", "sam", NOW)
    assert database.pending_shares(sam.id) == []
    with TestClient(app, base_url="https://testserver") as client:
        assert oidc_callback(app, client, "sam", "/tokens") == "/tokens"
        assert client.get("/tokens").status_code == 200
    assert [share.name for share in database.pending_shares(sam.id)] == ["shop"]
    token, userinfo = provider.requests[-2:]
    assert token.url == provider.document["token_endpoint"]
    assert userinfo.url == provider.document["userinfo_endpoint"]
    form = dict(parse_qsl(token.content.decode()))
    assert form["grant_type"] == "authorization_code"
    assert form["redirect_uri"] == "https://cliquet.example/auth/callback"
    # The secret, the way the provider lists: in the form for Auth0, else by basic auth.
    methods = provider.document["token_endpoint_auth_methods_supported"]
    in_form = isinstance(methods, list) and "client_secret_post" in methods
    assert ("client_secret" in form) == in_form
    assert ("authorization" in token.headers) != in_form
    assert userinfo.headers["authorization"] == "Bearer opaque-sam"


def test_the_callback_checks_the_issuer_the_provider_names(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    app = hosted(tmp_path, executor)
    with TestClient(app, base_url="https://testserver") as client:
        start = client.get("/login", follow_redirects=False)
        back = app.state.fake_provider.authorize(start.headers["location"], "sam")
        url = httpx.URL(back)
        params = {k: v for k, v in url.params.items() if k != "iss"}
        wrong = {**params, "iss": "https://elsewhere.example/"}
        refused = client.get(f"{url.path}?{urlencode(wrong)}", follow_redirects=False)
        assert refused.status_code == 400
        assert "came from another provider" in refused.text
        # An answer naming no issuer is taken: Cliquet knows one provider only.
        taken = client.get(f"{url.path}?{urlencode(params)}", follow_redirects=False)
        assert taken.status_code == 303
    # A provider that names no issuer in its answer isn't asked to.
    quiet = realm_provider()
    app = hosted(tmp_path / "quiet", executor, quiet)
    app.state.apps.database.person_for(quiet.issuer, "auth0|sam", "sam@example.com", "sam", NOW)
    with TestClient(app, base_url="https://testserver") as client:
        start = client.get("/login", follow_redirects=False)
        back = httpx.URL(quiet.authorize(start.headers["location"], "sam"))
        params = {k: v for k, v in back.params.items() if k != "iss"}
        answer = client.get(f"{back.path}?{urlencode(params)}", follow_redirects=False)
        assert answer.status_code == 303


def test_a_refused_sign_in_says_the_provider_refused(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    app = hosted(tmp_path, executor)
    issuer = app.state.fake_provider.issuer
    with TestClient(app, base_url="https://testserver") as client:
        refused = client.get(
            f"/auth/callback?{urlencode({'error': 'access_denied', 'iss': issuer})}",
            follow_redirects=False,
        )
    assert refused.status_code == 401
    assert "The sign-in provider refused the sign-in. Try again." in refused.text


def test_sign_out_ends_the_provider_s_session(tmp_path: Path, executor: LocalExecutor) -> None:
    app = hosted(tmp_path, executor)
    location = signed_out(app, "https://example.eu.auth0.com/")
    assert location.startswith("https://example.eu.auth0.com/oauth/logout?")
    query = dict(httpx.URL(location).params)
    # No ID token: Cliquet keeps none.
    assert query == {
        "client_id": "the-client-id",
        "post_logout_redirect_uri": "https://cliquet.example/",
    }


def test_without_an_end_session_endpoint_sign_out_is_cliquet_s_only(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    app = hosted(tmp_path, executor, auth0_provider(end_session_endpoint=None))
    # A page says so: the home page would send them to the provider, which signs them in.
    assert signed_out(app, "https://example.eu.auth0.com/") == "page"


def signed_out(app, issuer: str) -> str:
    """Where signing out sends a signed-in person, once their session is gone: an address, or
    `page` when it shows them the signed-out page."""
    person = app.state.apps.database.person_for(
        issuer, "auth0|1", "etienne@example.com", "etienne", NOW
    )
    with TestClient(app, base_url="https://testserver") as client:
        act_as(app, client, person)
        token = app.state.web_auth.csrf_for_session(client.cookies.get("session"))
        response = client.post("/logout", data={"csrf": token}, follow_redirects=False)
        assert 'session=""' in response.headers["set-cookie"]
    if response.status_code == 200:
        assert "You are signed out of Cliquet." in response.text
        return "page"
    assert response.status_code in {302, 303}
    return response.headers["location"]


def test_the_callback_without_a_state_is_refused(tmp_path: Path, executor: LocalExecutor) -> None:
    app = hosted(tmp_path, executor)
    issuer = app.state.fake_provider.issuer
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get(
            f"/auth/callback?{urlencode({'code': 'not-a-code', 'iss': issuer})}",
            follow_redirects=False,
        )

    assert response.status_code in {400, 401, 403}
    assert "sign-in" in response.text.lower()


@pytest.mark.skipif(
    os.environ.get("CLIQUET_TEST_OIDC") != "1",
    reason="set CLIQUET_TEST_OIDC=1 and CLIQUET_OIDC_ISSUER to check the real provider",
)
def test_the_real_provider_signs_in_assistants(tmp_path: Path) -> None:
    settings = oidc_settings(tmp_path, os.environ["CLIQUET_OIDC_ISSUER"])
    found = anyio.run(Provider(settings).discovery)
    assert found.end_session_endpoint is not None
    assert found.userinfo_endpoint is not None
    assert found.registers_clients


def test_a_deleted_development_login_is_refused_with_what_to_do(server) -> None:
    server.state.apps.database.close_account("etienne", NOW)
    with TestClient(server) as client:
        refused = client.get("/login", follow_redirects=False)
    assert refused.status_code == 403
    assert "Set CLIQUET_DEV_LOGIN to another login" in refused.text
    assert "session" not in refused.headers.get("set-cookie", "")
