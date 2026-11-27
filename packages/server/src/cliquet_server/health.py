"""Whether the server can work: what `/healthz` checks."""

import logging
import os
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

import anyio

from cliquet_server.db import Database

logger = logging.getLogger(__name__)

# How long the database may take to answer, and how long a result is reused, in seconds.
DEADLINE = 2.0
REUSE_FOR = 5.0
# The least free space the data disk may have, in bytes.
MIN_FREE = 10 * 1024 * 1024


class Health:
    """Checks the database and its disk, the parts the server can't work without.

    The sandbox service is not checked: with one machine, a failed check takes every app off
    the host's proxy, so an outage at Sprites must not fail it.
    """

    def __init__(self, database: Database, data_dir: Path) -> None:
        self.database = database
        self.data_dir = data_dir
        self.deadline = DEADLINE
        self.reuse_for = REUSE_FOR
        # The clock of the reuse window: a test moves it on.
        self.monotonic: Callable[[], float] = time.monotonic
        # Its own worker thread, so a slow database never holds the pages' threads.
        self._limiter = anyio.CapacityLimiter(1)
        self._last: tuple[float, list[str]] | None = None
        # Set when the ping that missed its deadline ends: none starts meanwhile.
        self._abandoned: threading.Event | None = None

    async def failing(self) -> list[str]:
        """The names of the parts that fail, none when the server is healthy."""
        now = self.monotonic()
        if self._last is not None and now - self._last[0] < self.reuse_for:
            return self._last[1]
        failing = []
        for name, problem in (("database", await self._database()), ("disk", self._disk())):
            if problem is not None:
                logger.warning("the health check fails on the %s: %s", name, problem)
                failing.append(name)
        self._last = (now, failing)
        return failing

    async def _database(self) -> str | None:
        if self._abandoned is not None and not self._abandoned.is_set():
            return "an earlier check is still waiting for it"
        done = threading.Event()

        def ping() -> None:
            try:
                self.database.ping()
            finally:
                done.set()

        try:
            with anyio.fail_after(self.deadline):
                await anyio.to_thread.run_sync(ping, limiter=self._limiter, abandon_on_cancel=True)
        except TimeoutError:
            self._abandoned = done
            return f"it did not answer within {self.deadline:g} seconds"
        except Exception as error:
            return f"{type(error).__name__}: {error}"
        return None

    def _disk(self) -> str | None:
        try:
            if not hasattr(os, "statvfs"):  # not on Windows: only the free space is known
                free = shutil.disk_usage(self.data_dir).free
            else:
                stat = os.statvfs(self.data_dir)
                if stat.f_flag & os.ST_RDONLY:
                    return f"{self.data_dir} is mounted read-only"
                free = stat.f_bavail * stat.f_frsize
        except OSError as error:
            return f"{type(error).__name__}: {error}"
        if free < MIN_FREE:
            return f"{self.data_dir} has {free:,} bytes free, less than {MIN_FREE:,}"
        return None
