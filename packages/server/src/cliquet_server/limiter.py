"""What one caller can make the server do: calls at once and calls per window, per key.

The server is one process, so the counts live in its memory and a restart forgets them. A
key names who is counted, such as an app on its address or a person on `/me/mcp`, never a
token's value. A call over a limit is refused at once, never queued: a refused caller holds
no thread, and other callers never wait for it.
"""

import contextlib
import logging
import math
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass

from cliquet_server.settings import Settings

_log = logging.getLogger(__name__)

# How many keys a limiter remembers before it forgets those whose window has ended.
_KEYS_KEPT = 10_000


@dataclass(frozen=True)
class Limit:
    """At most `per_window` calls begun in each `window` seconds, and, when set, at most
    `at_once` running together."""

    per_window: int
    window: float
    at_once: int | None = None


class Limited(Exception):
    """A call over a limit. `at_once` says it is the calls running together, not the calls
    in the window, and `retry_after` how many whole seconds to wait, at least one."""

    def __init__(self, key: str, limit: Limit, at_once: bool, retry_after: int) -> None:
        super().__init__(f"{key} is over its limit")
        self.key, self.limit, self.at_once, self.retry_after = key, limit, at_once, retry_after


class Limiter:
    """Counts each key's calls against one `Limit`, in a fixed window from its first call.

    Its lock is held for a moment, never across a call, so the limiter serves threads and
    the event loop alike. `clock` gives seconds, as `time.monotonic` does.
    """

    def __init__(self, limit: Limit, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self._clock = clock
        self._lock = threading.Lock()
        self._running: dict[str, int] = {}
        # A key's window: when it began, and how many calls began in it.
        self._windows: dict[str, tuple[float, int]] = {}

    @contextlib.contextmanager
    def call(self, key: str) -> Generator[None]:
        """Run one call counted for `key`, or raise `Limited` before it starts."""
        self.take(key, run=True)
        try:
            yield
        finally:
            self.release(key)

    def take(self, key: str, *, run: bool = False) -> None:
        """Count one call for `key` in its window, or raise `Limited`. With `run`, the call
        also counts as running until `release` ends it."""
        limit = self.limit
        with self._lock:
            now = self._clock()
            start, count = self._windows.get(key, (now, 0))
            ended = now - start >= limit.window
            if ended:
                start, count = now, 0
            running = self._running.get(key, 0)
            if run and limit.at_once is not None and running >= limit.at_once:
                raise Limited(key, limit, True, 1)
            if count >= limit.per_window:
                wait = max(1, math.ceil(start + limit.window - now))
                raise Limited(key, limit, False, wait)
            if ended:
                # A new window goes last, so the windows stay in the order they began.
                self._windows.pop(key, None)
            if key not in self._windows and len(self._windows) >= _KEYS_KEPT:
                # Before the key goes in, so the key just counted is never the one forgotten.
                self._forget(now)
            self._windows[key] = (start, count + 1)
            if run:
                self._running[key] = running + 1

    def release(self, key: str) -> None:
        """End one running call that `take` counted for `key` with `run`."""
        with self._lock:
            left = self._running[key] - 1
            if left:
                self._running[key] = left
            else:
                del self._running[key]

    def _forget(self, now: float) -> None:
        """Forget the windows that have ended, oldest first, then, if the table is still
        full, the oldest ones, to make room for one more: a forgotten key starts a new window.
        Call it with the lock held."""
        windows, window = self._windows, self.limit.window
        while windows:
            oldest = next(iter(windows))
            if now - windows[oldest][0] < window and len(windows) < _KEYS_KEPT:
                return
            del windows[oldest]


class Once:
    """Says yes to a key at most once every `every` seconds: what logs a refusal, or tells
    an app's owner, without repeating it for every call of a loop."""

    def __init__(self, every: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._every = every
        self._clock = clock
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def first(self, key: str) -> bool:
        """True when `key` wasn't seen in the last `every` seconds, and remember it now."""
        with self._lock:
            now = self._clock()
            last = self._last.get(key)
            if last is not None and now - last < self._every:
                return False
            self._last.pop(key, None)
            self._last[key] = now
            while len(self._last) > _KEYS_KEPT:
                del self._last[next(iter(self._last))]
            return True


@contextlib.contextmanager
def admitted(limiter: Limiter, key: str) -> Generator[Limited | None]:
    """Run the block as one call counted for `key`, yielding None, or yield why it is
    refused, and count nothing."""
    try:
        limiter.take(key, run=True)
    except Limited as refusal:
        yield refusal
        return
    try:
        yield None
    finally:
        limiter.release(key)


def why(refusal: Limited, what: str) -> str:
    """The sentences that tell an agent which limit `what`, such as "this app", hit, and
    when to try again."""
    limit = refusal.limit
    if refusal.at_once:
        return (
            f"Too many calls to {what} at once: at most {limit.at_once} run together. "
            "Wait for one to finish, then try again."
        )
    return (
        f"Too many calls to {what}: at most {limit.per_window} {_per(limit.window)}. "
        f"Try again in {_seconds(refusal.retry_after)}."
    )


def why_runs(refusal: Limited) -> str:
    """The sentences that tell an agent its owner started too many runs in their sandbox."""
    return (
        f"You started {refusal.limit.per_window} runs in your sandbox in the last hour, the "
        "most there can be: each publish, check, preview, test run, tried view and set of "
        "screenshots is one. "
        f"Try again in {_seconds(refusal.retry_after)}."
    )


def logged(refusal: Limited, once: Once, who: str | None = None) -> None:
    """Log a refusal once per key and minute, with `who` was refused when the key doesn't
    say it, never a token's value, so it shows in the server's log without filling it."""
    if once.first(refusal.key):
        kind = "at once" if refusal.at_once else "in its window"
        by = "" if who is None else f": {who}"
        _log.warning("refused %s, over its limit of calls %s%s", refusal.key, kind, by)


def _per(window: float) -> str:
    """A window as a person reads it: a minute, an hour, or so many seconds."""
    if window == 60:
        return "a minute"
    if window == 3600:
        return "an hour"
    return f"every {_seconds(math.ceil(window))}"


def _seconds(seconds: int) -> str:
    """So many seconds, or minutes once it is more than two."""
    if seconds > 120:
        minutes = math.ceil(seconds / 60)
        return f"{minutes} minutes"
    return "1 second" if seconds == 1 else f"{seconds} seconds"


class Limits:
    """The server's limiters, from its settings: calls per key, at once and per minute, and
    each owner's runs in their sandbox per hour, with what logs and tells of a refusal."""

    def __init__(
        self,
        *,
        calls_at_once: int,
        calls_per_minute: int,
        sandbox_runs_per_hour: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.calls = Limiter(Limit(calls_per_minute, 60, calls_at_once), clock=clock)
        self.sandbox_runs = Limiter(Limit(sandbox_runs_per_hour, 3600), clock=clock)
        self.logged = Once(60, clock=clock)
        self.told = Once(3600, clock=clock)

    @classmethod
    def of(cls, settings: Settings) -> Limits:
        """The limits the server's settings give."""
        return cls(
            calls_at_once=settings.calls_at_once,
            calls_per_minute=settings.calls_per_minute,
            sandbox_runs_per_hour=settings.sandbox_runs_per_hour,
        )
