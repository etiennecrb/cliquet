"""Who calls Cliquet's own FastMCP server, the person's endpoint, which serves the builder's
tools too, and what their client draws.

It takes bearer tokens through `AppAuth`: Cliquet's API tokens for everywhere, and the sign-in
provider's access tokens. The provider is its authorization server, and it registers no client
and issues no token of its own. It is one resource: its metadata names its address, and it
takes the provider's tokens for one audience only, that address by default. claude.ai refuses
metadata whose resource is not the address it connects to, and asks the provider for a token
for that address. A token limited to some apps is refused here, before FastMCP reads the
request: it works on those apps' endpoints only. So is an expired token, with the sentence
that says when it expired.
"""

from collections.abc import Callable, Collection, Mapping
from typing import Any

from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.server.auth import AccessToken, RemoteAuthProvider, TokenVerifier
from fastmcp.server.dependencies import get_access_token, get_context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp.server.apps import client_supports_apps
from mcp_types import CallToolRequestParams, ReadResourceRequestParams
from pydantic import AnyHttpUrl, ConfigDict, TypeAdapter
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from cliquet_server import tokens
from cliquet_server.app_auth import (
    CHOOSE_FIRST,
    SCOPES,
    AppAuth,
    AuthRefused,
    Newcomer,
    PersonAccess,
    limited_elsewhere,
)
from cliquet_server.apps import Apps
from cliquet_server.db import PersonInfo
from cliquet_server.limiter import Limited, Limits, admitted, logged, why, why_runs
from cliquet_server.sign_in import development_person

# An address as it is written: an issuer is compared as a string, so a slash is never added.
_EXACT_URL = TypeAdapter(AnyHttpUrl, config=ConfigDict(url_preserve_empty_path=True))


class EndpointAuth(RemoteAuthProvider):
    """The sign-in of a FastMCP server mounted at `mount`, such as /me: bearer tokens checked
    by `AppAuth` for its own address, the resource its metadata names."""

    def __init__(self, app_auth: AppAuth, mount: str) -> None:
        super().__init__(
            # Never called: verify_token below checks the tokens. It requires no scope, as an
            # app endpoint requires none: a provider may grant a third-party client such as
            # claude.ai `offline_access` alone, as Auth0 does, and the person is found by the
            # token's subject.
            token_verifier=TokenVerifier(),
            authorization_servers=[_EXACT_URL.validate_python(app_auth.authorization_server())],
            base_url=f"{app_auth.settings.base_url}{mount}",
            scopes_supported=list(SCOPES),
        )
        self.app_auth = app_auth

    async def verify_token(self, token: str) -> AccessToken | None:
        """The access the token gives, with its person, or None when it is invalid or
        expired."""
        resource = None if self._resource_url is None else str(self._resource_url)
        access = await self.app_auth.access(token, resource)
        if access is not None and access.expired is not None:
            # `RefuseTokens` answers first, with the sentence that says when. This keeps an
            # expired token out of any FastMCP server mounted without it.
            return None
        refused = _limited(self.app_auth, access)
        if access is not None and refused is not None:
            # `RefuseTokens` answers first. This keeps a limited token out of any FastMCP
            # server mounted without it.
            return access.model_copy(update={"person": None, "refused": AuthRefused(403, refused)})
        return access


class RefuseTokens:
    """Refuse a request whose bearer token is one of Cliquet's that expired, as a revoked one
    is, with the sentence that says when, or one limited to some apps, with the sentence that
    says where it works and what reaches the rest. Every other request goes on to `app`."""

    def __init__(self, app: ASGIApp, app_auth: AppAuth) -> None:
        self.app = app
        self.app_auth = app_auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        token = bearer(scope) if scope["type"] == "http" else None
        if token is not None and tokens.is_token(token):
            access = await self.app_auth.access(token)
            if access is not None and access.refused is not None:
                # Expired: refused as a revoked token is, with the sentence that says when.
                await unauthorized(scope, receive, send, access.refused.message, True)
                return
            refused = _limited(self.app_auth, access)
            if refused is not None:
                await JSONResponse({"error": refused}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _limited(app_auth: AppAuth, access: PersonAccess | None) -> str | None:
    """Why a token limited to some apps is refused here, or None for any other."""
    if access is None or access.limits is None:
        return None
    return limited_elsewhere(app_auth.settings.base_url, access.limits)


async def unauthorized(
    scope: Scope, receive: Receive, send: Send, message: str, sent: bool
) -> None:
    """A 401 that names no authorization server and no protected-resource metadata, so a
    client sent here with one of Cliquet's tokens doesn't try to sign in: a person makes a
    token instead. Only a request that `sent` a token is told it is invalid (RFC 6750)."""
    headers = challenge(message, sent)
    await JSONResponse({"error": message}, status_code=401, headers=headers)(scope, receive, send)


def challenge(message: str, sent: bool) -> dict[str, str]:
    """The headers of a 401 that names no authorization server: invalid, with `message`,
    only for a request that `sent` a token (RFC 6750)."""
    if not sent:
        return {"WWW-Authenticate": "Bearer"}
    described = f'Bearer error="invalid_token", error_description="{_quote(message)}"'
    return {"WWW-Authenticate": described}


def _quote(value: str) -> str:
    """A value safe in an HTTP quoted string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def bearer(scope: Scope) -> str | None:
    """The bearer token in a request, if there is one."""
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization" and value[:7].lower() == b"bearer ":
            token = value[7:].strip().decode(errors="replace")
            return token or None
    return None


def token_person(*, listing: bool = False) -> PersonInfo | None:
    """The person behind the call's bearer token, as `AppAuth` found it, or None when the call
    carries none. Raises `ToolError` when the token names no person, a newcomer's too, and,
    unless it is for `listing` their tools or counting their calls, when they must verify
    their email first."""
    token = get_access_token()
    if not isinstance(token, PersonAccess):
        return None
    if token.refused is not None:
        raise ToolError(token.refused.message)
    if token.newcomer is not None:
        raise ToolError(CHOOSE_FIRST)
    if token.locked_out is not None and not listing:
        raise ToolError(token.locked_out)
    return token.person


def token_newcomer() -> Newcomer | None:
    """The newcomer behind the call's bearer token, an identity with no person yet, if it is
    one."""
    token = get_access_token()
    return token.newcomer if isinstance(token, PersonAccess) else None


def caller(apps: Apps, *, listing: bool = False) -> PersonInfo:
    """The person behind the call, as the database has them now: the bearer token's, or, for
    a client in memory, as in tests, the development login's. Raises `ToolError` when there
    is none, as `token_person` does."""
    person = token_person(listing=listing)
    if person is not None:
        return person
    if apps.settings.dev_login is not None:
        try:
            return development_person(apps.database, apps.settings, apps.now())
        except ValueError as error:
            raise ToolError(str(error)) from None
    raise ToolError("Sign in to Cliquet, or send an API token, to use your apps.")


def counted_as(apps: Apps) -> str:
    """Whom the call counts against for its limits: the person's login, one whose time to
    verify their email is over too, or a newcomer's issuer and subject, which no login can be,
    as they hold a space. Raises `ToolError` when there is nobody."""
    newcomer = token_newcomer()
    if newcomer is not None:
        return f"newcomer {newcomer.issuer} {newcomer.subject}"
    return caller(apps, listing=True).login


def api_token() -> tokens.ApiToken | None:
    """The call's API token, by its name and id, if it is one of Cliquet's: None for a sign-in."""
    token = get_access_token()
    return token.api_token if isinstance(token, PersonAccess) else None


def draws_screens() -> bool:
    """Whether the client draws MCP Apps: as it said when it connected, before 2026-07-28, or
    on this request, from 2026-07-28."""
    request = get_context().request_context
    if request is None:
        return False
    return client_supports_apps(request._srctx)


class PersonLimits(Middleware):
    """Counts each person's calls of one of Cliquet's own servers, `where`, such as "your
    apps", and refuses those over its limits with a tool error the agent reads. The calls of
    a tool in `elsewhere` count where it says instead, such as "the builder". The calls of
    `runs` also count against the person's runs in their sandbox per hour.

    `login` names whom the call counts against, as `counted_as` does, or raises `ToolError`
    when there is nobody: such a call isn't counted, and its tool refuses it.
    """

    def __init__(
        self,
        limits: Limits,
        where: str,
        login: Callable[[], str],
        runs: Collection[str] = (),
        elsewhere: Mapping[str, str] | None = None,
    ) -> None:
        self.limits, self.where, self.login, self.runs = limits, where, login, frozenset(runs)
        self.elsewhere = dict(elsewhere or {})

    def _key(self, where: str) -> str | None:
        """The key the call is counted under, or None when no person is behind it."""
        try:
            return f"{where} {self.login()}"
        except ToolError:
            return None

    def _refused(self, refusal: Limited, where: str) -> str:
        """Why a call is refused, logged once a minute."""
        logged(refusal, self.limits.logged)
        return why(refusal, where)

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Run the call, or refuse it when its person is over a limit."""
        where = self.elsewhere.get(context.message.name, self.where)
        key = self._key(where)
        if key is None:
            return await call_next(context)
        with admitted(self.limits.calls, key) as refusal:
            if refusal is None:
                if context.message.name in self.runs:
                    try:
                        # A key of its own, so each kind of refusal is logged.
                        self.limits.sandbox_runs.take(f"sandbox {key}")
                    except Limited as over:
                        logged(over, self.limits.logged)
                        raise ToolError(why_runs(over)) from None
                return await call_next(context)
        raise ToolError(self._refused(refusal, where))

    async def on_read_resource(
        self,
        context: MiddlewareContext[ReadResourceRequestParams],
        call_next: CallNext[ReadResourceRequestParams, Any],
    ) -> Any:
        """Read the resource, or refuse it when its person is over a limit."""
        key = self._key(self.where)
        if key is None:
            return await call_next(context)
        with admitted(self.limits.calls, key) as refusal:
            if refusal is None:
                return await call_next(context)
        raise ResourceError(self._refused(refusal, self.where))
