"""Previews: a draft served on a copy of the data, one per owner and app."""

from __future__ import annotations

import hashlib
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cliquet import views
from cliquet.context import Caller
from cliquet.mcp_server import AppServer
from cliquet.runtime import EVERYONE, Runtime
from cliquet.store import scratch_repository
from cliquet.tools import app_role

if TYPE_CHECKING:
    from .workshop import Build

# What the answers say when the current data no longer matches the preview's start.
DATA_CHANGED = (
    "The current data changed since this preview started. "
    "Call start_preview to start again from it."
)


MIGRATION_CHANGED = (
    "migrate.py changed since this preview started, and the preview doesn't run it again. "
    "Call start_preview to start again from the current data."
)


class PreviewEnded(Exception):
    """The preview closed before the call could run on it."""


class PreviewSession:
    """One draft on a copy of the data: one runtime, and a server for each role it served.

    When the code changes, the runtime takes the new version's engine on the same data. The
    copy of the data is removed once the preview closed and no call uses it.
    """

    def __init__(
        self,
        login: str,
        app: str,
        build: Build,
        data: dict[str, bytes],
        app_py: Path,
        fingerprint: str,
    ) -> None:
        self.id = secrets.token_hex(16)
        self.login = login
        self.app = app
        self.fingerprint = fingerprint
        self.migration = migration_digest(build.migration)
        self.used = time.monotonic()
        self._lock = threading.Lock()
        # One version change at a time, so the build and the runtime's engine stay alike.
        self._following = threading.Lock()
        self._closed = False
        self._users = 0
        self._folder: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(
            prefix="cliquet-preview-"
        )
        try:
            data_dir = scratch_repository(Path(self._folder.name), data)
            # Only the app's owner opens its preview, in each role in turn.
            self.runtime = Runtime(app_py, data_dir=data_dir, engine=build.engine, owner=EVERYONE)
        except BaseException:
            self._folder.cleanup()
            raise
        self._build = build
        self._servers: dict[str | None, AppServer] = {}
        # The draft's code revision that didn't build, and why: a follow answers from it until
        # the code is written again or checked, instead of building the same code again.
        self.failure: tuple[int, str] | None = None
        # How many checks forgot it: a follow that began before one keeps no failure.
        self.checks = 0

    @property
    def build(self) -> Build:
        """The draft's build the preview runs now."""
        with self._lock:
            return self._build

    @property
    def version(self) -> str:
        """The draft's version the preview runs now."""
        return self.build.version

    @property
    def manifest(self) -> dict[str, Any]:
        """The draft's manifest, as this preview runs it."""
        return self.build.manifest

    @property
    def roles(self) -> list[str]:
        """The draft's roles, as this preview runs it."""
        return list(self.manifest["roles"])

    def follow(self, build: Build) -> None:
        """Run `build`, the draft as read at a later code revision than the one the preview
        runs: on the same data, as a new version if the code changed. A build read earlier
        changes nothing, so two calls that race keep the newer.

        Open transactions end, as at a publish. A preview that closed is left as it is: the
        next call on it says it ended.
        """
        with suppress(PreviewEnded), self._following, self._holding():
            current = self.build
            if build.revision <= current.revision:
                return
            changed = build.version != current.version
            if changed:
                # The runtime waits for the write that holds its lock: not under `_lock`.
                self.runtime.new_version(build.engine)
            with self._lock:
                self._build = build
                if changed:
                    # Each server lists the old version's tools.
                    self._servers = {}

    @contextmanager
    def using(self, role: str | None) -> Generator[AppServer]:
        """The server for this role, made the first time the role is used, for one call.

        The role is checked against the version the preview runs: `NoSuchRole` when it isn't
        one of the draft's.
        """
        with self._holding():
            with self._lock:
                app_role(self._build.manifest, role)
                server = self._servers.get(role)
                if server is None:
                    server = AppServer(self.runtime, Caller("preview", role))
                    self._servers[role] = server
            yield server

    @contextmanager
    def _holding(self) -> Generator[None]:
        """Keep the copy of the data until the block ends. `PreviewEnded` if it closed."""
        with self._lock:
            if self._closed:
                raise PreviewEnded("this preview has ended")
            self._users += 1
        try:
            yield
        finally:
            with self._lock:
                self._users -= 1
                last = self._closed and self._users == 0
            if last:
                self._remove()

    def call(self, role: str | None, name: str, arguments: dict[str, Any], **options: Any) -> Any:
        """Call one of the app's tools as `role`, on the preview."""
        with self.using(role) as server:
            return server.call(name, arguments, **options)

    def data(self) -> dict[str, bytes]:
        """The preview's data files now, by name. `PreviewEnded` if it closed."""
        with self._holding():
            store = self.runtime.store
            return store.read_declared(store.head(), self.manifest["files"])

    def close(self) -> None:
        """Close the preview, and remove the copy of the data once no call uses it."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            last = self._users == 0
        if last:
            self._remove()

    def _remove(self) -> None:
        self.runtime.close()
        self._folder.cleanup()


def migration_digest(content: bytes | None) -> str:
    """What names a draft's migrate.py, or its absence."""
    return "" if content is None else hashlib.sha256(content).hexdigest()


class Previews:
    """The previews that are open, one per owner and app."""

    def __init__(self, idle_seconds: float = 1800, limit: int = 20) -> None:
        self.idle_seconds = idle_seconds
        self.limit = limit
        self._sessions: dict[tuple[str, str], PreviewSession] = {}
        # The apps whose preview a publish ended, until the next starts or the app is deleted:
        # the page the person still has open then says why its calls stopped working.
        self._published: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def start(
        self,
        login: str,
        app: str,
        build: Build,
        data: dict[str, bytes],
        app_py: Path,
        fingerprint: str,
    ) -> PreviewSession:
        """A new preview of the draft on `data`, in place of any open for this app."""
        session = PreviewSession(login, app, build, data, app_py, fingerprint)
        with self._lock:
            old = self._sessions.pop((login, app), None)
            self._published.discard((login, app))
            closed = self._expire()
            self._sessions[(login, app)] = session
            closed += self._trim()
        for one in ([old] if old is not None else []) + closed:
            one.close()
        return session

    def find(self, login: str, app: str) -> PreviewSession | None:
        """The app's open preview, if there is one. Finding it counts as using it."""
        return self._lookup(lambda: self._sessions.get((login, app)))

    def open_for(self, login: str) -> set[str]:
        """The apps of `login` with a preview open and not idle too long, without counting
        that as using them: a console page that only links to a preview keeps none alive."""
        now = time.monotonic()
        with self._lock:
            return {
                app
                for (owner, app), session in self._sessions.items()
                if owner == login and now - session.used < self.idle_seconds
            }

    def get(self, session_id: str) -> PreviewSession | None:
        """The open preview with this id, as a shown view names it."""
        return self._lookup(
            lambda: next((one for one in self._sessions.values() if one.id == session_id), None)
        )

    def _lookup(self, pick: Callable[[], PreviewSession | None]) -> PreviewSession | None:
        """The session `pick` finds among those still open, marked as used."""
        with self._lock:
            closed = self._expire()
            session = pick()
            if session is not None:
                session.used = time.monotonic()
        for one in closed:
            one.close()
        return session

    def forget_failure(self, login: str, app: str) -> None:
        """Let the app's preview build the draft again at its next follow, without counting it
        as used: after a check, which may find the draft builds now."""
        with self._lock:
            session = self._sessions.get((login, app))
        if session is not None:
            session.checks += 1
            session.failure = None

    def stop(self, login: str, app: str) -> bool:
        """Close the app's preview, and forget that a publish ended the last one. True when
        one was open."""
        with self._lock:
            session = self._sessions.pop((login, app), None)
            self._published.discard((login, app))
        if session is None:
            return False
        session.close()
        return True

    def end_at_publish(self, login: str, app: str) -> bool:
        """Close the app's preview, which `ended_by_publish` then says until the next starts.
        With none open, what it said stays. True when one was open."""
        with self._lock:
            session = self._sessions.pop((login, app), None)
            if session is not None:
                self._published.add((login, app))
        if session is None:
            return False
        session.close()
        return True

    def ended_by_publish(self, login: str, app: str) -> bool:
        """Whether a publish ended the app's last preview, and none started since."""
        with self._lock:
            return (login, app) in self._published

    def close(self) -> None:
        """Close every preview."""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._published.clear()
        for session in sessions:
            session.close()

    def _expire(self) -> list[PreviewSession]:
        now = time.monotonic()
        expired = []
        for key, session in list(self._sessions.items()):
            if now - session.used >= self.idle_seconds:
                expired.append(self._sessions.pop(key))
        return expired

    def _trim(self) -> list[PreviewSession]:
        trimmed = []
        while len(self._sessions) > self.limit:
            key = min(self._sessions, key=lambda k: self._sessions[k].used)
            trimmed.append(self._sessions.pop(key))
        return trimmed


def rewrite_calls(
    screen: dict[str, Any], session_id: str, app_py: str, role: str | None = None
) -> dict[str, Any]:
    """A copy of a screen whose calls to the app, views included, go to the builder's
    `preview_call` tool, naming the preview, the digest of the app.py that laid the screen
    out, and the role the screen was shown as."""
    return views.relay_calls(screen, PREVIEW_ROUTES, preview_fixed(session_id, app_py, role))


# A screen shown by the builder calls its `preview_call` tool, for an action and a view alike.
PREVIEW_ROUTES = {"action": ("preview_call", "tool"), "view": ("preview_call", "tool")}


def preview_fixed(session_id: str, app_py: str, role: str | None) -> dict[str, Any]:
    """What every call of a screen shown on a preview names: the preview, the digest of the
    app.py that laid the screen out, and the role."""
    fixed: dict[str, Any] = {"preview": session_id, "app_py": app_py}
    if role is not None:
        fixed["role"] = role
    return fixed
