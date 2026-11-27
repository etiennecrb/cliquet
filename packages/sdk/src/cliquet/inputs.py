"""A file an action takes, which the person uploads: how a call gets it.

The agent passes a reference, not the file: `app_ask_for_file` makes one, and the person
uploads the file to it. The runtime claims the reference once it knows the call will run, and
hands the file to the engine with the call, as it hands the data. A call that commits uses the
reference up; one that is refused, fails or changes nothing gives it back. A server keeps the
references in an `Inputs`; one with none takes no file.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from cliquet.files import input_name


@dataclass(frozen=True)
class Input:
    """A file an action is given: the name it has in the working copy, and its bytes."""

    name: str
    content: bytes


class Claim(Protocol):
    """One file a call may take: claimed when the call runs, then used up or given back."""

    def claim(self) -> Input:
        """The file, kept for this call alone. Raises `Refused` when it can't be had."""
        ...

    def use(self) -> None:
        """The call committed: the file can't be had again."""
        ...

    def release(self) -> None:
        """The call kept nothing: the file can be claimed again."""
        ...


@dataclass(frozen=True)
class Asked:
    """A reference made for an action's file: its id, the page where the person uploads the
    file, and when it ends if no file came."""

    reference: str
    link: str
    expires: datetime


class Inputs(Protocol):
    """Where one app's references wait for their file and their call, each bound to a person,
    an action and its file argument."""

    def ask(self, person: str, action: str, argument: str) -> Asked:
        """Make a reference for a file the person will upload for this argument."""
        ...

    def claim(self, person: str, action: str, argument: str, reference: str) -> Claim:
        """What a call claims through `reference`. A reference that isn't this person's, for
        this action and argument, is missing, as one that never was."""
        ...


@dataclass
class Given:
    """A file a test gives a call itself, as `cliquet.testing` does: it can be claimed again
    and again, as a test needs no reference."""

    name: str
    content: bytes

    @classmethod
    def of(cls, value: bytes | bytearray | Path) -> Given:
        """A file from its bytes, or from the file at a path."""
        if isinstance(value, Path):
            return cls(input_name(value.name), value.read_bytes())
        return cls("file", bytes(value))

    def claim(self) -> Input:
        """The file, each time."""
        return Input(self.name, self.content)

    def use(self) -> None:
        """Nothing to use up."""

    def release(self) -> None:
        """Nothing to give back."""
