"""A file an upload panel sends in parts, kept on disk until its last part.

A panel is a trusted page in the conversation. It reads the file the person chose and sends it
to a tool in parts, each small enough for one MCP message. `Parts` keeps one file per upload,
outside wherever the file goes, and hands the whole file over at its last part. An upload
nobody finishes is dropped, so parts never fill the disk. The builder's upload panel and the
panel that gives an action its file share it.
"""

import base64
import binascii
import os
import secrets
import threading
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from pathlib import Path

from cliquet.limits import MB

# The bytes of one part. In base64, inside a tool call, it stays under `MAX_BODY`.
PART = 2 * MB
# An upload whose last part came longer ago than this, in seconds, is dropped.
IDLE = 10 * 60
# The uploads one person may have on their way at once: a new one drops the oldest.
OPEN_UPLOADS = 4

ENDED = "This upload has ended, and nothing was written. Choose the file again."


class PartRefused(Exception):
    """A part can't be taken. The message says why, and what to do next."""


@dataclass(frozen=True)
class Received:
    """What one part left: the upload it goes on, how much of it arrived, and, after its last
    part, the whole `file`, which the caller moves where it goes, then removes."""

    upload: str
    received: int
    file: Path | None = None


@dataclass
class _Upload:
    """One upload on its way: whose, what it is for, its size, and how much arrived."""

    who: str
    what: Hashable
    size: int
    received: int
    file: Path
    touched: float


class Parts:
    """The uploads on their way in parts, for every person, of at most `limit` bytes each:
    one over it is refused with `too_big`."""

    def __init__(
        self, root: Path, limit: int, too_big: str, clock: Callable[[], float] = time.time
    ) -> None:
        self.root = Path(root)
        self.limit = limit
        self.too_big = too_big
        self.clock = clock
        self._lock = threading.Lock()
        self._open: dict[str, _Upload] = {}

    def receive(
        self,
        who: str,
        what: Hashable,
        size: int,
        offset: int,
        data: str,
        upload: str | None,
        check: Callable[[], None],
    ) -> Received:
        """Take one part of a file of `size` bytes for `what`, such as where it goes, sent by
        `who`: the first starts an upload, once `check` raised nothing, and the last hands the
        whole file over. Raises `PartRefused`, and what `check` raises."""
        part = _decoded(data)
        if len(part) > PART:
            raise PartRefused(f"A part is at most {PART:,} bytes; this one is {len(part):,}.")
        with self._lock:
            if upload is None:
                one, upload = self._start(who, what, size, offset, check), secrets.token_urlsafe(16)
                self._open[upload] = one
            else:
                one = self._next(upload, who, what, size, offset)
            if one.received + len(part) > one.size:
                self._drop(upload)
                raise PartRefused(
                    f"The file is bigger than the {one.size:,} bytes it said, and nothing was "
                    "written. Choose it again."
                )
            with one.file.open("ab") as out:
                out.write(part)
            one.received += len(part)
            one.touched = self.clock()
            if one.received < one.size:
                return Received(upload, one.received)
            del self._open[upload]
        return Received(upload, one.received, one.file)

    def _start(
        self, who: str, what: Hashable, size: int, offset: int, check: Callable[[], None]
    ) -> _Upload:
        """A new upload, once `check` passes and old ones are dropped."""
        if offset != 0:
            raise PartRefused(ENDED)
        if size > self.limit:
            raise PartRefused(self.too_big)
        if size < 1:
            raise PartRefused("The file is empty. Choose a file with something in it.")
        check()
        self._sweep(who)
        self.root.mkdir(parents=True, exist_ok=True)
        file = self.root / f"{secrets.token_hex(16)}.part"
        file.touch()
        return _Upload(who, what, size, 0, file, self.clock())

    def _next(self, upload: str, who: str, what: Hashable, size: int, offset: int) -> _Upload:
        """The upload this part continues, or a refusal that ends it."""
        one = self._open.get(upload)
        if one is None or one.who != who:
            raise PartRefused(ENDED)
        if one.touched < self.clock() - IDLE:
            self._drop(upload)
            raise PartRefused(ENDED)
        if (one.what, one.size, one.received) != (what, size, offset):
            self._drop(upload)
            raise PartRefused(ENDED)
        return one

    def drop(self, who: str) -> None:
        """Drop every upload of this person on its way, with its parts."""
        with self._lock:
            for upload, one in list(self._open.items()):
                if one.who == who:
                    self._drop(upload)

    def _sweep(self, who: str) -> None:
        """Drop every idle upload, this person's oldest past the few they may keep, and parts
        left on the disk by an earlier run."""
        idle = self.clock() - IDLE
        for upload, one in list(self._open.items()):
            if one.touched < idle:
                self._drop(upload)
        # Oldest first; among uploads touched at once, in the order they started.
        theirs = [upload for upload, one in self._open.items() if one.who == who]
        theirs.sort(key=lambda upload: self._open[upload].touched)
        for upload in theirs[: max(0, len(theirs) - OPEN_UPLOADS + 1)]:
            self._drop(upload)
        kept = {one.file for one in self._open.values()}
        if self.root.is_dir():
            for left in self.root.iterdir():
                if left not in kept and _modified(left) < idle:
                    left.unlink(missing_ok=True)

    def _drop(self, upload: str) -> None:
        one = self._open.pop(upload, None)
        if one is not None:
            one.file.unlink(missing_ok=True)


def _decoded(data: str) -> bytes:
    """The bytes of a part, which the panel sends in base64."""
    try:
        return base64.b64decode(data, validate=True)
    except binascii.Error, ValueError:
        raise PartRefused("A part is sent in base64, and this one isn't.") from None


def _modified(file: Path) -> float:
    try:
        return os.stat(file).st_mtime
    except FileNotFoundError:
        return float("inf")
