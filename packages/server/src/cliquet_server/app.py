"""The Cliquet server as an ASGI app."""

import contextlib
import functools
import logging
import threading
from collections.abc import AsyncGenerator, Callable, Collection, Mapping
from datetime import UTC, datetime

import anyio
import httpx
from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import BaseRoute, Mount, Route, compile_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cliquet.limits import MAX_BODY, MAX_UPLOAD
from cliquet.sandbox import gzipped_answers
from cliquet_server import __version__
from cliquet_server.accounts import Accounts
from cliquet_server.app_auth import AppAuth
from cliquet_server.apps import Apps, AppStarting
from cliquet_server.builder import Builder, builder_enabled
from cliquet_server.callers import EndpointAuth, RefuseTokens
from cliquet_server.db import open_database
from cliquet_server.endpoints import AppEndpoint, Registry, builder_gone, moved, rpc_error
from cliquet_server.executor import Executor, make_executor
from cliquet_server.health import Health
from cliquet_server.limiter import Limits
from cliquet_server.me import make_me
from cliquet_server.oidc import Provider
from cliquet_server.pages import not_found, starting, verify_email
from cliquet_server.sandbox_time import SandboxTime
from cliquet_server.settings import Settings, is_local
from cliquet_server.sign_in import LockedOut
from cliquet_server.web import (
    FILE_UPLOAD_PATH,
    MAX_FILE_UPLOAD,
    TOO_LARGE,
    UPLOAD_PATH,
    web_routes,
)
from cliquet_server.web.common import content_length
from cliquet_server.web_sign_in import WebAuth

logger = logging.getLogger(__name__)

# Sent on every response that doesn't set its own: no sniffing, no framing by another site,
# no address sent to another site, and no page in a search engine's results, since the
# console and the apps are private, and the website says what Cliquet is.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "frame-ancestors 'self'",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "same-origin",
    "X-Robots-Tag": "noindex",
}


class SecurityHeaders:
    """ASGI middleware that adds `SECURITY_HEADERS` to each response missing them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request on, and add the headers as the response starts."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


class LoggedPageCrashes:
    """ASGI middleware that logs a request the server failed on as unexpected, with its
    traceback, and answers 500, as a page that crashes says nothing more."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request on, and log what it raised, if anything."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def sending(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, sending)
        except ClientDisconnect:
            raise  # the person went away: not a failure of the server's
        except Exception:
            logger.exception("unexpected: %s %s failed", scope["method"], scope["path"])
            if started:
                # Too late for a 500: the connection is cut, as before.
                raise
            await PlainTextResponse("Internal Server Error", status_code=500)(scope, receive, send)


# The routes whose body may pass `MAX_BODY`: the upload pages, a draft's and a reference's,
# whose file Starlette spools to disk.
BODY_LIMITS = {UPLOAD_PATH: MAX_UPLOAD, FILE_UPLOAD_PATH: MAX_FILE_UPLOAD}
# The routes that say themselves, on a page, that a body is over their limit.
OWN_REFUSALS = frozenset({UPLOAD_PATH, FILE_UPLOAD_PATH})
# An app's address, for an agent that runs on its own: it refuses with JSON-RPC errors.
APP_ADDRESS = "/apps/{login}/{app}/mcp"
_AT_APP_ADDRESS = compile_path(APP_ADDRESS)[0]


class BodyLimit:
    """ASGI middleware that refuses a request body over its route's limit, before a route
    reads it: `MAX_BODY`, or the one `limits` gives its path.

    A route in `own_refusals` gets a request whose stated length is over its limit, so that
    it answers with its own page: with no body, and `scope[TOO_LARGE]` set.

    A body that states no length is read here, to count it, so it is held in memory: it is
    at most `MAX_BODY` on every route.
    """

    def __init__(
        self,
        app: ASGIApp,
        limits: Mapping[str, int] | None = None,
        own_refusals: Collection[str] = (),
    ) -> None:
        self.app = app
        self.limits = [
            (compile_path(path)[0], limit, path in own_refusals)
            for path, limit in (limits or {}).items()
        ]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass on a request whose body is within the limit, and answer 413 to the others."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        limit, own = next(
            ((most, own) for route, most, own in self.limits if route.match(path)),
            (MAX_BODY, False),
        )
        try:
            length = content_length(Headers(scope=scope))
        except ValueError:
            await _too_large(scope, receive, send, limit)
            return
        if length is not None:
            # The HTTP server hands on no more than the length says.
            if length <= limit:
                await self.app(scope, receive, send)
            elif own:
                await self.app({**scope, TOO_LARGE: True}, _no_body(), send)
            else:
                await _too_large(scope, receive, send, limit)
            return
        # A body sent in chunks has no length: read it here, up to the limit, then hand it on.
        messages: list[Message] = []
        size = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            size += len(message.get("body", b""))
            if size > MAX_BODY:
                await _too_large(scope, receive, send, MAX_BODY)
                return
            if not message.get("more_body", False):
                break

        async def replay() -> Message:
            return messages.pop(0) if messages else await receive()

        await self.app(scope, replay, send)


def _no_body() -> Receive:
    """What a route reads of a body it was not handed: an empty one, then the client gone."""
    messages: list[Message] = [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive() -> Message:
        return messages.pop() if messages else {"type": "http.disconnect"}

    return receive


async def _too_large(scope: Scope, receive: Receive, send: Send, limit: int) -> None:
    error = f"a request's body is at most {limit:,} bytes"
    if _AT_APP_ADDRESS.match(scope["path"]):
        answer = rpc_error(413, error)
    else:
        answer = JSONResponse({"error": error}, status_code=413)
    await answer(scope, receive, send)


def _finish_deletions(accounts: Accounts) -> None:
    """Finish the account deletions a stop cut off, and log what fails unexpectedly."""
    try:
        accounts.finish()
    except Exception:
        logger.exception("unexpected: the deletions cut off weren't finished at boot")


def _finish_retired(accounts: Accounts, login: str) -> None:
    """Finish deleting the account a sign-in closed, as its person became the account waiting
    for their email, and log what fails unexpectedly: the server's next start finishes it."""
    try:
        accounts.delete(login)
    except Exception:
        logger.exception("unexpected: the closed account %s wasn't finished", login)


def create_app(
    settings: Settings,
    *,
    executor: Executor | None = None,
    clock: Callable[[], datetime] | None = None,
    limits: Limits | None = None,
    oidc_transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    """The server as an ASGI app. `limits`, made from the settings by default, says what one
    caller can make it do, and `oidc_transport`, the network by default, reaches the sign-in
    provider."""
    database = open_database(settings.data_dir, settings.database_url)
    made_executor = executor is None
    used = make_executor(settings, database) if executor is None else executor
    # Each owner's sandbox time this month, read back from the database, while a budget holds it.
    meter = SandboxTime.of(settings, database, clock or (lambda: datetime.now(UTC)))
    apps = Apps(settings, database, used, clock=clock, meter=meter)
    # An error kept for an owner goes after 30 days, in an app that fails no more too.
    database.drop_old_errors(apps.now())
    limits = Limits.of(settings) if limits is None else limits
    registry = Registry(apps, limits)
    provider = Provider(settings, oidc_transport) if settings.auth == "oidc" else None
    builder = Builder(apps, settings) if builder_enabled(settings) else None
    accounts = Accounts(apps, builder, registry.inputs)
    # An account a sign-in closed, as its person became the one waiting for their email.
    retire = functools.partial(_finish_retired, accounts)
    app_auth = AppAuth(
        settings, database, clock=clock, provider=provider, notices=apps.notices, retire=retire
    )
    web_auth = WebAuth(
        settings, database, clock=clock, provider=provider, notices=apps.notices, retire=retire
    )
    # The person's own endpoint, for every app they may use, and for building when they may.
    me_auth = EndpointAuth(app_auth, "/me")
    me_app = make_me(
        apps, registry, settings, auth=me_auth, limits=limits, builder=builder
    ).http_app(path="/mcp")

    remote = settings.allow_remote_dev_login and not is_local(settings.base_url)
    if builder is not None and remote:
        logger.warning(
            "the builder uses the development login %r on a public address: "
            "anyone can build as that login",
            settings.dev_login,
        )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        try:
            async with contextlib.AsyncExitStack() as stack:
                await stack.enter_async_context(registry.running())
                if meter is not None:
                    meter.start()
                await stack.enter_async_context(me_app.lifespan(me_app))
                if provider is not None:
                    # Never in the way of the start: the provider may be slow or down.
                    group = await stack.enter_async_context(anyio.create_task_group())
                    stack.callback(group.cancel_scope.cancel)
                    group.start_soon(provider.check)
                # In the background: the server answers meanwhile, and says what is starting.
                await anyio.to_thread.run_sync(apps.warm_up)
                # Deletions a stop cut off are finished meanwhile too, in a thread of their own.
                threading.Thread(
                    target=_finish_deletions, args=(accounts,), name="deletions", daemon=True
                ).start()
                yield
        finally:
            if builder is not None:
                builder.previews.close()
            apps.close()
            if meter is not None:
                # What the last minute counted, before the database closes.
                meter.close()
            database.close()
            if made_executor:
                used.close()

    routes: list[BaseRoute] = [
        Route("/healthz", _healthz),
        *me_auth.get_well_known_routes(mcp_path="/mcp"),
        Mount("/me", app=RefuseTokens(me_app, app_auth)),
        Route(
            APP_ADDRESS,
            endpoint=AppEndpoint(registry, app_auth),
            methods=["GET", "POST", "DELETE"],
        ),
        Route("/apps/{login}/{app}/{role}/mcp", endpoint=moved, methods=["GET", "POST", "DELETE"]),
        # The builder's old address and its metadata: /me/mcp serves its tools now.
        Mount("/builder", app=builder_gone),
        Mount("/.well-known/oauth-protected-resource/builder", app=builder_gone),
    ]
    routes += web_routes(apps, registry, settings, web_auth, builder, accounts)

    app = Starlette(
        routes=routes,
        middleware=[
            gzipped_answers(),
            Middleware(SecurityHeaders),
            Middleware(LoggedPageCrashes),
            Middleware(BodyLimit, limits=BODY_LIMITS, own_refusals=OWN_REFUSALS),
        ],
        # Wherever a request waited for an app, or its owner's sandbox, that is still starting.
        # Wherever a person whose time to verify their email is over asked for a page.
        exception_handlers={404: not_found, AppStarting: starting, LockedOut: verify_email},
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.health = Health(database, settings.data_dir)
    app.state.executor = used
    app.state.apps = apps
    app.state.registry = registry
    app.state.limits = limits
    app.state.builder = builder
    app.state.accounts = accounts
    app.state.app_auth = app_auth
    app.state.web_auth = web_auth
    app.state.provider = provider
    return app


async def _healthz(request: Request) -> JSONResponse:
    """Answer whether the server can work, with its version, and 503 with the failing parts."""
    health: Health = request.app.state.health
    if failing := await health.failing():
        body = {"ok": False, "version": __version__, "failing": failing}
        return JSONResponse(body, status_code=503)
    return JSONResponse({"ok": True, "version": __version__})
