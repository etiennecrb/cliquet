"""One MCP endpoint per app, for an agent that runs on its own: the role comes from its token,
and the servers behind it, one per role, caller and token name, are made when first asked
for."""

import contextlib
import hashlib
import threading
import time
from collections.abc import AsyncGenerator, Callable, Generator
from dataclasses import dataclass
from typing import Any

import anyio
from anyio.abc import TaskGroup, TaskStatus
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from mcp.server.streamable_http_manager import (
    DEFAULT_SESSION_IDLE_TIMEOUT,
    StreamableHTTPSessionManager,
)
from mcp_types import INVALID_REQUEST
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from cliquet import tools
from cliquet.context import Caller
from cliquet.errors import new_error_id, report, shown
from cliquet.mcp_server import Admit, AppServer, KeepError
from cliquet.tools import app_role
from cliquet_server import tokens
from cliquet_server.app_auth import (
    AppAuth,
    PersonAccess,
    app_address,
    expired_token,
    limited_elsewhere,
    me_address,
    needs_limited_token,
    tokens_link,
)
from cliquet_server.app_errors import keeper, where_kept
from cliquet_server.app_icon import app_icons
from cliquet_server.apps import Apps, Live, UnknownApp
from cliquet_server.callers import bearer, challenge
from cliquet_server.db import PersonInfo, TokenExpired
from cliquet_server.executor import SandboxError
from cliquet_server.inputs import References
from cliquet_server.limiter import Limits, admitted, logged, why
from cliquet_server.sandbox_time import OverBudget, runs_app_code

# What an error of an app kept before any tool was called names as its tool: a refusal of the
# token an agent connected with.
_CONNECT = "(connect)"


def role_of(role: str) -> str | None:
    """The role an address names: `-` is an app without roles."""
    return None if role == "-" else role


def role_label(role: str) -> str:
    """A role as a person reads it: `-`, the one role of an app without roles, is member."""
    return "member" if role == "-" else role


def caller_role(manifest: dict[str, Any], role: str) -> str | None:
    """The role a caller gets from the role in an address. Raises `NoSuchRole`."""
    return app_role(manifest, role_of(role))


class Unopened(Exception):
    """Why a person can't open an app as a role: each surface answers it in its own way."""


class NotYours(Unopened):
    """The person may not open the app, or it isn't published: answered as a missing app."""


class ShareWaiting(NotYours):
    """A share of the app waits for the person to accept it. Only what Cliquet knows itself:
    the app's owner and name, and the roles shared. A surface that offers no way to accept
    it answers it as a missing app."""

    def __init__(self, login: str, name: str, roles: list[str]) -> None:
        super().__init__(f"{login}/{name} waits for you to accept it")
        self.login, self.name, self.roles = login, name, roles


class NoRole(Unopened):
    """The person doesn't hold `role`, which the app has, or, with no role named, holds none
    to pick. `held` is the roles they hold."""

    def __init__(self, role: str | None, held: list[str]) -> None:
        super().__init__(f"you don't hold the role {role}" if role else "you hold no role")
        self.role, self.held = role, held


class UnknownRole(Unopened):
    """The app has no role `role`, as an address names it. `roles` is the app's roles, None
    when a publish changed them meanwhile, and `held` the person's. `listed` names the app's
    roles, for a person who may read them: the owner, or a member whose role it dropped."""

    def __init__(
        self, role: str, roles: list[str] | None, held: list[str], listed: str | None
    ) -> None:
        super().__init__(listed or f"there is no role {role}")
        self.role, self.roles, self.held, self.listed = role, roles, held, listed


class NotStarted(Unopened):
    """The app can't be started: `why`, as the person may read it. `retry_after` is set when
    the month's sandbox time is used, to the seconds until it resets."""

    def __init__(self, why: str, retry_after: int | None = None) -> None:
        super().__init__(why)
        self.why, self.retry_after = why, retry_after


@dataclass(frozen=True)
class Opened:
    """An app opened for a person: its live version, the role in the address (`-` for an app
    without roles), the roles the person holds, and the server for that role."""

    live: Live
    address: str
    held: list[str]
    server: AppServer
    agent: tokens.ApiToken | None = None

    @property
    def role(self) -> str | None:
        """The role the person acts with: None in an app without roles."""
        return role_of(self.address)


@contextlib.contextmanager
def _refused(
    who: str, login: str, name: str, address: str = "-", held: list[str] | None = None
) -> Generator[None]:
    """Turn a failed start or a missing app into why the person can't open it, and a role
    the app no longer has, when it was published again while it was opened as `address`."""
    try:
        yield
    except UnknownApp:
        raise NotYours(f"there is no app {login}/{name}") from None
    except tools.NoSuchRole as error:
        raise UnknownRole(address, None, held or [], str(error)) from None
    except SandboxError as error:
        wait = error.retry_after if isinstance(error, OverBudget) else None
        raise NotStarted(why_not_started(error, who, login, name), wait) from None


def why_not_started(error: SandboxError, who: str, login: str, name: str) -> str:
    """Why `login`'s app `name` can't be started, as the person `who` may read it: in full
    for the app's owner. For anyone else, an id in place of any text from the app's code,
    such as its build problems: the one the failed start is kept under for the owner, or else
    one the server's log has. The month's sandbox time, used, is said as it is, and logged
    where it is counted."""
    if isinstance(error, OverBudget):
        return error.told(who == login)
    kept = error.error_id
    if kept is None:
        return shown(error, str(error), who == login, "", error_id=_logged(error, login, name))
    # Kept as the start failed: only the sentence that says where the owner reads it is new.
    return shown(
        error, str(error), who == login, "", error_id=kept, keep=lambda *_: where_kept(kept)
    )


# How long one failure to start keeps its id, and how many are kept.
_LOGGED_FOR = 600.0
_LOGGED_MOST = 256
_logged_ids: dict[tuple[str, str, str], tuple[str, float]] = {}
_logged_lock = threading.Lock()
_now = time.monotonic


def _logged(error: SandboxError, login: str, name: str) -> str:
    """The id an app's failure to start is logged under: logged once, with its traceback,
    and then the same id for ten minutes, however often it is met."""
    key = (login, name, hashlib.sha256(str(error).encode()).hexdigest())
    now = _now()
    with _logged_lock:
        found = _logged_ids.get(key)
        if found is not None and now - found[1] < _LOGGED_FOR:
            return found[0]
    error_id = report(error, f"{login}/{name} can't be started: {error}")
    with _logged_lock:
        _logged_ids.pop(key, None)
        _logged_ids[key] = (error_id, now)
        while len(_logged_ids) > _LOGGED_MOST:
            del _logged_ids[next(iter(_logged_ids))]
    return error_id


# The key of an app's server: the owner's login, the app's name, the role, the caller, and the
# API token the caller connected with, by its name and id, None for a sign-in. A token made
# again under the same name has a new id, so it never gets the old one's server.
_Key = tuple[str, str, str, str, tokens.ApiToken | None]

# How many of the sessions a publish ended are remembered, so their next request is told why.
# Each is remembered for the library's idle time: by then it would have ended anyway.
_ENDED_MOST = 4096
_ENDED_FOR = DEFAULT_SESSION_IDLE_TIMEOUT

# How long an entry stays with no request: twice a session's idle time. Both clocks start
# after the last request, so the library has ended an entry's sessions before it is dropped,
# and the drop ends no session a client could still use.
IDLE_FOR = 2 * DEFAULT_SESSION_IDLE_TIMEOUT


@dataclass(eq=False)
class _Served:
    """A running session manager at `key`, the scope of its task, and how many requests it
    is serving now. The count is the entry's own, so a new entry at the key starts at none."""

    key: _Key
    manager: StreamableHTTPSessionManager
    scope: anyio.CancelScope
    busy: int = 0


class Registry:
    """The apps' servers and MCP endpoints, one per app, role, caller and token, made when
    first asked for, and dropped once their app changes or after `IDLE_FOR` unused. A change
    made through one keeps the token's name, and the app's code sees the token's id.

    No lock of the registry is held while an app starts, which can take minutes: a call
    waits for its own app, never for another's.
    """

    def __init__(self, apps: Apps, limits: Limits | None = None) -> None:
        self._apps = apps
        # The references to the files people upload for an action, for every app's servers.
        settings = apps.settings
        self.inputs = References(settings.data_dir / "inputs", settings.base_url)
        # Where the apps' icons are served, and the site's address: the server's own unless a
        # deployment sets it apart.
        self._base_url = apps.settings.base_url
        self._website_url = apps.settings.website_url
        # What limits the calls of the apps' MCP clients, each app on its own: none in tests
        # that don't ask for it.
        self.limits = limits
        self._group: TaskGroup | None = None
        # Guards the dictionaries, for a moment: never across an app's start.
        self._lock = anyio.Lock()
        # A manager serves the server stored at its key.
        self._servers: dict[_Key, AppServer] = {}
        self._managers: dict[_Key, _Served] = {}
        # When each key was last looked up or ended a request, on `monotonic`.
        self._used: dict[_Key, float] = {}
        # The sessions a publish ended, by key and session id, with when, on `monotonic`: the
        # oldest first, so a lookup tells a client why its session is gone.
        self._ended: dict[tuple[_Key, str], float] = {}
        # The clock of `_ended` and `_used`: a test can make time pass.
        self.monotonic: Callable[[], float] = time.monotonic

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncGenerator[None]:
        """Run the session managers' tasks until the block ends."""
        async with anyio.create_task_group() as group:
            self._group = group
            try:
                yield
            finally:
                group.cancel_scope.cancel()

    def _sweep(self) -> None:
        """Drop the entries made from an app that isn't the one served now: published again,
        or forgotten, even after the entry was stored. Drop as well those with no request in
        flight that went `IDLE_FOR` unused. Call it with `self._lock` held.

        It is lazy: a publish drops nothing, and the next lookup of any app sweeps, so a
        session's next request finds its manager gone, and is told the app was published, or
        after an idle drop only that its session ended, as the library had already ended it."""
        now = self.monotonic()
        for key, server in list(self._servers.items()):
            served = self._apps.current(key[0], key[1])
            if served is None or served.runtime is not server.runtime:
                self._drop(key, published=True)
                continue
            found = self._managers.get(key)
            if (found is None or not found.busy) and now - self._used[key] >= IDLE_FOR:
                self._drop(key, published=False)

    def _drop(self, key: _Key, *, published: bool) -> None:
        """Drop the server and manager at `key`, ending its sessions. When `published`, the
        sessions are remembered as ended by a publish. Call it with `self._lock` held."""
        self._servers.pop(key, None)
        self._used.pop(key, None)
        found = self._managers.pop(key, None)
        if found is None:
            return
        if published:
            # Read before the cancel, which makes the manager forget its sessions. A library
            # that renamed it gives none: those sessions are then told only that they ended.
            sessions: dict[str, object] = getattr(found.manager, "_server_instances", {})
            now = self.monotonic()
            for session_id in sessions:
                self._ended[(key, session_id)] = now
            self._prune(now)
        found.scope.cancel()

    def _prune(self, now: float) -> None:
        """Forget the ended sessions past `_ENDED_FOR`, then the oldest over `_ENDED_MOST`."""
        ended = self._ended
        while ended:
            oldest = next(iter(ended))
            if now - ended[oldest] < _ENDED_FOR and len(ended) <= _ENDED_MOST:
                break
            del ended[oldest]

    def ended_by_publish(self, opened: Opened, who: str, session_id: str) -> bool:
        """Whether a publish ended the session `session_id` of `who` at the app `open_for`
        opened, in the last 30 minutes."""
        info = opened.live.info
        key = (info.login, info.name, opened.address, who, opened.agent)
        at = self._ended.get((key, session_id))
        return at is not None and self.monotonic() - at < _ENDED_FOR

    async def _serve(
        self,
        server: AppServer,
        *,
        task_status: TaskStatus[
            tuple[StreamableHTTPSessionManager, anyio.CancelScope]
        ] = anyio.TASK_STATUS_IGNORED,
    ) -> None:
        # An entry outlives its sessions: see `IDLE_FOR`.
        manager = StreamableHTTPSessionManager(
            app=server.server, session_idle_timeout=DEFAULT_SESSION_IDLE_TIMEOUT
        )
        with anyio.CancelScope() as scope:
            async with manager.run():
                task_status.started((manager, scope))
                await anyio.sleep_forever()

    async def app_server(
        self,
        login: str,
        name: str,
        role: str,
        who: str = "guest",
        agent: tokens.ApiToken | None = None,
    ) -> AppServer:
        """The app's MCP server for this role and caller, made if needed. `agent` is the API
        token the caller connected with: each change keeps its name, and the app sees its id."""
        key = (login, name, role, who, agent)
        async with self._lock:
            self._sweep()
            found = self._servers.get(key)
            if found is not None:
                self._used[key] = self.monotonic()
                return found
        live = await self._apps.started(login, name)
        async with self._lock:
            # Made by another call while this one waited for the app.
            found = self._servers.get(key)
            if found is not None:
                self._used[key] = self.monotonic()
                return found
            token_id = None if agent is None else agent.id
            caller = Caller(who, caller_role(live.runtime.manifest, role), token_id)
            # The errors of the app's code are kept for its owner, whoever the caller is.
            keep = keeper(self._apps, login, name)
            admit = None
            if self.limits is not None or self._apps.meter is not None:
                admit = _admit(self._apps, self.limits, login, name, caller, agent, keep)
            server = AppServer(
                live.runtime,
                caller,
                live.instructions,
                keep_error=keep,
                agent=None if agent is None else agent.name,
                admit=admit,
                limiter=self._apps.calls,
                icons=app_icons(live.info, self._base_url),
                website_url=self._website_url,
                files=self.inputs.for_app(login, name),
            )
            self._servers[key] = server
            self._used[key] = self.monotonic()
            return server

    async def tell_expired(self, login: str, name: str, expired: TokenExpired) -> None:
        """Keep a refusal of an expired token at the app's address as an error of the app,
        once an hour per token, as a call over a limit is, so its owner sees that an agent of
        theirs, or a share's, still uses it. Only for a token limited to the app whose person
        still holds its role there, as `open_for` checks a live one: its owner made it for
        another place, or not at all, or no longer shares the app, otherwise."""
        role = next(
            (one.role for one in expired.limits or () if (one.login, one.name) == (login, name)),
            None,
        )
        if role is None or self.limits is None:
            return
        who = expired.person.login
        database = self._apps.database
        if not await anyio.to_thread.run_sync(database.may_use, who, login, name, role):
            return
        key = f"expired {app_key(login, name)} {expired.token.id}"
        if not self.limits.told.first(key):
            return
        told = (
            f"{who} with the token {expired.token.name!r} was refused at the app's address: "
            f"the token expired on {tokens.expiry(expired.at)}. If an agent still uses it, "
            f"give it a new token, made at {self._base_url}/tokens."
        )
        caller = Caller(who, role_of(role), expired.token.id)
        keep = keeper(self._apps, login, name)
        await anyio.to_thread.run_sync(keep, caller, _CONNECT, new_error_id(), told)

    async def manager(
        self, login: str, name: str, role: str, who: str, agent: tokens.ApiToken | None = None
    ) -> _Served:
        """The app's MCP session manager for this role, caller and token, made if
        needed: only ever for the server stored for that key, read under the same lock. It
        is held for one request, and kept until `left` is called with it."""
        key = (login, name, role, who, agent)
        while True:
            server = await self.app_server(login, name, role, who, agent)
            async with self._lock:
                if self._servers.get(key) is not server:
                    # Dropped while this call waited for the lock: get the one served now.
                    continue
                # A manager stored at the key serves that server: dropping one drops both.
                found = self._managers.get(key)
                if found is None:
                    if self._group is None:
                        raise RuntimeError("the registry is not running")
                    # Quick: it starts the manager's task, so two calls never make two managers.
                    manager, scope = await self._group.start(self._serve, server)
                    found = self._managers[key] = _Served(key, manager, scope)
                found.busy += 1
                return found

    def left(self, served: _Served) -> None:
        """A request `manager` held `served` for has ended: it counts as a use. With no await,
        so it needs no lock and runs even in a cancelled request."""
        served.busy -= 1
        if self._managers.get(served.key) is served:
            self._used[served.key] = self.monotonic()

    async def open_for(
        self,
        person: PersonInfo,
        login: str,
        name: str,
        role: str | None,
        *,
        only: bool = False,
        agent: tokens.ApiToken | None = None,
    ) -> Opened:
        """The app opened for the person as `role`, an address's role, or with no role named,
        the first role they hold, or with `only` the one they hold. `agent` names the token
        that acts, as `app_server` keeps it. Every surface that opens
        an app asks here. Raises `Unopened`, and `AppStarting` while the app starts.

        A share that waits gives nothing, and an app the person may not open is missing.
        The database is called in threads, each call short, never on the event loop.
        """
        database = self._apps.database
        if not await anyio.to_thread.run_sync(database.may_open, person.login, login, name):
            shares = await anyio.to_thread.run_sync(database.pending_shares, person.id)
            waiting = [one.role for one in shares if (one.login, one.name) == (login, name)]
            if waiting:
                raise ShareWaiting(login, name, waiting)
            raise NotYours(f"there is no app {login}/{name}")
        with _refused(person.login, login, name):
            live = await self._apps.started(login, name)
        held = await anyio.to_thread.run_sync(self._apps.held_roles, person, live)
        roles = list(live.runtime.manifest["roles"])
        if role is None and roles:
            if not held or (only and len(held) > 1):
                raise NoRole(None, held)
            role = held[0]
        else:
            # A role named, or the one role of an app without roles, which the person must
            # still hold.
            named, role = role is not None, role or "-"
            may_use = await anyio.to_thread.run_sync(
                database.may_use, person.login, login, name, role
            )
            if named:
                try:
                    caller_role(live.runtime.manifest, role)
                except tools.NoSuchRole as error:
                    listed = str(error) if may_use else None
                    raise UnknownRole(role, roles, held, listed) from None
            if not may_use:
                raise NoRole(role if named else None, held)
        with _refused(person.login, login, name, role, held):
            server = await self.app_server(login, name, role, person.login, agent)
        return Opened(live, role, held, server, agent)

    async def opened_manager(self, opened: Opened, who: str) -> _Served:
        """The MCP session manager of an app `open_for` opened, held as `manager` holds it.
        Raises `Unopened` only if the app was published again meanwhile and changed."""
        info = opened.live.info
        with _refused(who, info.login, info.name, opened.address, opened.held):
            return await self.manager(info.login, info.name, opened.address, who, opened.agent)


def app_key(login: str, name: str) -> str:
    """The key an app's calls are counted under, on its address and its pages alike."""
    return f"app {login}/{name}"


def _admit(
    apps: Apps,
    limits: Limits | None,
    login: str,
    name: str,
    caller: Caller,
    agent: tokens.ApiToken | None,
    keep: KeepError,
) -> Admit:
    """What lets the calls of an app's MCP clients through, counted for the app whichever
    token calls, or refuses one: logged once a minute, and kept once an hour as an error of
    the app, so its owner sees that an agent of theirs, or a share's, is stuck in a loop.

    A call that runs in the owner's sandbox is refused first once the month's sandbox time
    that holds it is used, and the owner's is kept once an hour as an error of the app, so
    its owner sees why their agents stopped."""
    key = app_key(login, name)
    who = caller.id if agent is None else f"{caller.id} with the token {agent.name!r}"

    @contextlib.asynccontextmanager
    async def admit(tool: str) -> AsyncGenerator[str | None]:
        over = apps.over_budget(login) if runs_app_code(tool) else None
        if over is not None:
            if not over.server and limits is not None and limits.told.first(f"budget {key}"):
                told = f"{who} was refused at the app's address: {over}"
                await anyio.to_thread.run_sync(keep, caller, tool, new_error_id(), told)
            yield over.told(caller.id == login)
            return
        if limits is None:
            yield None
            return
        with admitted(limits.calls, key) as refusal:
            if refusal is None:
                yield None
                return
        logged(refusal, limits.logged, who)
        refused = why(refusal, f"{login}/{name}")
        if limits.told.first(key):
            told = (
                f"{who} hit a limit at the app's address, and calls over it are refused: "
                f"{refused} If an agent is stuck in a loop, stop it, or revoke its token."
            )
            await anyio.to_thread.run_sync(keep, caller, tool, new_error_id(), told)
        yield refused

    return admit


class AppEndpoint:
    """The ASGI app behind `/apps/{login}/{app}/mcp`. It takes only Cliquet's tokens limited to
    its app, among others, and acts as the token's person in the token's role for it."""

    def __init__(self, registry: Registry, auth: AppAuth) -> None:
        self._registry = registry
        self._auth = auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        params = scope["path_params"]
        login, name = params["login"], params["app"]
        found = await self._limited(login, name, scope, receive, send)
        if found is None:
            return
        access, role = found
        person = access.person
        assert person is not None
        try:
            opened = await self._registry.open_for(
                person, login, name, role, agent=access.api_token
            )
            served = await self._registry.opened_manager(opened, person.login)
        except ShareWaiting:
            why = (
                f"{login}/{name} waits for you to accept its share: accept it on the Apps page "
                "in the console, or ask your assistant to."
            )
            await _error(scope, receive, send, 404, why)
            return
        except NotYours:
            why = f"There is no app {login}/{name} for you. Check the address."
            await _error(scope, receive, send, 404, why)
            return
        except NoRole:
            await _error(
                scope,
                receive,
                send,
                403,
                f"This token is limited to the role {role}, which you no longer hold in "
                f"{login}/{name}: ask its owner to share it again, or revoke the token.",
            )
            return
        except UnknownRole as error:
            await _error(scope, receive, send, 404, str(error))
            return
        except NotStarted as error:
            if error.retry_after is not None:
                headers = {"Retry-After": str(error.retry_after)}
                await rpc_error(429, error.why, headers)(scope, receive, send)
                return
            await _error(scope, receive, send, 503, f"The app can't be started: {error.why}")
            return
        # The entry is kept while the request runs, an open stream included.
        try:
            session_id = Request(scope).headers.get(MCP_SESSION_ID_HEADER)
            if session_id is not None:
                if self._registry.ended_by_publish(opened, person.login, session_id):
                    why = (
                        f"{login}/{name} was published again, so this session ended before "
                        "your request ran. Connect again to get the new version's tools."
                    )
                else:
                    why = "This session ended before your request ran. Connect again."
                send = _session_gone(scope, receive, send, why)
            await served.manager.handle_request(scope, receive, send)
        finally:
            self._registry.left(served)

    async def _limited(
        self, login: str, name: str, scope: Scope, receive: Receive, send: Send
    ) -> tuple[PersonAccess, str] | None:
        """The access of a token limited to this app, among others, and its role here, or None
        once the request has been refused: with no token, one that isn't Cliquet's or isn't
        valid, one that expired, one for everywhere, or one limited to other apps. No answer
        names an authorization server. A person who must verify their email first is told so."""
        base = self._auth.settings.base_url
        needs = needs_limited_token(base, login, name)
        token = bearer(scope)
        if token is None or not tokens.is_token(token):
            await _unauthorized(scope, receive, send, needs, token is not None)
            return None
        access = await self._auth.access(token)
        if access is not None and access.expired is not None:
            await self._registry.tell_expired(login, name, access.expired)
            message = expired_token(base, access.expired.at)
            await _unauthorized(scope, receive, send, message, True)
            return None
        if access is None or access.person is None:
            message = f"The token is invalid or was revoked. {needs}"
            await _unauthorized(scope, receive, send, message, True)
            return None
        if access.locked_out is not None:
            await _error(scope, receive, send, 403, access.locked_out)
            return None
        limits = access.limits
        if limits is None:
            await _error(scope, receive, send, 403, needs)
            return None
        role = next((one.role for one in limits if (one.login, one.name) == (login, name)), None)
        if role is None:
            await _error(scope, receive, send, 403, limited_elsewhere(base, limits))
            return None
        return access, role


def _session_gone(scope: Scope, receive: Receive, send: Send, why: str) -> Send:
    """`send`, with the 404 the session manager gives a session it doesn't know replaced by
    one that says `why`."""
    gone = False

    async def sending(message: Message) -> None:
        nonlocal gone
        if message["type"] == "http.response.start" and message["status"] == 404:
            gone = True
            return
        if not gone:
            await send(message)
            return
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            await rpc_error(404, why)(scope, receive, send)

    return sending


async def _unauthorized(
    scope: Scope, receive: Receive, send: Send, message: str, sent: bool
) -> None:
    """`callers.unauthorized` at an app's address: the same 401 and challenge, with the
    message in a JSON-RPC error."""
    await rpc_error(401, message, challenge(message, sent))(scope, receive, send)


def rpc_error(status: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    """A refusal at an app's address, as `status`: a JSON-RPC error with no id, as the MCP
    library sends its own, so that a client shows `message` rather than its own text."""
    error = {"code": INVALID_REQUEST, "message": message}
    return JSONResponse({"jsonrpc": "2.0", "id": None, "error": error}, status, headers)


async def _error(scope: Scope, receive: Receive, send: Send, status: int, message: str) -> None:
    """Answer a refusal at an app's address."""
    await rpc_error(status, message)(scope, receive, send)


async def moved(request: Request) -> JSONResponse:
    """The answer at an old address with a role, `/apps/{login}/{app}/{role}/mcp`: gone, and
    where an agent connects now. No redirect: the new address takes another kind of token."""
    login, name = request.path_params["login"], request.path_params["app"]
    base = request.app.state.settings.base_url
    message = (
        f"This address is gone: connect an assistant at {me_address(base)}, or give an agent "
        f"that runs on its own {app_address(base, login, name)} with a token limited to the "
        f"app, made at {base}{tokens_link(login, name)}."
    )
    return rpc_error(410, message)


async def builder_gone(scope: Scope, receive: Receive, send: Send) -> None:
    """The answer at the builder's old address, `/builder/mcp`, and its metadata, whatever the
    method or path: gone, and where an assistant connects now. It names no authorization
    server, so no client tries to sign in there."""
    base = scope["app"].state.settings.base_url
    message = (
        f"This address is gone: connect your assistant at {me_address(base)}, which serves "
        "the builder's tools to the people who may build."
    )
    await JSONResponse({"error": message}, status_code=410)(scope, receive, send)
