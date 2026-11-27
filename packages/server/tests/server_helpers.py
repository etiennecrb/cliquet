import asyncio
import base64
import contextlib
import functools
import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
from collections.abc import AsyncGenerator, Callable, Generator, Sequence
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode

import anyio
import httpx
import httpx2
import mcp_types as types
import pytest
import uvicorn
from fastmcp import Client as BuilderClient
from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import ECKey, RSAKey
from mcp import Client
from mcp.client import ClientExtension
from mcp.client._transport import TransportStreams
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.testclient import TestClient

from cliquet import screenshot
from cliquet.context import Caller
from cliquet.runtime import Runtime
from cliquet.sandbox import bundle_of, version_id
from cliquet.scaffold import sdk_files, set_sdk_source, write_template
from cliquet_server.app import create_app
from cliquet_server.apps import Apps
from cliquet_server.builder import Builder
from cliquet_server.builder.drafts import Drafts
from cliquet_server.db import Database, PersonInfo, TokenLimit
from cliquet_server.endpoints import Registry
from cliquet_server.engine import SandboxEngine
from cliquet_server.executor import Executor, SandboxError
from cliquet_server.limiter import Limits
from cliquet_server.me import SHOW, Me, answered, make_me
from cliquet_server.repos import AppRepo
from cliquet_server.settings import Settings

# Whole apps the tests run, and the examples agents read.
APPS = Path(__file__).parents[2] / "sdk" / "tests" / "apps"
EXAMPLES = Path(__file__).parents[3] / "examples"
SDK = Path(__file__).parents[3] / "packages" / "sdk"


def sdk_copy(target: Path, changed: str | None = None) -> Path:
    """A copy of the SDK in `target`, with a blank line added to the file `changed`."""
    for name, content in sdk_files(SDK).items():
        if name == changed:
            content += b"\n"
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        (target / name).write_bytes(content)
    return target


def unexpected(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The records that log a failure of Cliquet's own: at ERROR, on one of Cliquet's
    loggers, starting with `unexpected: `."""
    return [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and record.name.split(".")[0] in ("cliquet", "cliquet_server")
        and record.getMessage().startswith("unexpected: ")
    ]


def one_unexpected(
    caplog: pytest.LogCaptureFixture, exc_type: type[BaseException] | None = None
) -> logging.LogRecord:
    """The one record that logs a failure of Cliquet's own, with its traceback: of an
    `exc_type`, when given."""
    (record,) = unexpected(caplog)
    assert record.exc_info and record.exc_info[1] is not None
    assert exc_type is None or isinstance(record.exc_info[1], exc_type)
    return record


def example_code(app: Path = APPS / "stock") -> dict[str, bytes]:
    """An app's code as the server keeps it: no lock file, and no path to the SDK."""
    files = bundle_of(app)
    files.pop("app.py.lock", None)
    files["app.py"] = set_sdk_source(files["app.py"].decode(), None).encode()
    return files


def example_data(app: Path = APPS / "stock") -> dict[str, bytes]:
    """An app's data files, by name."""
    data = app / "data"
    return {path.name: path.read_bytes() for path in sorted(data.iterdir()) if path.is_file()}


def stock_quantity(result: dict, sku: str) -> int:
    """The quantity of `sku` in the stock example's `stock` answer, as `try_call` returns it."""
    data = result["result"]
    if isinstance(data, dict) and "result" in data:
        data = data["result"]
    return next(row["quantity"] for row in data if row["sku"] == sku)


def draft_files(drafts: Drafts, login: str, app: str) -> dict[str, bytes]:
    """Every file of a draft, by path, leaving out Python's caches. The builder's tools read
    only what they need."""
    folder = drafts.folder(login, app)
    paths = sorted(folder.rglob("*")) if folder.is_dir() else []
    return {
        path.relative_to(folder).as_posix(): path.read_bytes()
        for path in paths
        if path.is_file() and "__pycache__" not in path.relative_to(folder).parts
    }


def draft_example(
    builder: Builder, name: str, app: Path = APPS / "stock", login: str = "etienne"
) -> None:
    """A new draft named `name` that holds an app's code, tests and data, as an assistant
    writes it."""
    builder.create_app(login, name)
    data = {f"data/{file}": content for file, content in example_data(app).items()}
    builder.drafts.start(login, name, {**example_code(app), **data})


NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
OWNER = Caller("sam", "owner")
STAFF = Caller("till", "staff")


def published_with_icon(server: Starlette, icon: str = "leaf") -> None:
    """Publish the stock example as etienne/shop again, naming an icon and keeping its accent."""
    apps = server.state.apps
    code = apps.repo("etienne", "shop").code()
    source = code["app.py"].replace(b'    icon="leaf",\n', b"").replace(b'    icon="star",\n', b"")
    named = b'    accent="#0f766e",\n    icon="' + icon.encode() + b'",\n'
    code["app.py"] = source.replace(b'    accent="#0f766e",\n', named)
    if code["app.py"] == source:
        raise AssertionError("the stock example no longer names its accent where this edits it")
    apps.publish("etienne", "shop", code, note=f"With the {icon} icon")


def with_accent(server: Starlette, accent: str) -> None:
    """Publish etienne/shop again with another accent in place of the stock example's."""
    apps = server.state.apps
    code = apps.repo("etienne", "shop").code()
    changed = code["app.py"].replace(b'accent="#0f766e"', f'accent="{accent}"'.encode())
    assert changed != code["app.py"]
    apps.publish("etienne", "shop", {**code, "app.py": changed}, note="Another accent")


def served_example(
    folder: Path,
    executor: Executor,
    app: Path = APPS / "stock",
    clock: Callable[[], datetime] = lambda: NOW,
) -> Runtime:
    """An app published in a repository in `folder`, served through the sandbox, with the
    server's clock at `clock`."""
    code = example_code(app)
    version = version_id(code)
    client = executor.sandbox("etienne")
    built = client.put_version("etienne", app.name, version, code)
    repo = AppRepo(folder)
    data = {f"data/{key}": content for key, content in example_data(app).items()}
    repo.publish(
        None,
        {**code, "app.py.lock": built.lock, **data},
        version=version,
        note="test",
        login="etienne",
        when=NOW,
    )
    return Runtime(
        folder,
        engine=SandboxEngine(client, "etienne", app.name, version, built.manifest, repo.code),
        clock=clock,
        # The callers of these tests are OWNER, so they take the owner's path, as on a live app.
        owner=OWNER.id,
    )


@contextlib.contextmanager
def running(app: Starlette) -> Generator[str]:
    """Serve `app` on a free local port for the length of the block, and give its address."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class Watched:
    """A draft's lock that sets `waiting` when a thread has to wait for it."""

    def __init__(self, waiting: threading.Event) -> None:
        self._lock = threading.RLock()
        self.waiting = waiting

    def __enter__(self) -> None:
        if not self._lock.acquire(blocking=False):
            self.waiting.set()
            self._lock.acquire()

    def __exit__(self, *_: object) -> None:
        self._lock.release()


class BlockedBuild:
    """The sandbox's builds of one app, held until `release`, and counted."""

    def __init__(self) -> None:
        self.building = threading.Event()
        self.released = threading.Event()
        self.builds = 0
        # The builds held and not returned yet.
        self.running = 0
        self.returned = threading.Condition()

    def release(self) -> None:
        self.released.set()


@contextlib.contextmanager
def blocked_build(
    apps: Apps, name: str, monkeypatch: pytest.MonkeyPatch
) -> Generator[BlockedBuild]:
    """Hold every build of the app `name` in the sandbox until the block ends or `release`.

    A held build waits for at most a minute, so that a failing test never hangs. The block
    ends once every build it held returned, so no start outlives its test.
    """
    blocked = BlockedBuild()
    client = apps.executor.sandbox("etienne")
    real = type(client).put_version

    def put_version(self, login: str, app: str, version: str, files: dict[str, bytes]):
        if app != name:
            return real(self, login, app, version, files)
        with blocked.returned:
            blocked.builds += 1
            blocked.running += 1
        try:
            blocked.building.set()
            assert blocked.released.wait(timeout=60), "the test never released the build"
            return real(self, login, app, version, files)
        finally:
            with blocked.returned:
                blocked.running -= 1
                blocked.returned.notify_all()

    monkeypatch.setattr(type(client), "put_version", put_version)
    try:
        yield blocked
    finally:
        blocked.release()
        with blocked.returned:
            assert blocked.returned.wait_for(lambda: blocked.running == 0, timeout=60)


@contextlib.contextmanager
def failing_build(
    apps: Apps,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    error: SandboxError | None = None,
) -> Generator[list[str]]:
    """Make every build of the app `name` fail, as an unreachable sandbox does, or with
    `error`, until the block ends. It gives the versions it failed."""
    client = apps.executor.sandbox("etienne")
    real = type(client).put_version
    failed: list[str] = []
    failing = True

    def put_version(self, login: str, app: str, version: str, files: dict[str, bytes]):
        if failing and app == name:
            failed.append(version)
            raise error or SandboxError("the sandbox is away")
        return real(self, login, app, version, files)

    monkeypatch.setattr(type(client), "put_version", put_version)
    try:
        yield failed
    finally:
        failing = False


def published_before_roles_were_kept(database: Database, login: str, name: str) -> None:
    """Drop the roles kept for the app, as for one published before they were kept."""
    path = database._engine.url.database
    assert path is not None
    with contextlib.closing(sqlite3.connect(path)) as raw, raw:
        raw.execute(
            "delete from app_roles where app_id = (select apps.id from apps join people "
            "on people.id = apps.owner_id where people.login = ? and apps.name = ?)",
            (login, name),
        )


def text(answer: types.CallToolResult) -> str:
    """The text of a tool's answer: its first part."""
    (content, *_) = answer.content
    assert isinstance(content, types.TextContent)
    return content.text


def texts(answer: types.CallToolResult) -> list[str]:
    """The text of each part of a tool's answer: for a query, the app's result, then Cliquet's
    own words on the version of the data."""
    assert all(isinstance(content, types.TextContent) for content in answer.content)
    return [content.text for content in answer.content if isinstance(content, types.TextContent)]


def call(
    server: Starlette, login: str, name: str, arguments: dict[str, Any], draws: bool = False
) -> types.CallToolResult:
    """Call one of the person's tools on /me as `login`, as the endpoint's thin tool does."""
    state = server.state
    me = Me(state.apps, state.registry, state.settings.base_url, state.builder)
    person = state.apps.database.person_by_login(login)
    extra = {"draws_screens": draws} if name == SHOW else {}
    return anyio.run(lambda: answered(name, getattr(me, name)(person, **arguments, **extra)))


def me_server(
    apps: Apps,
    builder: Builder | None = None,
    *,
    auth: AuthProvider | None = None,
    limits: Limits | None = None,
) -> FastMCP:
    """The person's endpoint with the builder's tools, as the server makes it, for a client in
    memory: with no token, it is the development login, who builds."""
    builder = builder or Builder(apps, apps.settings)
    registry = Registry(apps, limits)
    return make_me(apps, registry, apps.settings, auth=auth, limits=limits, builder=builder)


def tools(server: Starlette, *made: tuple[str, dict[str, Any]]) -> list[types.CallToolResult]:
    """Call the person's endpoint's tools in turn, in memory, as etienne, the shop's owner,
    who builds: each answer, a refusal too."""

    async def main() -> list[types.CallToolResult]:
        async with BuilderClient(me_server(server.state.apps)) as client:
            return [
                await client.call_tool(name, arguments, raise_on_error=False)
                for name, arguments in made
            ]

    return anyio.run(main)


def app_status(address: str, token: str, app: str = "etienne/shop") -> int:
    """The status of a request with `token` at the app's own address."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    return httpx.post(f"{address}/apps/{app}/mcp", json={}, headers=headers).status_code


def template_code(folder: Path, name: str = "list") -> dict[str, bytes]:
    """The code of the small app `cliquet new` writes: one action, one rule, no roles."""
    write_template(folder, name)
    return bundle_of(folder)


AUTH0_ISSUER = "https://example.eu.auth0.com/"
# A second provider, whose issuer has a path and no final slash, as Keycloak's realms do.
REALM_ISSUER = "https://login.example.org/realms/cliquet"
SECRET_KEY = "a-secret-key-that-is-at-least-32-characters"
CLIENT_ID = "the-client-id"
CLIENT_SECRET = "the-client-secret"
# The ends of the addresses of Auth0's and Keycloak's calls that send a verification email
# again, which the faked providers answer.
EMAIL_CALLS = ("/api/v2/jobs/verification-email", "/send-verify-email")
ME = "http://127.0.0.1:8800/me/mcp"

# The people the faked providers sign in, by name: their subject, email and whether it is
# verified. Two share the local part sam.
PEOPLE: dict[str, tuple[str, str | None, bool]] = {
    "etienne": ("auth0|etienne", "Etienne@Example.com", True),
    "sam": ("auth0|sam", "sam@example.com", True),
    "owner": ("auth0|owner", "etienne@example.com", True),
    "other": ("auth0|other", "other@example.com", True),
    "sam-a": ("auth0|sam-a", "sam@a.example", True),
    "sam-b": ("auth0|sam-b", "sam@b.example", True),
    "imported": ("auth0|imp", "pat@example.com", True),
    "earlier": ("auth0|old", "kim@example.com", True),
    "ann": ("auth0|ann", "ann@example.com", True),
    "kim": ("auth0|kim", "kim@example.com", True),
    "a": ("auth0|a", "A@Example.com", True),
    "other-a": ("auth0|other-a", "a@other.example", True),
    "collision": ("auth0|collision", "etienne@other.example.com", True),
    "unverified": ("auth0|unverified", "etienne@example.com", False),
    "email-less": ("auth0|email-less", None, False),
    "lee": ("auth0|lee", "lee@example.com", False),
}


@functools.cache
def _signing_key(kind: str) -> RSAKey | ECKey:
    """A key the faked provider signs with, made once per run, since an RSA key is slow to
    make."""
    if kind == "RSA":
        return RSAKey.generate_key(2048, parameters={"kid": "rsa-1"}, private=True)
    return ECKey.generate_key("P-256", parameters={"kid": "ec-1"}, private=True)


class FakeProvider:
    """An OpenID Connect provider in place of a real one: its discovery document, keys, token,
    userinfo and authorization endpoints, answered through `transport` with no network, and
    the access tokens it signs. `email_in_tokens` puts the person's email in their access
    tokens, as some providers do and Auth0 does only with an Action."""

    def __init__(
        self,
        issuer: str,
        endpoints: str,
        *,
        algorithm: str = "RS256",
        email_in_tokens: bool = False,
        document: dict[str, object] | None = None,
    ) -> None:
        self.issuer = issuer
        self.algorithm = algorithm
        self.key = _signing_key("RSA" if algorithm.startswith(("RS", "PS")) else "EC")
        self.email_in_tokens = email_in_tokens
        base = issuer.rstrip("/") + endpoints
        self.document: dict[str, object] = {
            "issuer": issuer,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "userinfo_endpoint": f"{base}/userinfo",
            "jwks_uri": f"{base}/jwks",
            "end_session_endpoint": f"{base}/logout",
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
        }
        for name, value in (document or {}).items():
            if value is None:
                self.document.pop(name, None)
            else:
                self.document[name] = value
        self.transport = httpx.MockTransport(self._answer)
        # Whether the provider is unreachable, every request the server sent, and the PKCE
        # challenge of each code given out.
        self.down = False
        # The subject the userinfo endpoint names in place of the token's, if it lies.
        self.userinfo_subject: str | None = None
        # The people it signs in, as `PEOPLE` has them: a test verifies one's email here.
        self.people = dict(PEOPLE)
        self.requests: list[httpx.Request] = []
        self._challenges: dict[str, str] = {}
        # The server's own tokens it gave out by the client credentials grant, the
        # verification emails it was asked to send, and what it answers each such request.
        self.server_tokens: list[str] = []
        self.emails: list[httpx.Request] = []
        self.email_status = 201

    def token(self, who: str, audience: str | list[str] = ME, **claims: object) -> str:
        """An access token for `who`, signed by this provider for `audience`."""
        subject, email, verified = self.people[who]
        payload: dict[str, object] = {
            "iss": self.issuer,
            "sub": subject,
            "aud": audience,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "scope": "openid email offline_access",
        }
        if self.email_in_tokens and email is not None:
            payload |= {"email": email, "email_verified": verified}
        payload |= claims
        header = {"alg": self.algorithm, "kid": self.key.kid}
        return jwt.encode(header, payload, self.key, algorithms=[self.algorithm])

    def authorize(self, location: str, who: str) -> str:
        """What a browser sent to the authorization endpoint at `location` comes back to: the
        callback with a code for `who`, the state and the issuer."""
        url = httpx.URL(location)
        assert str(url.copy_with(query=None)) == self.document["authorization_endpoint"]
        query = dict(url.params)
        assert query["client_id"] == CLIENT_ID and query["code_challenge_method"] == "S256"
        code = f"code-{who}-{len(self._challenges)}"
        self._challenges[code] = query["code_challenge"]
        back = {"code": code, "state": query["state"], "iss": self.issuer}
        return f"{httpx.URL(query['redirect_uri']).path}?{urlencode(back)}"

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("the provider is unreachable", request=request)
        url = str(request.url.copy_with(query=None))
        if url == self.issuer.rstrip("/") + "/.well-known/openid-configuration":
            return httpx.Response(200, json=self.document)
        if url == self.document.get("jwks_uri"):
            return httpx.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        if url == self.document.get("token_endpoint") and request.method == "POST":
            return self._token_answer(request)
        if url == self.document.get("userinfo_endpoint"):
            return self._userinfo(request)
        if url.endswith(EMAIL_CALLS):
            return self._email(request)
        return httpx.Response(404)

    def _email(self, request: httpx.Request) -> httpx.Response:
        """Auth0's or Keycloak's call that sends a verification email again: it takes one of
        the server's own tokens, and answers `email_status`."""
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        if bearer not in self.server_tokens:
            return httpx.Response(401, json={"error": "invalid_token"})
        self.emails.append(request)
        return httpx.Response(self.email_status, json={"status": "pending"})

    def _userinfo(self, request: httpx.Request) -> httpx.Response:
        """The userinfo endpoint: who a code's opaque token names, or one of this provider's
        access tokens with the `openid` scope, as Auth0 refuses one without it."""
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        if bearer.startswith("opaque-"):
            who = bearer.removeprefix("opaque-")
        else:
            try:
                claims = jwt.decode(bearer, self.key, algorithms=[self.algorithm]).claims
            except JoseError, ValueError:
                return httpx.Response(401, json={"error": "invalid_token"})
            if "openid" not in str(claims.get("scope", "")).split():
                return httpx.Response(401, json={"error": "insufficient_scope"})
            subjects = {person[0]: name for name, person in self.people.items()}
            who = subjects.get(str(claims.get("sub")), "")
        if who not in self.people:
            return httpx.Response(401, json={"error": "invalid_token"})
        subject, email, verified = self.people[who]
        info = {"sub": self.userinfo_subject or subject, "email": email, "email_verified": verified}
        return httpx.Response(200, json={k: v for k, v in info.items() if v is not None})

    def _token_answer(self, request: httpx.Request) -> httpx.Response:
        """The token endpoint: a code given out by `authorize`, its PKCE verifier and the
        client's secret, sent the way the discovery document lists."""
        form = dict(parse_qsl(request.content.decode()))
        methods = self.document.get("token_endpoint_auth_methods_supported") or []
        assert isinstance(methods, list)
        basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if request.headers.get("authorization") == f"Basic {basic}":
            authenticated = "client_secret_basic" in methods
        else:
            in_form = form.get("client_id") == CLIENT_ID
            in_form = in_form and form.get("client_secret") == CLIENT_SECRET
            authenticated = in_form and "client_secret_post" in methods
        if form.get("grant_type") == "client_credentials":
            if not authenticated:
                return httpx.Response(401, json={"error": "invalid_client"})
            self.server_tokens.append(f"server-{len(self.server_tokens)}")
            return httpx.Response(
                200, json={"access_token": self.server_tokens[-1], "expires_in": 86400}
            )
        challenge = self._challenges.pop(form.get("code", ""), None)
        verifier = form.get("code_verifier", "")
        digest = hashlib.sha256(verifier.encode()).digest()
        proved = base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == challenge
        if not authenticated or not proved or "audience" in form or "resource" in form:
            return httpx.Response(400, json={"error": "invalid_grant"})
        who = form["code"].split("-")[1:-1]
        return httpx.Response(200, json={"access_token": f"opaque-{'-'.join(who)}"})


def auth0_provider(**document: object) -> FakeProvider:
    """A provider shaped as an Auth0 tenant: an issuer with a final slash, no email in its
    access tokens, and both ways of registering an MCP client."""
    shape: dict[str, object] = {
        "registration_endpoint": f"{AUTH0_ISSUER}oidc/register",
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
    }
    return FakeProvider(AUTH0_ISSUER, "/oauth", document=shape | document)


def realm_provider(**document: object) -> FakeProvider:
    """A provider shaped as a Keycloak realm: a path in its issuer, email in its access tokens,
    another kind of key, and no way of registering an MCP client."""
    return FakeProvider(
        REALM_ISSUER,
        "/protocol/openid-connect",
        algorithm="ES256",
        email_in_tokens=True,
        document=document,
    )


def oidc_env(folder: Path, issuer: str = AUTH0_ISSUER, **overrides: str) -> dict[str, str]:
    """The variables of a server whose data is in `folder`, signing in through `issuer`."""
    return {
        "CLIQUET_DATA_DIR": str(folder / "cliquet-data"),
        "CLIQUET_AUTH": "oidc",
        "CLIQUET_OIDC_ISSUER": issuer,
        "CLIQUET_OIDC_CLIENT_ID": CLIENT_ID,
        "CLIQUET_OIDC_CLIENT_SECRET": CLIENT_SECRET,
        "CLIQUET_SECRET_KEY": SECRET_KEY,
        **overrides,
    }


def oidc_settings(folder: Path, issuer: str = AUTH0_ISSUER, **overrides: str) -> Settings:
    """Settings that sign in through `issuer`, with the data in `folder`."""
    return Settings.from_env(oidc_env(folder, issuer, **overrides))


def oidc_server(
    folder: Path,
    executor: Executor,
    provider: FakeProvider | None = None,
    *,
    limits: Limits | None = None,
    clock: Callable[[], datetime] = lambda: NOW,
    **overrides: str,
) -> Starlette:
    """The server signing in through `provider`, Auth0's shape by default, with its data in
    `folder`, `limits`, the settings' by default, and its clock at `clock`. The provider is
    kept as `state.fake_provider`."""
    provider = provider or auth0_provider()
    settings = oidc_settings(folder, provider.issuer, **overrides)
    server = create_app(
        settings,
        executor=executor,
        clock=clock,
        limits=limits,
        oidc_transport=provider.transport,
    )
    server.state.fake_provider = provider
    return server


def oidc_callback(server: Starlette, client: TestClient, who: str, next: str = "/") -> str:
    """Sign in through the server's faked provider as `who`, and return where the callback
    sends them."""
    start = client.get(f"/login?next={next}", follow_redirects=False)
    assert start.status_code == 302, start.text
    back = server.state.fake_provider.authorize(start.headers["location"], who)
    answer = client.get(back, follow_redirects=False)
    assert answer.status_code == 303, answer.text
    return answer.headers["location"]


def login_form(client: TestClient) -> tuple[str, str]:
    """The login page's form token and the login it suggests."""
    page = client.get("/auth/choose-login")
    assert page.status_code == 200
    assert "in your apps' addresses and in their history" in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
    login = re.search(r'name="login" type="text" required value="([^"]*)"', page.text)
    assert csrf is not None and login is not None
    return csrf[1], login[1]


def choose_login(client: TestClient, login: str, csrf: str) -> httpx2.Response:
    """Send the login form."""
    data = {"csrf": csrf, "login": login}
    return client.post("/auth/choose-login", data=data, follow_redirects=False)


def sign_in(client: TestClient, next: str = "/") -> None:
    """Sign in through the development login."""
    response = client.get(f"/login?next={quote(next)}", follow_redirects=False)
    assert response.status_code in {302, 303, 307}, response.text


@contextlib.contextmanager
def signed_in(server: Starlette, *, base_url: str = "http://testserver") -> Generator[TestClient]:
    """A client signed in through the development login."""
    with TestClient(server, base_url=base_url) as client:
        sign_in(client)
        yield client


def act_as(server: Starlette, client: TestClient, person: PersonInfo) -> None:
    """Give a test client the signed session of one person."""
    client.cookies.set("session", server.state.web_auth.session_cookie(person))


class _Entered:
    """A transport whose context has already been entered by the helper."""

    def __init__(self, streams: TransportStreams) -> None:
        self._streams = streams

    async def __aenter__(self) -> TransportStreams:
        return self._streams

    async def __aexit__(self, *args: object) -> None:
        return None


def app_token(
    server: Starlette,
    role: str = "owner",
    app: str = "shop",
    person: PersonInfo | None = None,
    *,
    login: str = "etienne",
) -> str:
    """A token limited to `login`'s app as `role`, for `person`: by default the app's owner,
    made if the database doesn't know them yet."""
    database = server.state.apps.database
    if person is None:
        person = database.person_by_login(login) or database.person_for(
            "cliquet://dev", f"dev:{login}", f"{login}@dev.invalid", login, NOW
        )
    limit = TokenLimit(login, app, role)
    count = len(database.tokens(person.id))
    return database.create_token(person.id, f"{app} as {role} {count}", NOW, [limit])[1]


@contextlib.asynccontextmanager
async def app_client(
    address: str,
    path: str,
    token: str = "dev",
    *,
    mode: str = "auto",
    extensions: Sequence[ClientExtension] = (),
) -> AsyncGenerator[Client]:
    """An MCP client for an endpoint, with a bearer token: for an app endpoint, one limited
    to the app, from `app_token`. `mode` is how the client picks its protocol version, as
    `Client` takes it, and `extensions` are those it says it supports."""
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
        streamable_http_client(f"{address}{path}", http_client=http) as streams,
        Client(_Entered(streams), mode=mode, extensions=extensions) as client,
    ):
        yield client


async def tool_names(client: Client) -> set[str]:
    """The names of the tools a client lists."""
    return {tool.name for tool in (await client.list_tools()).tools}


def open_session(address: str, path: str, token: str) -> str:
    """The id of a session opened by hand at an endpoint, as a client before 2026-07-28 does."""
    params = {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
    response = httpx.post(f"{address}{path}", json=body, headers=_session_headers(token))
    assert response.status_code == 200, response.text
    return response.headers["mcp-session-id"]


def in_session(address: str, path: str, token: str, session_id: str) -> httpx.Response:
    """The answer to a `tools/list` sent by hand in the session `session_id`."""
    headers = {**_session_headers(token), "Mcp-Session-Id": session_id}
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    return httpx.post(f"{address}{path}", json=body, headers=headers, timeout=30)


def rpc_refusal(response: httpx.Response | httpx2.Response, status: int) -> str:
    """The message of a refusal at an app's address, as `status`: a JSON-RPC error with no
    id, as the MCP library sends its own."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/json"
    answer = response.json()
    assert (answer["jsonrpc"], answer["id"], answer["error"]["code"]) == (
        "2.0",
        None,
        types.INVALID_REQUEST,
    )
    return answer["error"]["message"]


def session_gone(response: httpx.Response) -> str:
    """The message of the 404 an endpoint gives a session it doesn't serve."""
    return rpc_refusal(response, 404)


def _session_headers(token: str) -> dict[str, str]:
    """The headers of a request sent by hand before 2026-07-28."""
    return {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}


# Code added to the stock example: a query and a view that fail with data in the message.
# The view lays out for Cliquet's own check, so the version can be published.
FAILING = b'''

@shop.query(roles=["owner", "staff"])
def secret(ctx: QueryContext) -> int:
    """Fail, with data in the message."""
    raise KeyError("Sencha costs 4.20")


@shop.view(roles=["owner", "staff"])
def broken(ctx: QueryContext) -> Screen:
    """Fail, with data in the message, except for Cliquet's own check."""
    if ctx.caller.id != "cliquet":
        raise KeyError("Sencha costs 4.20")
    return Screen("<p>ok</p>")
'''
ERROR_ID = r"e-[0-9a-f]{12}"


def member(database: Database, person: PersonInfo, login: str, app: str, role: str) -> PersonInfo:
    """Share `login`'s app with a known person as `role`, and accept it as they would."""
    assert person.email is not None
    database.share(login, app, person.email, role, NOW)
    assert role in database.accept_share(person.id, login, app, NOW)
    return person


def a_shop_member(server: Starlette, role: str | None = "owner") -> PersonInfo:
    """sam, a member of etienne/shop with `role`: by default the role named owner. With no
    role, sam is known but has no share."""
    database = server.state.apps.database
    person = database.person_for(
        "https://example.eu.auth0.com/", "auth0|sam", "sam@example.com", "sam", NOW
    )
    if role is None:
        return person
    return member(database, person, "etienne", "shop", role)


def csrf_for(server: Starlette, client: TestClient) -> str:
    """The form token of the client's signed-in session."""
    token = server.state.web_auth.csrf_for_session(client.cookies.get("session"))
    assert token is not None
    return token


def publish_failing(server: Starlette) -> None:
    """Publish the stock example as etienne/shop again, with `FAILING` added."""
    apps = server.state.apps
    code = apps.repo("etienne", "shop").code()
    code["app.py"] += FAILING
    apps.publish("etienne", "shop", code, note="Failing")


GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@localhost",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@localhost",
}


def git(folder: Path, *args: str, input: bytes | None = None) -> str:
    """Run git in `folder`, and give its output."""
    return subprocess.run(
        ["git", *args],
        cwd=folder,
        input=input,
        capture_output=True,
        check=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.decode()


def fail_first_end(monkeypatch: pytest.MonkeyPatch) -> list[Runtime]:
    """Make the next end of a runtime's transactions fail, as a full disk would, and the ones
    after it work. Returns the runtime whose end failed, once it did."""
    failed: list[Runtime] = []
    end = Runtime.end_transactions

    def end_transactions(runtime: Runtime, published: str | None = None) -> None:
        if not failed:
            failed.append(runtime)
            raise OSError("disk full")
        end(runtime, published)

    monkeypatch.setattr(Runtime, "end_transactions", end_transactions)
    return failed


# A draft's migrate.py for the template's items.csv: every item's name in capitals.
MIGRATION = '''\
import csv


def migrate() -> None:
    """Write every item's name in capitals."""
    with open("items.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row["name"] = row["name"].upper()
    with open("items.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "note"])
        writer.writeheader()
        writer.writerows(rows)
'''


def published(builder: Builder) -> None:
    """Publish the template app tasks, with milk and eggs as its first data."""
    builder.create_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "data/items.csv", "name,note\nmilk,\neggs,\n")
    assert builder.publish_app("etienne", "tasks", "First version")["published"] is True


def names(builder: Builder) -> list[str]:
    """The names of the live items of tasks."""
    live = builder.apps.live("etienne", "tasks")
    return [row["name"] for row in live.runtime.query_json(Caller("etienne", None), "items", {})]


# A test that drives a browser, skipped where there is none.
needs_browser = pytest.mark.skipif(
    screenshot.find_browser() is None, reason="needs a Chrome, Chromium or Edge"
)


def kept(server: Starlette, files: dict[str, bytes | None], label: str = "import") -> str:
    """Commit data files to etienne/shop as a change of the owner's, and give its commit.

    The console reads whatever git holds, so tests may write files no action of the stock
    example would.
    """
    store = server.state.apps.live("etienne", "shop").runtime.store
    message = f"{label}\n\nApp-Action: {label}\nApp-Caller: etienne\n"
    return store.commit(store.head(), files, message, "etienne", NOW)


def wait_for(browser: screenshot.Browser, expression: str, timeout: float = 30) -> Any:
    """The value of the JavaScript `expression` once it is truthy, polled until `timeout`."""
    deadline = time.monotonic() + timeout
    while not (value := browser.evaluate(expression)):
        assert time.monotonic() < deadline, f"{expression} wasn't true in time"
        time.sleep(0.05)
    return value


# An app that tries to reach the network, for a real sandbox's test.
# The script block's first line is joined here, so that type checkers don't read this file as one.
PROBE_APP = (
    "# /// "
    + """script
# requires-python = ">=3.14"
# dependencies = ["cliquet-sdk>=0.0.1,<0.1"]
# ///
\"\"\"An app that tries to reach the network, to measure the Sprite's policy.\"\"\"

import socket
import urllib.request

from cliquet import App, QueryContext

probe = App("probe", files=["notes.txt"])


def _try(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=8) as answer:
            return f"answered {answer.status}"
    except Exception as error:
        return f"{type(error).__name__}: {error}"


@probe.query()
def reach(ctx: QueryContext) -> dict[str, str]:
    \"\"\"Try example.com and PyPI, and say what happened.\"\"\"
    try:
        dns = str(socket.getaddrinfo("example.com", 443)[0][4][0])
    except OSError as error:
        dns = f"{type(error).__name__}: {error}"
    return {
        "dns": dns,
        "https": _try("https://example.com/"),
        "pypi": _try("https://pypi.org/simple/"),
    }
"""
)


needs_browser = pytest.mark.skipif(
    screenshot.find_browser() is None, reason="needs a Chrome or Chromium"
)

# A host for a builder's panel: it hands the frame what ask_for_file, or create_token, answered,
# and passes the frame's tool calls to the builder through the test's server, keeping each one
# with its size.
HOST = """\
<!doctype html>
<html><head><meta charset="utf-8"><title>Host</title></head>
<body>
<iframe id="frame" sandbox="allow-scripts allow-same-origin"
        style="width: 100%; height: 700px; border: 0"></iframe>
<script>
"use strict";
const frame = document.getElementById("frame");
window.calls = [];
window.contexts = [];
window.messages = [];
window.links = [];
const send = (message) => frame.contentWindow.postMessage(message, "*");
const tool = async (name, args) =>
  (await fetch("call", {method: "POST", body: JSON.stringify({name, arguments: args})})).json();
async function answer({method, params}) {
  if (method === "ui/initialize") {
    const hostCapabilities = {openLinks: {}, updateModelContext: {}};
    if (!window.noTools) hostCapabilities.serverTools = {};
    if (window.offersMessage) hostCapabilities.message = {text: {}};
    return {protocolVersion: params.protocolVersion, hostInfo: {name: "host", version: "0"},
            hostCapabilities, hostContext: {theme: "light", displayMode: "inline"}};
  }
  if (method === "tools/call") {
    const args = params.arguments || {};
    window.calls.push({tool: params.name, size: JSON.stringify(args).length,
                       offset: args.offset});
    return tool(params.name, args);
  }
  if (method === "ui/update-model-context") { window.contexts.push(params); return {}; }
  if (method === "ui/message") {
    window.messages.push(params);
    if (window.refusesMessage) throw new Error("no message now");
    return {};
  }
  if (method === "ui/open-link") { window.links.push(params.url); return {}; }
  throw new Error("no " + method);
}
window.addEventListener("message", async (event) => {
  const message = event.data;
  if (event.source !== frame.contentWindow || !message || message.jsonrpc !== "2.0") return;
  if (message.method === "ui/notifications/initialized") {
    window.answered = await tool(window.asks, window.asked);
    send({jsonrpc: "2.0", method: "ui/notifications/tool-result", params: window.answered});
  } else if (message.id !== undefined && waiting.has(message.id)) {
    waiting.get(message.id)(message);
    waiting.delete(message.id);
  } else if (message.method && message.id !== undefined) {
    try {
      send({jsonrpc: "2.0", id: message.id, result: await answer(message)});
    } catch (error) {
      send({jsonrpc: "2.0", id: message.id, error: {code: -32603, message: String(error)}});
    }
  }
});
// Hand the panel ask_for_file's answer again, as a host may when it draws the frame again,
// then wait for the panel to answer a ping: it handles messages in the order they come.
const waiting = new Map();
window.notifyAgain = () => {
  send({jsonrpc: "2.0", method: "ui/notifications/tool-result", params: window.answered});
  return new Promise((resolve) => {
    waiting.set("again", resolve);
    send({jsonrpc: "2.0", id: "again", method: "ping"});
  });
};
// Show `page`, the panel the tool `asks` names, as the host does when its model called it.
window.start = async (asked, offersMessage, asks = "ask_for_file", page = "panel.html") => {
  window.asked = asked;
  window.offersMessage = offersMessage;
  window.asks = asks;
  frame.srcdoc = await (await fetch(page)).text();
};
// Choose a file of `size` bytes in the panel, as its file chooser would, then send it.
window.choose = (size, bytes) => {
  const doc = frame.contentDocument;
  const data = new Uint8Array(size);
  if (bytes === "pattern") for (let i = 0; i < size; i++) data[i] = (i * 7 + 3) % 251;
  else data.set(new TextEncoder().encode(bytes));
  const chosen = new DataTransfer();
  chosen.items.add(new File([data], "ledger.beancount"));
  const input = doc.getElementById("file");
  input.files = chosen.files;
  input.dispatchEvent(new Event("change", {bubbles: true}));
  doc.getElementById("send").click();
};
</script>
</body></html>
"""

PANEL = "frame.contentDocument"
SAID = f"{PANEL}.getElementById('said')"
TELL = f"{PANEL}.getElementById('tell')"


@contextlib.contextmanager
def panel_host(mcp: FastMCP, pages: dict[str, bytes]) -> Generator[str]:
    """Serve `HOST` at / and the panels' `pages` on this machine, with `mcp` behind the
    frame's tool calls, for the length of the block, and give the host's address."""
    served = {"/": HOST.encode(), **pages}

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with BuilderClient(mcp) as client:
            result = await client.call_tool(name, arguments, raise_on_error=False)
        return {
            "content": [part.model_dump(exclude_none=True) for part in result.content],
            "structuredContent": result.structured_content,
            "isError": result.is_error,
        }

    class Pages(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.answer(served.get(self.path.split("?")[0], b""), "text/html; charset=utf-8")

        def do_POST(self) -> None:
            sent = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            result = asyncio.run(call(sent["name"], sent["arguments"]))
            self.answer(json.dumps(result).encode(), "application/json")

        def answer(self, body: bytes, kind: str) -> None:
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Pages)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
