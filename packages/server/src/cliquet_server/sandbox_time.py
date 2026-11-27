"""Each owner's sandbox time this month, and the monthly budgets that hold it.

What counts is the time an owner's sandbox is busy: the wall time during which at least one
call the server makes for a person runs in it, so calls that overlap count once. What the
server starts on its own, its warm-up at boot and an executor's probes, counts nothing. A
month is a calendar month in UTC, and an interval across its end is split between the two.

The counts live in memory, where every check reads them, so no request waits for the
database. They are written there at most once a minute per owner, and read back as the
server starts: a restart keeps them, and a crash loses a minute at most.

An owner with the right plus has the Plus budget. The owners who hold it are read with the
counts, at start and at each write, since `cliquet-server grant` changes them from another
process: a grant or a withdrawal holds within a minute.
"""

import contextlib
import logging
import math
import threading
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError

from cliquet.tools import ASK_FOR_FILE, COMMIT_TRANSACTION, ROLLBACK_TRANSACTION
from cliquet_server.db import Database, Right
from cliquet_server.executor import SandboxFault
from cliquet_server.limiter import Once
from cliquet_server.settings import Settings

_log = logging.getLogger(__name__)

# How often the counts are written, in seconds: each owner's at most once in that time.
WRITTEN_EVERY = 60.0
# How often a refusal is logged, in seconds, per owner and for the whole server.
LOGGED_EVERY = 3600.0
# The built-in tools that never reach the sandbox: they read git or the transaction's record.
_OUTSIDE = frozenset(
    {
        "app_history",
        ASK_FOR_FILE,
        "app_begin_transaction",
        "app_transaction_calls",
        COMMIT_TRANSACTION,
        ROLLBACK_TRANSACTION,
    }
)
# The key the whole server's refusals are logged under: no login has a space.
_SERVER = "the server"


def runs_app_code(tool: str) -> bool:
    """Whether a call of one of an app's tools may run in its owner's sandbox: every action,
    query and view, and the built-ins that run one. A read of a resource, the renderer's
    static page, never does."""
    return tool not in _OUTSIDE and "://" not in tool


def month_of(moment: datetime) -> str:
    """The calendar month, in UTC, that a moment is in: `YYYY-MM`."""
    return f"{moment.astimezone(UTC):%Y-%m}"


def next_month(moment: datetime) -> datetime:
    """The first moment of the calendar month, in UTC, after the one a moment is in."""
    moment = moment.astimezone(UTC)
    if moment.month == 12:
        return datetime(moment.year + 1, 1, 1, tzinfo=UTC)
    return datetime(moment.year, moment.month + 1, 1, tzinfo=UTC)


def _month_start(moment: datetime) -> datetime:
    """The first moment of the calendar month, in UTC, that a moment is in."""
    moment = moment.astimezone(UTC)
    return datetime(moment.year, moment.month, 1, tzinfo=UTC)


def _previous_month(moment: datetime) -> str:
    """The calendar month, in UTC, before the one a moment is in."""
    return month_of(_month_start(moment) - timedelta(days=1))


def reset_words(reset: datetime) -> str:
    """When the month's budgets reset, as a person reads it."""
    return f"{reset.day} {reset:%B %Y} at 00:00 UTC"


class OverBudget(SandboxFault):
    """A call refused before it starts: the month's sandbox time of its owner's sandbox, or of
    the whole server, is used. Its text is what the app's owner reads, `others` what anyone
    else does, and `retry_after` how many whole seconds are left until the budgets reset.
    `server` says it is the whole server's."""

    def __init__(self, owner: str, others: str, retry_after: int, *, server: bool) -> None:
        super().__init__(owner)
        self.others, self.retry_after, self.server = others, retry_after, server

    def told(self, owner: bool) -> str:
        """What a caller reads: the owner's words, or anyone else's."""
        return str(self) if owner else self.others


@dataclass
class _Busy:
    """The calls running in one owner's sandbox, and since when it is counted busy."""

    calls: int
    since: datetime


class SandboxTime:
    """Counts each owner's sandbox time this month against their budget: `per_plus_owner`
    minutes for an owner with the right plus, `per_owner` for any other, or for everyone
    while `per_plus_owner` is None. The whole server's, the sum of the owners', counts
    against `per_server`. A budget of None is no budget. `clock` is the server's.

    Its lock is held for a moment, never across a call, so it serves threads and the event
    loop alike. The counts are read once, here, and written only by `write`.
    """

    def __init__(
        self,
        database: Database,
        *,
        per_owner: int | None,
        per_server: int | None,
        base_url: str,
        clock: Callable[[], datetime],
        per_plus_owner: int | None = None,
    ) -> None:
        self.per_owner, self.per_server = per_owner, per_server
        self.per_plus_owner = per_plus_owner
        self._database = database
        self._account = f"{base_url}/account"
        self._clock = clock
        self._lock = threading.Lock()
        # One write at a time: the writer's, or the last one at shutdown.
        self._writing = threading.Lock()
        self._busy: dict[str, _Busy] = {}
        # The month counted, each owner's seconds in it, and their sum.
        now = clock()
        self._month = month_of(now)
        # A month before the previous one is never read again.
        database.drop_sandbox_time_before(_previous_month(now))
        self._used: dict[str, float] = dict(database.sandbox_time(self._month))
        self._total = sum(self._used.values())
        # The seconds not written yet, by owner and month.
        self._pending: dict[tuple[str, str], float] = {}
        self._plus = self._plus_owners()
        self._logged = Once(LOGGED_EVERY)
        self._stopping = threading.Event()
        self._writer: threading.Thread | None = None

    @classmethod
    def of(
        cls, settings: Settings, database: Database, clock: Callable[[], datetime]
    ) -> SandboxTime | None:
        """The meter the settings ask for: None when they set no budget, so nothing is
        counted or kept."""
        per_owner = settings.sandbox_minutes_per_owner_month
        per_server = settings.sandbox_minutes_per_month
        if per_owner is None and per_server is None:
            return None
        return cls(
            database,
            per_owner=per_owner,
            per_server=per_server,
            base_url=settings.base_url,
            clock=clock,
            per_plus_owner=settings.sandbox_minutes_per_plus_owner_month,
        )

    @contextlib.contextmanager
    def busy(self, login: str) -> Generator[None]:
        """Count the block as time the owner's sandbox is busy: once, whatever else runs in
        it meanwhile."""
        with self._lock:
            found = self._busy.get(login)
            if found is None:
                self._busy[login] = _Busy(1, self._clock())
            else:
                found.calls += 1
        try:
            yield
        finally:
            with self._lock:
                found = self._busy[login]
                found.calls -= 1
                if not found.calls:
                    del self._busy[login]
                    self._count(login, found.since, self._clock())

    def is_plus(self, login: str) -> bool:
        """Whether the owner has the Plus budget: they hold plus, and it is set."""
        return login in self._plus

    def budget(self, login: str) -> int | None:
        """The owner's minutes of sandbox time a month, or None for no budget."""
        return self.per_plus_owner if self.is_plus(login) else self.per_owner

    def used(self, login: str) -> float:
        """The seconds the owner's sandbox was busy this month, a call running now included."""
        now = self._clock()
        with self._lock:
            self._roll(month_of(now))
            return self._used.get(login, 0.0) + self._running(login, now)

    def refused(self, login: str) -> OverBudget | None:
        """Why a new call into the owner's sandbox is refused, or None when the month's time
        is left for it: the whole server's first, then the owner's. A refusal is logged once
        an hour, the whole server's as an error, an owner's as a warning."""
        now = self._clock()
        with self._lock:
            self._roll(month_of(now))
            total = self._total + sum(self._running(one, now) for one in self._busy)
            mine = self._used.get(login, 0.0) + self._running(login, now)
        reset = next_month(now)
        when = reset_words(reset)
        wait = max(1, math.ceil((reset - now).total_seconds()))
        if self.per_server is not None and total >= self.per_server * 60:
            if self._logged.first(_SERVER):
                # Fixed words, which an operator searches the log for: never `unexpected:`.
                _log.error(
                    "sandbox budget: the server used its %d minutes of sandbox time for %s, "
                    "so every call into a sandbox is refused until %s",
                    self.per_server,
                    month_of(now),
                    when,
                )
            words = (
                "This server has used this month's sandbox time, so no app's code runs until "
                f"it resets on {when}."
            )
            return OverBudget(words, words, wait, server=True)
        budget = self.budget(login)
        if budget is not None and mine >= budget * 60:
            if self._logged.first(login):
                _log.warning(
                    "%s's apps used their %d minutes of sandbox time for %s: their calls are "
                    "refused until %s",
                    login,
                    budget,
                    month_of(now),
                    when,
                )
            owner = (
                f"Your apps have used this month's sandbox time, {budget} minutes, so "
                f"no call runs in them until it resets on {when}. The Account tab of your "
                f"settings shows it: {self._account}"
            )
            others = (
                f"The apps of {login} have used this month's sandbox time, so no call runs in "
                f"them until it resets on {when}."
            )
            return OverBudget(owner, others, wait, server=False)
        return None

    def write(self) -> None:
        """Write the time counted since the last write, a call running now's included, to
        the database, and read the Plus owners again. What fails to be written is kept for
        the next write."""
        with self._writing:
            with self._lock:
                now = self._clock()
                for login, found in self._busy.items():
                    # A call that runs for hours is written as it goes.
                    if now > found.since:
                        self._count(login, found.since, now)
                        found.since = now
                pending, self._pending = self._pending, {}
            whole = {key: int(seconds) for key, seconds in pending.items() if seconds >= 1}
            try:
                if whole:
                    self._database.add_sandbox_time(whole)
            except SQLAlchemyError as error:
                # Only the counts of a crash meanwhile are lost: the calls go on.
                _log.warning("the sandbox time couldn't be written: %s", error)
                whole = {}
            with self._lock:
                for key, seconds in pending.items():
                    left = seconds - whole.get(key, 0)
                    if left > 0:
                        self._pending[key] = self._pending.get(key, 0.0) + left
            # Read once the counts are kept, so a failure here loses none of them.
            try:
                self._plus = self._plus_owners()
            except SQLAlchemyError as error:
                # The Plus owners read last hold until the next write.
                _log.warning("the Plus owners couldn't be read: %s", error)

    def start(self) -> None:
        """Write the counts once a minute, in a thread of its own, until `close`."""
        self._stopping.clear()
        self._writer = threading.Thread(target=self._write_loop, name="sandbox-time", daemon=True)
        self._writer.start()

    def close(self) -> None:
        """Stop writing once a minute, and write what is left."""
        self._stopping.set()
        if self._writer is not None:
            self._writer.join(timeout=5)
        self.write()

    def _write_loop(self) -> None:
        while not self._stopping.wait(WRITTEN_EVERY):
            try:
                self.write()
            except Exception:
                _log.exception("unexpected: the sandbox time wasn't written")

    def _plus_owners(self) -> frozenset[str]:
        """The owners with the Plus budget, from the database: none while it is unset."""
        if self.per_plus_owner is None:
            return frozenset()
        return self._database.logins_with(Right.PLUS)

    def _running(self, login: str, now: datetime) -> float:
        """The seconds of this month the owner's sandbox has been busy, as calls run in it
        now. Expects the lock."""
        found = self._busy.get(login)
        if found is None:
            return 0.0
        since = max(found.since, _month_start(now))
        return max(0.0, (now - since).total_seconds())

    def _count(self, login: str, start: datetime, end: datetime) -> None:
        """Count the owner's sandbox busy from `start` to `end`, split by calendar month.
        Expects the lock."""
        while start < end:
            month = month_of(start)
            until = min(end, next_month(start))
            seconds = (until - start).total_seconds()
            key = (login, month)
            self._pending[key] = self._pending.get(key, 0.0) + seconds
            self._roll(month)
            if month == self._month:
                self._used[login] = self._used.get(login, 0.0) + seconds
                self._total += seconds
            start = until

    def _roll(self, month: str) -> None:
        """Count `month` from nothing, when it is later than the month counted. Expects the
        lock."""
        if month > self._month:
            self._month, self._used, self._total = month, {}, 0.0
