"""The server's apps: one repository each, served through a sandbox."""

import asyncio
import contextlib
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Generator
from concurrent.futures import Future
from concurrent.futures import wait as wait_for
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
from sqlalchemy.exc import SQLAlchemyError

from cliquet.context import Caller
from cliquet.errors import AppFailed, SafeError, from_app, report
from cliquet.files import LOCK, MIGRATION, declares
from cliquet.limits import data_problem
from cliquet.runtime import Runtime
from cliquet.sandbox import bundle_of, version_id
from cliquet.scaffold import set_sdk_source
from cliquet.store import Moved
from cliquet.tools import MAX_APP_NAME
from cliquet.worker import Refusal
from cliquet_server.db import APP_NAME, AppExists, AppInfo, Database, PersonInfo, Right
from cliquet_server.engine import SandboxEngine
from cliquet_server.executor import BuildProblems, Executor, SandboxClient, SandboxError
from cliquet_server.limiter import Once
from cliquet_server.manifest import accent, icon, title
from cliquet_server.repos import AppRepo, CantRestore, changed
from cliquet_server.sandbox_time import OverBudget, SandboxTime
from cliquet_server.settings import Settings
from cliquet_server.sign_in import Notices

_log = logging.getLogger(__name__)

# How long a request waits for an app to start, before it answers that the app is starting.
# An app the sandbox built already starts in a few seconds; a new sandbox takes minutes.
START_WAIT = 20.0
# How long a page waits, before it shows that the app is starting: a person sees that at once,
# rather than a blank page, and it opens itself once the app has started.
PAGE_WAIT = 2.0
# How long one call of a builder's tool waits, in all, for apps and its owner's sandbox to
# start: an MCP client gives up on a call after about a minute, and a tool must answer before
# that. A build or a test run is never cut short, but its time is part of the call's.
BUILDER_WAIT = 45.0
# How many app calls, and how many builder calls, run at once. Each holds a worker thread
# while its sandbox works, so the CPU, not the threads, should limit them.
APP_CALLS = 100
BUILDER_CALLS = 40
# How long a builder's wait for a start lasts at least, past its call's deadline too: asking
# for a sandbox that is up, after a long build, takes a moment, and must not read as starting.
QUICK_WAIT = 1.0
# How many owners the warm-up brings up at once, at boot: the limits on uv locks and Sprite
# setups would otherwise let it start every app at once.
OWNERS_WARMED_AT_ONCE = 4
# How long a failed start is kept, in seconds: a request meanwhile is told why at once, and
# doesn't start the app again. Longer than a client is told to wait before it asks again.
FAILED_FOR = 60.0
# How often an owner's last use is written, at most, in seconds: the warm-up reads it in days.
USE_KEPT_EVERY = 600.0


class UnknownApp(Exception):
    """This owner has no published app of this name."""


class AppStarting(Exception):
    """The app is still starting, in the background: the request can be made again soon."""


class SandboxStarting(AppStarting):
    """The owner's sandbox is still starting, in the background: the call can be made again
    soon. Whatever answers that an app is starting answers this too."""


class StartFault(SandboxError, SafeError):
    """The server itself failed to start the app, said in Cliquet's own fixed words: every
    caller reads them, and the app keeps no error, since its code isn't to blame."""

    passes = True


class PublishRefused(Exception):
    """The version can't go live. `reasons` says why, one sentence or block each."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("\n".join(reasons))


@dataclass(frozen=True)
class Migrated:
    """What a migration made of all the live data at the commit `base`: None before the
    first publish."""

    base: str | None
    data: dict[str, bytes]


@dataclass(frozen=True)
class Live:
    """A published app, ready to be served."""

    info: AppInfo
    version: str
    runtime: Runtime
    instructions: str


class Apps:
    """The apps this server holds."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        executor: Executor,
        clock: Callable[[], datetime] | None = None,
        meter: SandboxTime | None = None,
    ) -> None:
        """`meter` counts each owner's sandbox time against the month's budgets: None
        counts nothing and refuses nothing."""
        self.settings = settings
        self.database = database
        self.executor = executor
        self._clock = clock or (lambda: datetime.now(UTC))
        self.meter = meter
        # What every surface tells a person once, as they come next, such as a sign-in's news.
        self.notices = Notices()
        self._cache: dict[tuple[str, str], Live] = {}
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # How long `started` and the builder wait, and a failed start is kept, by default.
        self.start_wait = START_WAIT
        self.page_wait = PAGE_WAIT
        self.builder_wait = BUILDER_WAIT
        self.failed_for = FAILED_FOR
        # The clock of a builder's tool call's deadline: a test can make starts take long.
        self.monotonic: Callable[[], float] = time.monotonic
        # When the builder's tool call running here stops waiting for starts, on `monotonic`.
        self._deadline: ContextVar[float | None] = ContextVar("deadline", default=None)
        # Whether the request running here is for a page, which waits `page_wait` for a start.
        self._page: ContextVar[bool] = ContextVar("page", default=False)
        # Each app's start while it runs, one at a time per app, then while it is kept failed.
        self._starts: dict[tuple[str, str], Future[Live]] = {}
        self._failed_at: dict[tuple[str, str], float] = {}
        self._starts_guard = threading.Lock()
        # Each owner's sandbox while the builder waits for it, one at a time per owner.
        self._sandboxes: dict[str, Future[SandboxClient]] = {}
        # How many times the apps were closed: a start begun before a close keeps nothing.
        self._closes = 0
        # Their own worker threads, so slow calls leave anyio's default ones to the pages.
        self.calls = anyio.CapacityLimiter(APP_CALLS)
        self.builder_calls = anyio.CapacityLimiter(BUILDER_CALLS)
        # When each owner's use was last written: once per `USE_KEPT_EVERY` at most.
        self._uses = Once(USE_KEPT_EVERY, clock=lambda: self.monotonic())

    def now(self) -> datetime:
        """The current time, from the server's clock."""
        return self._clock()

    def busy(self, login: str) -> contextlib.AbstractContextManager[None]:
        """Count the block as time the owner's sandbox is busy, when the server counts it."""
        return contextlib.nullcontext() if self.meter is None else self.meter.busy(login)

    def over_budget(self, login: str) -> OverBudget | None:
        """Why a new call into the owner's sandbox is refused, when the month's sandbox time
        that holds it is used, or None. It reads memory only."""
        return None if self.meter is None else self.meter.refused(login)

    def repo(self, login: str, name: str) -> AppRepo:
        """The app's repository on the server."""
        return AppRepo(self.owner_folder(login) / name)

    def owner_folder(self, login: str) -> Path:
        """The folder the owner's app repositories are kept in, made or not."""
        return self.settings.data_dir / "apps" / login

    def published(self) -> list[AppInfo]:
        """Every app that has been published."""
        return self.database.apps(published=True)

    @contextlib.contextmanager
    def builder_call(self) -> Generator[None]:
        """Run a builder's tool call with one deadline for every start it waits for.

        Its waits, in this context, use up the same `builder_wait` seconds from now. A build
        or a test run is never cut short, but its time is part of the call's.
        """
        token = self._deadline.set(self.monotonic() + self.builder_wait)
        try:
            yield
        finally:
            self._deadline.reset(token)

    @contextlib.contextmanager
    def page(self) -> Generator[None]:
        """Run a page's request: a start it waits for, with no wait of its own, is waited
        `page_wait` seconds, after which the page shows that the app is starting."""
        token = self._page.set(True)
        try:
            yield
        finally:
            self._page.reset(token)

    def _builder_left(self) -> float:
        """How long the builder may still wait for a start: what is left of its call's
        deadline, `QUICK_WAIT` at least unless `builder_wait` is less, or `builder_wait`
        outside a call."""
        deadline = self._deadline.get()
        if deadline is None:
            return self.builder_wait
        return max(min(QUICK_WAIT, self.builder_wait), deadline - self.monotonic())

    def live(self, login: str, name: str, *, again: bool = True) -> Live:
        """The app, ready to serve, for the builder's tools: `AppStarting` once it waited
        what is left of the call's deadline, while the start goes on. Raises `UnknownApp` or
        `SandboxError`.

        A failed start is tried again at once, unless not `again`: a tool that began it with
        `begin` doesn't start it twice. A request uses `started`.
        """
        cached = self._cache.get((login, name))
        if cached is not None:
            return cached
        start = self.start(login, name, again=again)
        if not _ended(start, self._builder_left()):
            raise AppStarting(f"{login}/{name} is starting")
        live = _outcome(start)
        return self._cache.get((login, name)) or live

    def begin(self, login: str, name: str) -> None:
        """Begin the app's start in the background, unless it is up, for a builder's tool that
        will wait for it later: it then runs during the tool's build and tests, rather than
        start once they have used up the call's deadline. A failed start is tried again."""
        if (login, name) not in self._cache:
            self.start(login, name, again=True)

    @contextlib.contextmanager
    def sandbox(self, login: str) -> Generator[SandboxClient]:
        """The owner's sandbox, for the builder's tools to use in the block, which counts as
        time it is busy: `SandboxStarting` once it waited what is left of the call's deadline,
        while its start goes on in the background. Raises `SandboxError`.

        A call meanwhile waits for the same start. A failed one is tried again at once.
        """
        self._note_use(login)
        start = self._sandbox_start(login)
        if not _ended(start, self._builder_left()):
            raise SandboxStarting(f"{login}'s sandbox is starting")
        client = _outcome(start)
        with self.busy(login):
            yield client

    @contextlib.contextmanager
    def _in_use(self, login: str) -> Generator[None]:
        """Run one call of the owner's live app: kept as their use, and counted as time their
        sandbox is busy."""
        self._note_use(login)
        with self.busy(login):
            yield

    def _note_use(self, login: str) -> None:
        """Keep that the owner's apps or the builder were used now, at most once per
        `USE_KEPT_EVERY`, so the warm-up after the next deploy brings their sandbox up."""
        if self._uses.first(login):
            try:
                self.database.note_apps_used(login, self._clock())
            except SQLAlchemyError as error:
                # Only the next warm-up's order depends on it: the call goes on.
                _log.warning("%s's last use couldn't be kept: %s", login, error)

    def _sandbox_start(self, login: str) -> Future[SandboxClient]:
        """The owner's sandbox start: the one that runs, else a new one, in a thread of its
        own. It always ends, with the sandbox's client or `SandboxError`."""
        with self._starts_guard:
            start = self._sandboxes.get(login)
            if start is not None:
                return start
            start = self._sandboxes[login] = Future()
        thread = threading.Thread(
            target=self._run_sandbox,
            args=(start, login),
            name=f"sandbox {login}",
            # Setting a Sprite up takes minutes: the server never waits for it to stop.
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError as error:
            _log.exception("unexpected: %s's sandbox can't be started: %s", login, error)
            self._forget_sandbox(start, login)
            start.set_exception(SandboxError(f"the server can't start it now ({error}): {_RETRY}"))
        return start

    def _run_sandbox(self, start: Future[SandboxClient], login: str) -> None:
        """Bring the owner's sandbox up, in its own thread, and end `start` with what it gave.
        It is forgotten first: the next call asks the executor again, which keeps a sandbox
        that is up and starts one that failed or stopped."""
        try:
            # Only a builder's tool asks for it: a person's call, counted.
            with self.busy(login):
                client = self._owner_sandbox(login)
        except BaseException as error:
            self._forget_sandbox(start, login)
            start.set_exception(error if isinstance(error, SandboxError) else _start_fault(error))
            return
        self._forget_sandbox(start, login)
        start.set_result(client)

    def _owner_sandbox(self, login: str) -> SandboxClient:
        """The owner's sandbox, from the executor: the one way every start, publish and tool
        asks for it. A failure is Cliquet's own, never the app's, and logged here once, as
        unexpected: a `SandboxError` as it is raised, anything else as a `StartFault` in
        Cliquet's fixed words. A set-up cut off by a close, as at a deploy, isn't logged."""
        closes = self._closes
        try:
            return self.executor.sandbox(login)
        except Exception as error:
            if self._closes == closes:
                _log.exception("unexpected: %s's sandbox didn't start: %s", login, error)
            if isinstance(error, SandboxError):
                raise
            raise _start_fault(error) from error

    def _forget_sandbox(self, start: Future[SandboxClient], login: str) -> None:
        """Forget the owner's sandbox start `start`, unless another one began since."""
        with self._starts_guard:
            if self._sandboxes.get(login) is start:
                del self._sandboxes[login]

    async def started(self, login: str, name: str, wait: float | None = None) -> Live:
        """The app, ready to serve, for a request: `AppStarting` once it waited `wait`
        seconds, by default `page_wait` for a page and `start_wait` for any other request,
        while the start goes on. It holds no thread.

        A start that failed raises its error, for `failed_for` seconds, then starts again.
        """
        cached = self._cache.get((login, name))
        if cached is not None:
            return cached
        start = self.start(login, name)
        if not start.done():
            if wait == 0:
                raise AppStarting(f"{login}/{name} is starting")
            loop = asyncio.get_running_loop()
            done = asyncio.Event()

            def wake(_: Future[Live]) -> None:
                # A loop that closed has no one waiting on it any more.
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(done.set)

            start.add_done_callback(wake)
            if wait is None:
                wait = self.page_wait if self._page.get() else self.start_wait
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(wait):
                    await done.wait()
            if not start.done():
                raise AppStarting(f"{login}/{name} is starting")
        live = _outcome(start)
        # A publish may have served another version as soon as the start ended.
        return self._cache.get((login, name)) or live

    async def declared_roles(self, login: str, name: str, wait: float | None = None) -> list[str]:
        """The roles the published app declares, none for an app without roles: as kept at
        each publish and start, or else from its start, which keeps them, waited for as
        `started` waits, and raising what it raises."""
        kept = await anyio.to_thread.run_sync(self.database.kept_roles, login, name)
        if kept is not None:
            return kept
        # Published before its roles were kept.
        live = await self.started(login, name, wait)
        return list(live.runtime.manifest["roles"])

    def current(self, login: str, name: str) -> Live | None:
        """The app as it is served now, if it is started. It never starts it."""
        return self._cache.get((login, name))

    def failed(self, login: str, name: str) -> SandboxError | None:
        """Why the app's last start failed, while that failure is kept. It never starts it."""
        key = (login, name)
        with self._starts_guard:
            start, failed = self._starts.get(key), self._failed_at.get(key)
        if start is None or failed is None or time.monotonic() - failed >= self.failed_for:
            return None
        # Kept just before its error is set: a start not yet done has none to give.
        if not start.done():
            return None
        error = start.exception()
        return error if isinstance(error, SandboxError) else None

    def start(
        self, login: str, name: str, *, again: bool = False, counted: bool = True
    ) -> Future[Live]:
        """The app's start: the one that runs, or that failed less than `failed_for` seconds
        ago unless `again`, else a new one, in a thread of its own. It always ends, with the
        app, `UnknownApp` or `SandboxError`.

        A new start a person causes, `counted`, counts as time the owner's sandbox is busy,
        and raises `OverBudget` at once, starting nothing and keeping nothing, once the
        month's sandbox time that holds it is used. The warm-up's counts nothing."""
        key = (login, name)
        with self._starts_guard:
            start = self._starts.get(key)
            failed = self._failed_at.get(key)
            if start is not None and (
                failed is None or (not again and time.monotonic() - failed < self.failed_for)
            ):
                return start
            refusal = self.over_budget(login) if counted else None
            if refusal is not None:
                raise refusal
            self._failed_at.pop(key, None)
            start = Future()
            self._starts[key] = start
            closes = self._closes
        thread = threading.Thread(
            target=self._run_start,
            args=(start, login, name, closes, counted),
            name=f"start {login}/{name}",
            # A start may wait minutes for a sandbox: the server never waits for it to stop.
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError as error:
            _log.exception("unexpected: %s/%s can't be started: %s", login, name, error)
            self._keep_failure(start, login, name)
            start.set_exception(StartFault(f"the server can't start it now ({error}): {_RETRY}"))
        return start

    def warm_up(self) -> list[Future[None]]:
        """Bring the sandbox of each owner who used their apps or the builder in the last
        `warm_days` up to date, then start their published apps, in the background: the most
        recent first, `OWNERS_WARMED_AT_ONCE` owners at a time, each app as `start` does. Each
        future ends when its owner is done.

        So after a deploy, no recent user waits for a sandbox or a build the server could have
        made beforehand, and a deploy doesn't set up the sandboxes nobody uses: any other
        owner's is brought up by their first request. What fails is logged, and the app's next
        request tries again.
        """
        owners: dict[str, list[str]] = {login: [] for login in self.database.sandbox_logins()}
        for info in self.published():
            owners.setdefault(info.login, []).append(info.name)
        try:
            # Every owner's sandbox is kept, warmed or not: the sweep removes the others.
            self.executor.sweep(set(owners))
        except SandboxError as error:
            # A daemon that is slow or away at boot is the operator's, not Cliquet's fault.
            _log.warning("the sandboxes weren't swept at boot: %s", error)
        except BaseException:
            _log.exception("unexpected: the sandboxes weren't swept at boot")
        since = self._clock() - timedelta(days=self.settings.warm_days)
        recent = [login for login in self.database.apps_used_since(since) if login in owners]
        waiting = deque((login, owners[login], Future[None]()) for login in recent)
        done = [one for _, _, one in waiting]
        closes = self._closes

        def warm() -> None:
            while True:
                try:
                    login, names, owner = waiting.popleft()
                except IndexError:
                    return
                try:
                    if self._closes == closes:
                        self._warm_owner(login, names, closes)
                except BaseException:
                    _log.exception("unexpected: %s didn't warm up at boot", login)
                finally:
                    owner.set_result(None)

        for _ in range(min(OWNERS_WARMED_AT_ONCE, len(waiting))):
            threading.Thread(target=warm, name="warm-up", daemon=True).start()
        return done

    def _warm_owner(self, login: str, names: list[str], closes: int) -> None:
        """Bring the owner's sandbox up to date, then start each app in `names`, one at a
        time, unless the apps are closed meanwhile. One app's failure stops no other. None of
        it counts as the owner's sandbox time, and an owner the month's sandbox time refuses,
        or everyone once the whole server's is used, is left as they are.

        A failure of Cliquet's own is logged where it happened, as unexpected: only the app's
        own, its build's problems, and an app that went meanwhile are warnings here."""
        if self.over_budget(login) is not None:
            return
        try:
            self._owner_sandbox(login)
        except SandboxError:
            return
        for name in names:
            if self._closes != closes:
                return
            try:
                _outcome(self.start(login, name, counted=False))
            except (UnknownApp, BuildProblems) as error:
                _log.warning("%s/%s didn't start at boot: %s", login, name, error)
            except SandboxError:
                pass  # Cliquet's own: logged as unexpected where it failed

    def _run_start(
        self, start: Future[Live], login: str, name: str, closes: int, counted: bool
    ) -> None:
        """Run the app's start, in its own thread, and end `start` with what it gave: as time
        the owner's sandbox is busy, when `counted`."""
        try:
            with self.busy(login) if counted else contextlib.nullcontext():
                live = self._start_once(start, login, name, closes)
        except UnknownApp as error:
            start.set_exception(error)
        except SandboxError as error:
            self._failed(start, login, name, error)
        except BaseException as error:
            start.set_exception(_start_fault(error))
        else:
            with self._starts_guard:
                if self._starts.get((login, name)) is start:
                    del self._starts[(login, name)]
            start.set_result(live)

    def _failed(self, start: Future[Live], login: str, name: str, error: SandboxError) -> None:
        """End `start` with `error`, once it is kept for the app's owner as an error of the
        app. Under the app's lock, as its failure is kept for a minute, and unless a publish,
        a forget or a close dropped that since: a version that went live meanwhile never gets
        the error of one that failed, and a start begun before a close keeps nothing."""
        key = (login, name)
        with self._app_lock(key):
            with self._starts_guard:
                current = self._starts.get(key) is start
            if current:
                self._keep_start_error(login, name, error)
        start.set_exception(error)

    def _keep_start_error(self, login: str, name: str, error: SandboxError) -> None:
        """Keep the app's failed start for its owner, as a failed call is kept, and give
        `error` the id it is kept under: the one a caller who isn't the owner reads. Cliquet's
        own fixed words, which every caller reads, keep nothing.

        It keeps the error's text and notes: the sandbox's answer says where the app's code
        raised, if it did. The server's own frames say nothing to the owner, and the log has
        them, read now, before a request raises the error afresh.
        """
        if not from_app(error):
            return
        notes = "".join(f"\n\n{note}" for note in getattr(error, "__notes__", ()))
        text = f"The app can't be started: {error}{notes}"
        try:
            error.error_id = self.database.keep_start_error(
                login,
                name,
                self.now(),
                text,
                # Logged once, as the id a caller reads, however often it fails the same way.
                lambda: report(error, f"{login}/{name} can't be started: {error}"),
            )
        except Exception:
            # Its caller is told an id the log has, as before.
            _log.exception("unexpected: %s/%s: the failed start wasn't kept", login, name)

    def _keep_failure(self, start: Future[Live], login: str, name: str) -> None:
        """Keep `start`, which failed, for `failed_for` seconds: unless the apps were closed,
        which forgets every start, or another start began since."""
        with self._starts_guard:
            if self._starts.get((login, name)) is start:
                self._failed_at[(login, name)] = time.monotonic()

    def _drop_failure(self, login: str, name: str) -> None:
        """Forget the app's failed start, after a publish or a forget. Expects the app's
        lock, under which a start fails."""
        key = (login, name)
        with self._starts_guard:
            if self._failed_at.pop(key, None) is not None:
                self._starts.pop(key, None)

    def _start_once(self, start: Future[Live], login: str, name: str, closes: int) -> Live:
        """Start the app, unless it is started, as `start`, begun after `closes` closes. A
        failure is kept under the app's lock, so a publish meanwhile drops it.

        A failure of the owner's sandbox is logged by `_owner_sandbox`, and one of the app's
        own start here: once, as unexpected, unless the app is to blame (its build's problems,
        or its code that failed), the app is unknown, or a close cut the start off."""
        key = (login, name)
        try:
            # The owner's sandbox first, with no lock held: setting a Sprite up again takes a
            # minute, and a forget or a delete of the app doesn't wait for it.
            self._owner_sandbox(login)
        except BaseException:
            with self._app_lock(key):
                self._keep_failure(start, login, name)
            raise
        with self._app_lock(key):
            try:
                if self._closes != closes:
                    raise SandboxError(f"the server stopped before it started: {_RETRY}")
                cached = self._cache.get(key)
                if cached is not None:
                    return cached
                began = time.monotonic()
                live = self._start(login, name)
                if self._closes != closes:
                    # The apps were closed during the start: it keeps nothing.
                    if self._cache.pop(key, None) is live:
                        live.runtime.close()
                    raise SandboxError(f"the server stopped while it started: {_RETRY}")
            except BaseException as error:
                self._keep_failure(start, login, name)
                if isinstance(error, AppFailed):
                    _log.warning("%s/%s can't be started: %s", login, name, error, exc_info=error)
                elif not (
                    isinstance(error, UnknownApp | BuildProblems)
                    or (isinstance(error, SandboxError) and self._closes != closes)
                ):
                    _log.error(
                        "unexpected: %s/%s can't be started: %s", login, name, error, exc_info=error
                    )
                raise
            _log.info("%s/%s started in %.1f s", login, name, time.monotonic() - began)
            return live

    def _start(self, login: str, name: str) -> Live:
        """Build the app's live version and keep it. Expects the app's lock."""
        info = self.database.app(login, name)
        if info is None or info.published_at is None:
            raise UnknownApp(f"{login} has no published app called {name}")
        repo = self.repo(login, name)
        code = repo.code()
        version = version_id(code)
        # Already up: a failure now is logged by `_start_once`, as the start's.
        client = self.executor.sandbox(login)
        built = client.put_version(login, name, version, code)
        look = accent(built.manifest), icon(built.manifest), title(built.manifest)
        if (info.accent, info.icon, info.title) != look:
            # An app published before the registry kept its look gets it here, at its start.
            self.database.set_look(login, name, *look)
            info = replace(info, accent=look[0], icon=look[1], title=look[2])
        runtime = Runtime(
            repo.folder,
            engine=SandboxEngine(
                client,
                login,
                name,
                version,
                built.manifest,
                repo.code,
                used=lambda: self._in_use(login),
            ),
            clock=self._clock,
            # The app's login is its owner's caller id.
            owner=login,
        )
        try:
            # A publish whose end of the old transactions failed left them open: end them now,
            # before anyone uses this version.
            if runtime.transactions.any_open():
                published = repo.published()
                if published is not None:
                    runtime.end_transactions(published)
        except BaseException:
            runtime.close()
            raise
        # Its roles are kept at each start too, so an app published before they were kept at
        # publish, or one whose publish stopped before, has them right once it started.
        self.database.keep_roles(login, name, list(built.manifest["roles"]))
        instructions = code.get("instructions.md", b"").decode()
        live = Live(info, version, runtime, instructions)
        self._cache[(login, name)] = live
        return live

    def held_roles(self, person: PersonInfo, live: Live) -> list[str]:
        """The app's roles this person may use, in the app's order: every role for its owner,
        and those of the memberships they accepted for anyone else."""
        roles = list(live.runtime.manifest["roles"])
        if person.login == live.info.login:
            return roles
        held = {
            membership.role
            for membership in self.database.memberships(person.id)
            if membership.app.login == live.info.login and membership.app.name == live.info.name
        }
        return [role for role in roles if role in held]

    def forget(self, login: str, name: str) -> None:
        """Stop serving the app, close its runtime, and forget a start that failed."""
        with self._app_lock((login, name)):
            live = self._cache.pop((login, name), None)
            if live is not None:
                live.runtime.close()
            self._drop_failure(login, name)

    def _replace(self, login: str, name: str, *, new_version: bool) -> Live:
        """Serve the version just published instead of the one before. For a `new_version`,
        every transaction open in the app ends, before anyone can begin one with it."""
        with self._app_lock((login, name)):
            self._drop_failure(login, name)
            old = self._cache.pop((login, name), None)
            if old is not None:
                old.runtime.close()
            with self.busy(login):
                live = self._start(login, name)
            if new_version:
                try:
                    live.runtime.end_transactions()
                except BaseException:
                    # Never serve the new version with transactions of the old one open.
                    self._cache.pop((login, name), None)
                    live.runtime.close()
                    raise
        return live

    def publish(
        self,
        login: str,
        name: str,
        code: dict[str, bytes],
        *,
        note: str,
        data: dict[str, bytes] | None = None,
        migration: bytes | None = None,
        migrated: Migrated | None = None,
        restores: str | None = None,
    ) -> Live:
        """Make `code` the app's live version, with its first data or a migration if given.

        `migrated` is what that migration already made of the live data, as the builder's
        checks ran it: it is taken while the data is still the one it ran on, and the
        migration runs again only on newer data. So a publish that doesn't race runs it once.

        A publish that `restores` the version of an earlier publish, named by its commit, runs
        no migration: it puts back the data from before each publish since, in the same commit
        as the code (`AppRepo.restored_data`).
        """
        if self.database.app(login, name) is None:
            raise UnknownApp(f"{login} has no app called {name}")
        repo = self.repo(login, name)
        if migration is not None and repo.head() is None:
            raise PublishRefused(["a migration is for an app that is live"])
        if restores is not None and (migration is not None or data is not None):
            raise PublishRefused(["a restore runs no migration, and brings no first data"])
        code = dict(code)
        code.pop(LOCK, None)
        code.pop(MIGRATION, None)
        if "app.py" not in code:
            raise PublishRefused(["the app has no app.py"])
        if data is not None:
            problem = data_problem("The first data", 0, sum(len(one) for one in data.values()))
            if problem is not None:
                raise PublishRefused([f"{problem} Start with less data."])
        version = version_id(code)
        version_code = {**code, MIGRATION: migration} if migration is not None else code
        with self.busy(login):
            client = self._owner_sandbox(login)
            built = client.put_version(login, name, version, version_code)
        engine = SandboxEngine(
            client,
            login,
            name,
            version,
            built.manifest,
            lambda: version_code,
            used=lambda: self.busy(login),
        )
        declared = built.manifest["files"]
        if data is not None:
            undeclared = sorted(file for file in data if not declares(declared, file))
            if undeclared:
                raise PublishRefused(
                    [f"the app doesn't declare the data file {file}" for file in undeclared]
                )
            if repo.head() is not None:
                raise PublishRefused(
                    [
                        "the app is live: its data changes through its actions, "
                        "or through a migration"
                    ]
                )
        elif migration is None and restores is None and repo.head() is not None:
            # Only declared files count toward the cap: a file kept but no longer declared
            # could be declared again later, past it.
            left = left_behind(repo.data_names(), declared)
            if left:
                raise PublishRefused(left)
        now = self._clock()
        for attempt in range(3):
            base = repo.head()
            every = migration is not None or restores is not None
            current = repo.data(repo.data_names(base), base) if every else repo.data(declared)
            new = data if data is not None else current
            if migration is not None:
                if migrated is not None and migrated.base == base:
                    new = migrated.data
                else:
                    new = migrate(engine, login, current)
            elif restores is not None:
                assert base is not None
                new = restore(repo, restores, base, current, declared)
            try:
                checked = engine.check(now, new)
            except AppFailed as error:
                raise PublishRefused([f"An invariant failed on the data: {error}"]) from error
            reasons = []
            if checked.refusal is not None:
                reasons.append(str(checked.refusal))
            reasons += [str(problem) for problem in checked.views]
            if reasons:
                raise PublishRefused(reasons)
            changes = changed(repo.code(), {**code, LOCK: built.lock})
            if data is not None:
                changes |= {
                    f"data/{file}": content for file, content in changed(current, data).items()
                }
            if every:
                changes |= {
                    f"data/{file}": content for file, content in changed(current, new).items()
                }
            if migration is not None:
                number = len(repo.migration_names()) + 1
                changes[f"migrations/{number:04d}-{version}.py"] = migration
            if not changes:
                break
            try:
                repo.publish(
                    base,
                    changes,
                    version=version,
                    note=note,
                    login=login,
                    when=now,
                    restores=restores,
                )
            except Moved:
                if attempt == 2:
                    raise
                continue
            break
        manifest = built.manifest
        self.database.mark_published(
            login,
            name,
            now,
            manifest["description"],
            accent=accent(manifest),
            icon=icon(manifest),
            title=title(manifest),
        )
        self.database.keep_roles(login, name, list(built.manifest["roles"]))
        # A new version went live, unless the code and the data were already these.
        return self._replace(login, name, new_version=bool(changes))

    def delete(self, login: str, name: str, *, in_sandbox: bool = True) -> None:
        """Forget an app, remove it from the sandbox, its repository and the database: under
        the app's lock, so a start that begins meanwhile, from a list or the warm-up, finds no
        app rather than build it again.

        The sandbox comes first, so that one still starting, `SandboxStarting`, leaves the
        app as it was. Not `in_sandbox`, the sandbox is left alone, never woken: the whole of
        it is destroyed next, as when an account is deleted."""
        client: SandboxClient | None = None
        if in_sandbox:
            with contextlib.suppress(SandboxError), self.sandbox(login) as found:
                client = found
        with self._app_lock((login, name)):
            live = self._cache.pop((login, name), None)
            if live is not None:
                live.runtime.close()
            self._drop_failure(login, name)
            if client is not None:
                with contextlib.suppress(SandboxError), self.busy(login):
                    client.delete_app(login, name)
            self.repo(login, name).delete()
            self.database.delete_app(login, name)

    def import_folder(
        self, folder: Path, login: str, name: str | None = None, *, email: str | None = None
    ) -> Live:
        """Publish the app in a folder made with `cliquet new`, as a new app.

        With `email`, an owner who has not signed in yet is claimed only by that email. An
        owner it makes is granted build.
        """
        folder = Path(folder)
        name = name or folder.name
        if not APP_NAME.fullmatch(name):
            raise ValueError(
                f"{name!r} is not an app name: pass --name with at most {MAX_APP_NAME} lower "
                "case letters, digits, - or _, starting with a letter"
            )
        if self.database.app(login, name) is not None:
            raise AppExists(f"{login} already has an app called {name}")
        code = bundle_of(folder)
        if "app.py" not in code:
            raise ValueError(f"{folder} has no app.py")
        code["app.py"] = set_sdk_source(code["app.py"].decode(), None).encode()
        data: dict[str, bytes] = {}
        data_folder = folder / "data"
        if data_folder.is_dir():
            for path in sorted(data_folder.rglob("*")):
                parts = path.relative_to(data_folder).parts
                if path.is_file() and "__pycache__" not in parts:
                    data[path.relative_to(data_folder).as_posix()] = path.read_bytes()
        new_owner = self.database.person_by_login(login) is None
        self.database.create_app(login, name, self._clock(), email=email)
        if new_owner:
            self.database.grant(login, Right.BUILD)
        try:
            return self.publish(login, name, code, note=f"Imported from {folder.name}", data=data)
        except BaseException:
            self.database.delete_app(login, name)
            self.repo(login, name).delete()
            if new_owner:
                self.database.withdraw(login, Right.BUILD)
            raise

    def close(self) -> None:
        """Close every cached runtime, and forget every start. A start in flight goes on in
        its thread, then keeps nothing."""
        with self._starts_guard:
            self._closes += 1
            self._starts.clear()
            self._failed_at.clear()
            self._sandboxes.clear()
        cached, self._cache = self._cache, {}
        for live in cached.values():
            live.runtime.close()

    def _app_lock(self, key: tuple[str, str]) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())


# What a start that failed on the server's side tells the caller to do.
_RETRY = "try again in a minute"


def _start_fault(error: BaseException) -> StartFault:
    """A start that failed for the server's reason, `error`, in Cliquet's fixed words."""
    return StartFault(f"the server failed to start it ({type(error).__name__}): {_RETRY}")


def _ended(start: Future[Any], seconds: float) -> bool:
    """Whether `start` ended, waiting for it `seconds` at most."""
    wait_for([start], timeout=seconds)
    return start.done()


def _outcome[T](start: Future[T]) -> T:
    """What a start that ended gave: the app, or its error raised afresh, so that the
    traceback of an error kept for a minute doesn't grow with each request."""
    error = start.exception()
    if error is None:
        return start.result()
    raise error.with_traceback(None)


def migrate(engine: SandboxEngine, login: str, current: dict[str, bytes]) -> dict[str, bytes]:
    """The app's data once the version's migration ran on `current`, a copy. Raises
    `PublishRefused` when it failed, its result was refused, or it would leave too much data."""
    try:
        result = engine.migrate(Caller(login, None), current)
    except AppFailed as error:
        raise PublishRefused([f"The migration failed: {error}"]) from error
    if isinstance(result, Refusal):
        raise PublishRefused([f"The migration's result was refused:\n{result}"])
    new = dict(current)
    for name, content in result.files.items():
        if content is None:
            new.pop(name, None)
        else:
            new[name] = content
    size = sum(len(content) for content in new.values())
    problem = data_problem("The migration", sum(len(one) for one in current.values()), size)
    if problem is not None:
        raise PublishRefused([f"{problem} Remove data through the app's actions first."])
    return new


def restore(
    repo: AppRepo, commit: str, base: str, current: dict[str, bytes], declared: list[str]
) -> dict[str, bytes]:
    """The data a restore of the version that `commit` published makes of `current`, all the
    data at `base`. Raises `PublishRefused` when it can't be put back, when the version doesn't
    declare one of its files, or when it would leave too much data."""
    try:
        new = repo.restored_data(commit, base)
    except CantRestore as error:
        raise PublishRefused([str(error)]) from error
    left = left_behind(sorted(new), declared, restored=True)
    if left:
        raise PublishRefused(left)
    size = sum(len(content) for content in new.values())
    problem = data_problem("The restore", sum(len(one) for one in current.values()), size)
    if problem is not None:
        raise PublishRefused([f"{problem} Remove data through the app's actions first."])
    return new


def left_behind(names: list[str], declared: list[str], *, restored: bool = False) -> list[str]:
    """What refuses a version without a migration that no longer declares some of the live
    data's files, `names`: one sentence each. A `restored` version can't remove them."""
    what = (
        "the restored version doesn't declare it. Restore a version that declares it, or "
        "publish a new version that removes it with a migration"
        if restored
        else "this version doesn't declare it. Declare it again, or remove it with a migration"
    )
    return [
        f'data/{name} is in the live data, but {what}: see read_guide("publishing").'
        for name in names
        if not declares(declared, name)
    ]
