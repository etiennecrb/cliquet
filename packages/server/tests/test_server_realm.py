"""Keycloak's cliquet realm, which docker/compose.yaml imports: it fits the server's sign-in."""

import json
import re
from pathlib import Path

import httpx
from starlette.testclient import TestClient

from cliquet_server.executor import LocalExecutor
from cliquet_server.oidc import Provider

from .server_helpers import (
    CLIENT_SECRET,
    NOW,
    REALM_ISSUER,
    act_as,
    oidc_server,
    oidc_settings,
    realm_provider,
)

DOCKER = Path(__file__).parents[3] / "docker"
BASE_URL = "https://cliquet.example"
# What compose.yaml passes Keycloak for the realm's placeholders.
PLACEHOLDERS = {"CLIQUET_BASE_URL": BASE_URL, "CLIQUET_OIDC_CLIENT_SECRET": CLIENT_SECRET}
POLICY = "org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy"


def realm() -> dict:
    """The realm as Keycloak imports it, with each placeholder replaced by its variable."""
    text = (DOCKER / "cliquet-realm.json").read_text()
    return json.loads(re.sub(r"\$\{(\w+)\}", lambda found: PLACEHOLDERS[found[1]], text))


def client() -> dict:
    """The realm's client for the server's web sign-in."""
    (found,) = [each for each in realm()["clients"] if each["clientId"] == "cliquet"]
    return found


def test_the_realm_uses_only_the_variables_compose_passes_keycloak() -> None:
    text = (DOCKER / "cliquet-realm.json").read_text()
    assert set(re.findall(r"\$\{([^}]*)\}", text)) == set(PLACEHOLDERS)
    compose = (DOCKER / "compose.yaml").read_text()
    keycloak = compose.split("\n  keycloak:\n")[1].split("\n\n")[0]
    for name in PLACEHOLDERS:
        assert f"{name}: ${{{name}:-" in keycloak


def test_the_client_takes_the_server_s_sign_in_and_sign_out(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    app = oidc_server(
        tmp_path,
        executor,
        realm_provider(),
        CLIQUET_BASE_URL=BASE_URL,
        CLIQUET_OIDC_CLIENT_ID="cliquet",
        CLIQUET_ALLOW_REMOTE_LOCAL_EXECUTOR="1",
    )
    issuer = app.state.fake_provider.issuer
    person = app.state.apps.database.person_for(issuer, "1", "sam@example.com", "sam", NOW)
    with TestClient(app, base_url="https://testserver") as browser:
        login = httpx.URL(browser.get("/login", follow_redirects=False).headers["location"])
        act_as(app, browser, person)
        csrf = app.state.web_auth.csrf_for_session(browser.cookies.get("session"))
        logout = browser.post("/logout", data={"csrf": csrf}, follow_redirects=False)
    sign_out = httpx.URL(logout.headers["location"])

    found = client()
    assert found["enabled"] and not found["publicClient"]
    assert found["secret"] == CLIENT_SECRET
    assert found["redirectUris"] == [login.params["redirect_uri"]]
    attributes = found["attributes"]
    assert attributes["post.logout.redirect.uris"] == sign_out.params["post_logout_redirect_uri"]
    assert attributes["pkce.code.challenge.method"] == login.params["code_challenge_method"]
    assert found["standardFlowEnabled"]
    assert not found["directAccessGrantsEnabled"] and not found["serviceAccountsEnabled"]


def test_every_client_s_access_tokens_name_the_mcp_server_url(tmp_path: Path) -> None:
    settings = oidc_settings(
        tmp_path, REALM_ISSUER, CLIQUET_BASE_URL=BASE_URL, CLIQUET_ALLOW_REMOTE_LOCAL_EXECUTOR="1"
    )
    audience = Provider(settings).audience
    loaded = realm()
    # Keycloak's own scopes, basic and email among them, put the subject and the verified email
    # in the access tokens.
    assert loaded["attributes"]["CreateDefaultClientScopes"] == "true"
    (scope,) = loaded["clientScopes"]
    assert scope["name"] in loaded["defaultDefaultClientScopes"]
    (mapper,) = scope["protocolMappers"]
    assert mapper["protocolMapper"] == "oidc-audience-mapper"
    assert mapper["config"]["included.custom.audience"] == audience
    assert mapper["config"]["access.token.claim"] == "true"


def test_only_claude_ai_registers_without_an_account() -> None:
    loaded = realm()
    assert loaded["registrationAllowed"] is False
    assert loaded["verifyEmail"] is False
    anonymous = {
        policy["providerId"]: policy["config"]
        for policy in loaded["components"][POLICY]
        if policy["subType"] == "anonymous"
    }
    assert anonymous["trusted-hosts"] == {
        "trusted-hosts": ["claude.ai"],
        "host-sending-registration-request-must-match": ["false"],
        "client-uris-must-match": ["true"],
    }
    # A client may register for the scope openid, which is no client scope in Keycloak.
    assert anonymous["allowed-client-templates"]["allowed-client-scopes"] == ["openid"]
    assert "consent-required" in anonymous
    assert "scope" in anonymous


def test_the_example_settings_name_the_realm_s_issuer() -> None:
    example = (DOCKER / ".env.example").read_text()
    host = re.search(r"^LOGIN_HOST=(\S+)$", example, re.MULTILINE)
    issuer = re.search(r"^#\s+CLIQUET_OIDC_ISSUER=(\S+)$", example, re.MULTILINE)
    client_id = re.search(r"^#\s+CLIQUET_OIDC_CLIENT_ID=(\S+)$", example, re.MULTILINE)
    assert host and issuer and client_id
    assert issuer[1] == f"https://{host[1]}/realms/{realm()['realm']}"
    assert client_id[1] == client()["clientId"]
