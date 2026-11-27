"""The references to the files people upload for an action's file argument.

An agent asks for one with `app_ask_for_file`, and gets it at once. The person uploads the file
to it, in the upload panel, in parts, or on its upload page, and the agent passes the reference
to the action. A reference is only an id, bound to one app, person, action and argument: for
anyone else it is missing. Its file works once: the call that commits uses it up, and a call
that keeps nothing gives it back. It ends an hour after its file came, or an hour after it was
asked for if no file came, and each person has a few open at most.

The references are kept in memory, and their files on disk under the data folder, as the
drafts' uploads are: a restart ends them all, and no file is ever kept after its call.
"""

import secrets
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from cliquet.errors import Refused, shortened
from cliquet.files import input_name
from cliquet.inputs import Asked, Claim, Input
from cliquet.limits import MAX_FILE_ARGUMENT, file_too_big
from cliquet.tools import ASK_FOR_FILE
from cliquet_server.parts import OPEN_UPLOADS, PartRefused, Parts, Received

# How long a reference lasts, in seconds: after its file came, or after it was asked for.
LIFETIME = 60 * 60
# The references one person may have open at once: a new one ends the oldest.
OPEN_REFERENCES = OPEN_UPLOADS
# The refusal of a file over the limit: the panel, its parts and the upload page say it.
TOO_BIG = file_too_big("kept")


def upload_path(reference: str) -> str:
    """The upload page of a reference, under the server's address."""
    return f"/uploads/{reference}"


@dataclass
class _Reference:
    """One reference: whose, for which call, and its file once it came."""

    login: str
    name: str
    person: str
    action: str
    argument: str
    asked: float
    arrived: float | None = None
    file_name: str = ""
    size: int = 0
    file: Path | None = None
    claimed: bool = False

    def ends(self) -> float:
        """When it ends, on the store's clock."""
        return (self.asked if self.arrived is None else self.arrived) + LIFETIME


@dataclass(frozen=True)
class Pending:
    """A reference as its person's upload page and panel show it: the app, the action and
    argument it is for, and its file, once one came."""

    reference: str
    login: str
    name: str
    action: str
    argument: str
    file_name: str | None
    size: int | None


class References:
    """Every app's references, and their files under `root`."""

    def __init__(self, root: Path, base_url: str, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.base_url = base_url.rstrip("/")
        self._files = self.root / "files"
        self.clock = clock
        # Its parts read the store's clock, whichever it is now.
        self.parts = Parts(self.root / "parts", MAX_FILE_ARGUMENT, TOO_BIG, lambda: self.clock())
        self._lock = threading.Lock()
        self._open: dict[str, _Reference] = {}
        # A restart ended every reference: their files go.
        shutil.rmtree(self._files, ignore_errors=True)

    def for_app(self, login: str, name: str) -> AppInputs:
        """The references of one app, as its MCP server takes them."""
        return AppInputs(self, login, name)

    def ask(self, login: str, name: str, person: str, action: str, argument: str) -> Asked:
        """A new reference for a file `person` will upload for an action's argument. The
        person's oldest open one that no call uses ends if they have too many."""
        now = self.clock()
        with self._lock:
            self._sweep(now)
            theirs = sorted(
                (one.asked, reference)
                for reference, one in self._open.items()
                if one.person == person
            )
            over = max(0, len(theirs) - OPEN_REFERENCES + 1)
            # One a call uses now isn't ended under it: the oldest others go.
            free = [reference for _, reference in theirs if not self._open[reference].claimed]
            for reference in free[:over]:
                self._end(reference)
            reference = secrets.token_urlsafe(16)
            self._open[reference] = _Reference(login, name, person, action, argument, now)
        expires = datetime.fromtimestamp(now + LIFETIME, UTC)
        return Asked(reference, self.base_url + upload_path(reference), expires)

    def find(self, person: str, reference: str) -> Pending | None:
        """The person's open reference, or None: anyone else's is missing."""
        with self._lock:
            one = self._theirs(person, reference)
            return None if one is None else _pending(reference, one)

    def receive(
        self, person: str, reference: str, file_name: str, source: BinaryIO, size: int
    ) -> Pending:
        """Keep the whole file the person sent for their reference, from the upload page, in
        place of any file it had. Raises `PartRefused` when it can't be taken."""
        self._may_receive(person, reference, size)
        self.root.mkdir(parents=True, exist_ok=True)
        self._files.mkdir(exist_ok=True)
        file = self._files / secrets.token_hex(16)
        with file.open("wb") as out:
            shutil.copyfileobj(source, out)
        return self._arrived(person, reference, file_name, file)

    def receive_part(
        self,
        person: str,
        reference: str,
        file_name: str,
        size: int,
        offset: int,
        data: str,
        upload: str | None,
    ) -> Received | Pending:
        """Take one part of the file the person sends for their reference, from the panel: the
        upload and how much of it arrived, or, at its last part, the reference with its file.
        Raises `PartRefused`."""
        what = (reference, file_name)
        got = self.parts.receive(
            person,
            what,
            size,
            offset,
            data,
            upload,
            lambda: self._may_receive(person, reference, size),
        )
        if got.file is None:
            return got
        self._files.mkdir(parents=True, exist_ok=True)
        file = self._files / got.file.name
        got.file.replace(file)
        return self._arrived(person, reference, file_name, file)

    def claim(
        self, login: str, name: str, person: str, action: str, argument: str, reference: str
    ) -> Input:
        """The file of the reference, kept for one call until it is used or released.
        Raises `Refused`: a reference that isn't this person's, for this app, action and
        argument, is missing."""
        with self._lock:
            one = self._theirs(person, reference)
            if one is None or (one.login, one.name, one.action, one.argument) != (
                login,
                name,
                action,
                argument,
            ):
                raise Refused(_missing(reference))
            if one.file is None:
                raise Refused(
                    f"The person hasn't uploaded the file for {reference} yet, so nothing was "
                    "changed. Once they say it is in, call again with the same reference."
                )
            if one.claimed:
                raise Refused(
                    f"Another call uses the file of {reference} now, so nothing was changed. "
                    "Call again in a moment."
                )
            one.claimed = True
            file, file_name = one.file, one.file_name
        try:
            return Input(file_name, file.read_bytes())
        except OSError:
            self.release(reference)
            raise Refused(_missing(reference)) from None

    def use(self, reference: str) -> None:
        """The call that claimed the reference committed: it ends, with its file."""
        with self._lock:
            self._end(reference)

    def release(self, reference: str) -> None:
        """The call that claimed the reference kept nothing: another may claim it."""
        with self._lock:
            one = self._open.get(reference)
            if one is not None:
                one.claimed = False

    def drop(self, person: str) -> None:
        """End every reference of this person, with their files and parts on their way."""
        with self._lock:
            for reference, one in list(self._open.items()):
                if one.person == person:
                    self._end(reference)
        self.parts.drop(person)

    def _may_receive(self, person: str, reference: str, size: int) -> None:
        """Refuse a file for a reference the person has no open one of, one that a call uses
        now, or a file over the limit."""
        if size > MAX_FILE_ARGUMENT:
            raise PartRefused(TOO_BIG)
        with self._lock:
            one = self._theirs(person, reference)
            if one is None:
                raise PartRefused(_ended(reference))
            if one.claimed:
                raise PartRefused("A call uses this file now. Upload it again in a moment.")

    def _arrived(self, person: str, reference: str, file_name: str, file: Path) -> Pending:
        """Give the reference its file, in place of any it had, from now."""
        with self._lock:
            one = self._theirs(person, reference)
            if one is None or one.claimed:
                file.unlink(missing_ok=True)
                raise PartRefused(_ended(reference))
            if one.file is not None:
                one.file.unlink(missing_ok=True)
            one.file, one.file_name = file, input_name(file_name)
            one.size, one.arrived = file.stat().st_size, self.clock()
            return _pending(reference, one)

    def _theirs(self, person: str, reference: str) -> _Reference | None:
        """The person's open reference, or None. Call it with the lock held."""
        self._sweep(self.clock())
        one = self._open.get(reference)
        return one if one is not None and one.person == person else None

    def _sweep(self, now: float) -> None:
        """End every reference past its time. Call it with the lock held."""
        for reference, one in list(self._open.items()):
            if one.ends() <= now and not one.claimed:
                self._end(reference)

    def _end(self, reference: str) -> None:
        one = self._open.pop(reference, None)
        if one is not None and one.file is not None:
            one.file.unlink(missing_ok=True)


@dataclass(frozen=True)
class AppInputs:
    """One app's references, as its MCP server asks for them: `cliquet.inputs.Inputs`."""

    store: References
    login: str
    name: str

    def ask(self, person: str, action: str, argument: str) -> Asked:
        """A new reference for the person's file for this action's argument."""
        return self.store.ask(self.login, self.name, person, action, argument)

    def claim(self, person: str, action: str, argument: str, reference: str) -> Claim:
        """What a call claims through `reference`."""
        return _Claim(self, person, action, argument, reference)


@dataclass(frozen=True)
class _Claim:
    """One call's claim on a reference."""

    app: AppInputs
    person: str
    action: str
    argument: str
    reference: str

    def claim(self) -> Input:
        """The reference's file, for this call alone."""
        app = self.app
        return app.store.claim(
            app.login, app.name, self.person, self.action, self.argument, self.reference
        )

    def use(self) -> None:
        """The call committed: the reference ends."""
        self.app.store.use(self.reference)

    def release(self) -> None:
        """The call kept nothing: the reference stays."""
        self.app.store.release(self.reference)


def _pending(reference: str, one: _Reference) -> Pending:
    arrived = one.file is not None
    return Pending(
        reference,
        one.login,
        one.name,
        one.action,
        one.argument,
        one.file_name if arrived else None,
        one.size if arrived else None,
    )


def _missing(reference: str) -> str:
    """What a call through a reference that isn't there for its caller is told."""
    return (
        f"There is no file {shortened(reference, 40)!r} for you here, so nothing was changed. A "
        "reference works once, for the person who asked for it, and only for the action it was "
        f"asked for. Call {ASK_FOR_FILE} for a new one."
    )


def _ended(reference: str) -> str:
    """What an upload to a reference that isn't there for its person is told."""
    return (
        "This upload's reference has ended, or isn't yours, and nothing was kept. Ask your "
        f"assistant for a new one: it calls {ASK_FOR_FILE} again."
    )
