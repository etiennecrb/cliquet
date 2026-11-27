"""A person whose email isn't verified asks the console for a new verification email, which
the server asks the sign-in provider to send through the call its settings name."""

import json
import logging
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qsl

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from cliquet_server.app import create_app
from cliquet_server.db import PersonInfo, Right
from cliquet_server.executor import LocalExecutor
from cliquet_server.settings import Settings, SettingsError
from cliquet_server.sign_in import locked_out, reminder

from .server_helpers import (
    APPS,
    AUTH0_ISSUER,
    NOW,
    REALM_ISSUER,
    FakeProvider,
    act_as,
    auth0_provider,
    csrf_for,
    oidc_env,
    realm_provider,
)
from .test_server_unverified import Clock

# The call as Auth0's Management API takes it.
AUTH0_CALL = {
    "CLIQUET_VERIFICATION_EMAIL_URL": f"{AUTH0_ISSUER}api/v2/jobs/verification-email",
    "CLIQUET_VERIFICATION_EMAIL_BODY": '{"user_id": "{subject}"}',
    "CLIQUET_VERIFICATION_EMAIL_AUDIENCE": f"{AUTH0_ISSUER}api/v2/",
}
SEND = "/account/verification-email"
BUTTON = f'action="{SEND}"'
SENDING = "The sign-in provider is sending you a new email to verify your address."
ON_ITS_WAY = "A new email to verify your address is on its way."
NOT_SENT = "The sign-in provider didn&#39;t send a new email. If you verified your address"


@pytest.fixture
def clock() -> Clock:
    return Clock()


def server_with(
    tmp_path: Path,
    executor: LocalExecutor,
    clock: Clock,
    call: dict[str, str],
    provider: FakeProvider | None = None,
) -> Starlette:
    """The server signing in through `provider`, Auth0's shape by default, that sends a
    verification email again through `call`, the settings that name it, with lee, whose
    email isn't verified, and etienne, whose is."""
    provider = provider or auth0_provider()
    settings = Settings.from_env(oidc_env(tmp_path, provider.issuer) | call)
    server = create_app(settings, executor=executor, clock=clock, oidc_transport=provider.transport)
    server.state.fake_provider = provider
    database = server.state.apps.database
    database.person_for(provider.issuer, "auth0|lee", None, "lee", NOW)
    database.person_for(provider.issuer, "auth0|owner", "etienne@example.com", "etienne", NOW)
    return server


@pytest.fixture
def server(tmp_path: Path, executor: LocalExecutor, clock: Clock) -> Starlette:
    return server_with(tmp_path, executor, clock, AUTH0_CALL)


def person(server: Starlette, login: str) -> PersonInfo:
    found = server.state.apps.database.person_by_login(login)
    assert found is not None
    return found


def ask(server: Starlette, client: TestClient, next: str = "/") -> str:
    """Press the button as the client's person, and return where it sends them."""
    data = {"csrf": csrf_for(server, client), "next": next}
    answer = client.post(SEND, data=data, follow_redirects=False)
    assert answer.status_code == 303, answer.text
    return answer.headers["location"]


def server_token_requests(provider: FakeProvider) -> list[dict[str, str]]:
    """The form of each request the server made for a token of its own."""
    forms = [dict(parse_qsl(sent.content.decode())) for sent in provider.requests]
    return [form for form in forms if form.get("grant_type") == "client_credentials"]


def test_an_unverified_person_gets_a_new_email_from_the_console(
    server: Starlette, clock: Clock
) -> None:
    provider = server.state.fake_provider
    settings = server.state.apps.settings
    with TestClient(server) as client:
        act_as(server, client, person(server, "lee"))
        home = client.get("/")
        # The reminder carries the button, and doesn't send them to their account page too.
        assert home.status_code == 200 and BUTTON in home.text
        assert reminder(settings, 7, lost_email=False).replace("'", "&#39;") in home.text
        assert "ask for a new one on your account page" not in home.text

        assert ask(server, client, "/tokens") == "/tokens"
        (sent,) = provider.emails
        assert sent.method == "POST"
        assert str(sent.url) == f"{AUTH0_ISSUER}api/v2/jobs/verification-email"
        assert json.loads(sent.content) == {"user_id": "auth0|lee"}
        (form,) = server_token_requests(provider)
        assert form["audience"] == f"{AUTH0_ISSUER}api/v2/"
        page = client.get("/tokens")
        assert SENDING in page.text and "then sign in again at" in page.text
        # Told once.
        assert SENDING not in client.get("/tokens").text

        # A second ask, a moment later, sends nothing.
        ask(server, client)
        assert len(provider.emails) == 1 and ON_ITS_WAY in client.get("/").text

        # Five minutes later, it sends another, with the same token.
        clock.now = NOW + timedelta(minutes=5)
        ask(server, client)
        assert len(provider.emails) == 2 and len(server_token_requests(provider)) == 1


def test_a_keycloak_realm_sends_it_by_the_user_s_address(
    tmp_path: Path, executor: LocalExecutor, clock: Clock
) -> None:
    provider = realm_provider()
    call = {
        "CLIQUET_VERIFICATION_EMAIL_URL": (
            "https://login.example.org/admin/realms/cliquet/users/{subject}/send-verify-email"
        ),
        "CLIQUET_VERIFICATION_EMAIL_METHOD": "put",
    }
    server = server_with(tmp_path, executor, clock, call, provider)
    assert server.state.apps.settings.verification_email_method == "PUT"
    with TestClient(server) as client:
        act_as(server, client, person(server, "lee"))
        ask(server, client)
    (sent,) = provider.emails
    assert sent.method == "PUT" and sent.content == b""
    # The subject is one segment of the address, whatever it holds.
    assert sent.url.raw_path.decode().startswith(
        "/admin/realms/cliquet/users/auth0%7Clee/send-verify-email"
    )
    (form,) = server_token_requests(provider)
    assert "audience" not in form and form["grant_type"] == "client_credentials"
    assert provider.issuer == REALM_ISSUER


def test_a_refused_call_is_told_and_logged_and_may_be_asked_again(
    server: Starlette, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    provider = server.state.fake_provider
    provider.email_status = 403
    with TestClient(server) as client:
        act_as(server, client, person(server, "lee"))
        with caplog.at_level(logging.WARNING):
            ask(server, client)
        assert NOT_SENT in client.get("/").text
        assert "answered 403" in caplog.text and provider.server_tokens[0] not in caplog.text

        # A refusal doesn't count: the next ask is sent at once.
        provider.email_status = 201
        ask(server, client)
        assert SENDING in client.get("/").text and len(provider.emails) == 2

        # A token the provider no longer takes is taken again at the next ask.
        provider.server_tokens.clear()
        clock.now = NOW + timedelta(minutes=5)
        with caplog.at_level(logging.WARNING):
            ask(server, client)
        assert "answered 401" in caplog.text and len(provider.emails) == 2
        ask(server, client)
        assert len(provider.server_tokens) == 1 and len(provider.emails) == 3


def test_the_button_on_a_page_a_form_drew_comes_back_to_its_section(
    tmp_path: Path, executor: LocalExecutor, server: Starlette
) -> None:
    apps = server.state.apps
    apps.database.grant("lee", Right.BUILD)
    apps.import_folder(APPS / "stock", "lee", "shop")
    with TestClient(server) as client:
        act_as(server, client, person(server, "lee"))
        form = {"email": "sam@example.com", "role": "staff", "csrf": csrf_for(server, client)}
        # Sharing is refused until the email is verified: the page comes from the POST.
        refused = client.post("/manage/lee/shop/share", data=form)
        assert "you can&#39;t share an app" in refused.text
        assert 'name="next" value="/manage/lee/shop"' in refused.text
        assert 'name="next" value="/manage/lee/shop/share"' not in refused.text


def test_a_provider_that_gives_no_token_sends_nothing(
    server: Starlette, caplog: pytest.LogCaptureFixture
) -> None:
    provider = server.state.fake_provider
    provider.document["token_endpoint_auth_methods_supported"] = []
    with TestClient(server) as client:
        act_as(server, client, person(server, "lee"))
        with caplog.at_level(logging.WARNING):
            ask(server, client)
        assert NOT_SENT in client.get("/").text
    assert provider.emails == [] and "client credentials grant" in caplog.text


def test_a_locked_out_person_asks_from_their_account_page(server: Starlette, clock: Clock) -> None:
    provider = server.state.fake_provider
    clock.now = NOW + timedelta(days=7)
    lee = person(server, "lee")
    refusal = locked_out(server.state.apps.settings, lee, clock.now)
    assert refusal is not None
    assert refusal.endswith("a new one on your account page, at http://127.0.0.1:8800/account.")
    with TestClient(server) as client:
        act_as(server, client, lee)
        assert client.get("/").status_code == 403
        account = client.get("/account")
        assert account.status_code == 200 and BUTTON in account.text
        assert "ask for a new one on your account page" not in account.text
        # A page they may no longer see sends them to their account page.
        assert ask(server, client, "/") == "/account"
        assert SENDING in client.get("/account").text
    assert len(provider.emails) == 1


def test_only_an_unverified_person_the_provider_knows_is_offered_one(
    tmp_path: Path, executor: LocalExecutor, server: Starlette, clock: Clock
) -> None:
    provider = server.state.fake_provider
    emails = server.state.web_auth.emails
    with TestClient(server) as client:
        act_as(server, client, person(server, "etienne"))
        assert BUTTON not in client.get("/").text
        ask(server, client)
        assert "Your email address is verified already." in client.get("/").text

        # The form needs the session's form token.
        act_as(server, client, person(server, "lee"))
        refused = client.post(SEND, data={"csrf": "wrong"}, follow_redirects=False)
        assert refused.status_code == 403
    assert provider.emails == []
    # Nor someone another issuer signed in, or with no subject, as an imported owner.
    lee = person(server, "lee")
    assert emails.offered(lee)
    assert not emails.offered(replace(lee, issuer="https://other.example.com/"))
    assert not emails.offered(replace(lee, subject=None))

    # Without the call, the reminder has no button, and the form isn't there.
    plain = server_with(tmp_path / "plain", executor, clock, {})
    with TestClient(plain) as client:
        act_as(plain, client, person(plain, "lee"))
        home = client.get("/")
        assert "you have 7 days left to verify it" in home.text and BUTTON not in home.text
        data = {"csrf": csrf_for(plain, client)}
        assert client.post(SEND, data=data).status_code == 404


def test_the_assistant_is_told_where_to_ask_for_a_new_email(
    server: Starlette, tmp_path: Path
) -> None:
    settings = server.state.apps.settings
    pointer = "If you lost the email, ask for a new one on your account page, at "
    assert reminder(settings, 3).endswith(f"{pointer}http://127.0.0.1:8800/account.")
    plain = Settings.from_env(oidc_env(tmp_path / "plain"))
    assert pointer not in reminder(plain, 3)
    lee = person(server, "lee")
    later = NOW + timedelta(days=7)
    assert pointer not in (locked_out(plain, lee, later) or "")


@pytest.mark.parametrize(
    ("overrides", "refusal"),
    [
        (
            {"CLIQUET_VERIFICATION_EMAIL_URL": "https://example.eu.auth0.com/api/v2/jobs"},
            "names nobody: put {subject} in it",
        ),
        (
            {"CLIQUET_VERIFICATION_EMAIL_URL": "http://example.com/{subject}"},
            "CLIQUET_VERIFICATION_EMAIL_URL: Value error, must be the https:// address",
        ),
        (
            {**AUTH0_CALL, "CLIQUET_VERIFICATION_EMAIL_BODY": '["{subject}"]'},
            "CLIQUET_VERIFICATION_EMAIL_BODY: Value error, must be a JSON object",
        ),
        (
            {**AUTH0_CALL, "CLIQUET_VERIFICATION_EMAIL_METHOD": "DELETE"},
            "CLIQUET_VERIFICATION_EMAIL_METHOD",
        ),
        (
            {"CLIQUET_VERIFICATION_EMAIL_METHOD": "PUT"},
            "CLIQUET_VERIFICATION_EMAIL_METHOD needs CLIQUET_VERIFICATION_EMAIL_URL",
        ),
        (
            {"CLIQUET_VERIFICATION_EMAIL_AUDIENCE": f"{AUTH0_ISSUER}api/v2/"},
            "CLIQUET_VERIFICATION_EMAIL_AUDIENCE needs CLIQUET_VERIFICATION_EMAIL_URL",
        ),
    ],
)
def test_the_call_s_settings_are_checked(
    tmp_path: Path, overrides: dict[str, str], refusal: str
) -> None:
    with pytest.raises(SettingsError) as raised:
        Settings.from_env(oidc_env(tmp_path, **overrides))
    assert refusal in str(raised.value)


def test_the_call_needs_a_sign_in_provider(tmp_path: Path) -> None:
    env = {
        "CLIQUET_DATA_DIR": str(tmp_path / "cliquet-data"),
        "CLIQUET_DEV_LOGIN": "dev",
        **AUTH0_CALL,
    }
    with pytest.raises(SettingsError, match="needs CLIQUET_AUTH=oidc"):
        Settings.from_env(env)
