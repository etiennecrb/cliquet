"""Which of an app's files are its code, its lock, its migration or its data, and where an
action finds a file it takes.

This is the one definition. The sandbox, the server's repository and the builder's drafts all
sort an app's paths with `kind_of`, so a new kind of code file is added here alone.
"""

import functools
import re
import unicodedata
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

from pydantic import WithJsonSchema

_DATA_PART = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}")
_DATA_PARTS = 8
_DATA_LENGTH = 200


def data_name_problem(name: str) -> str | None:
    """Why `name` can't be a data file, or None when it can."""
    parts = name.split("/")
    if not name:
        why = "it is empty"
    elif len(name) > _DATA_LENGTH:
        why = f"it is longer than {_DATA_LENGTH} characters"
    elif name.startswith("/"):
        why = "it starts with /, but it must be a path inside the app's data folder"
    elif len(parts) > _DATA_PARTS:
        why = f"it has more than {_DATA_PARTS} parts"
    elif "" in parts:
        why = "it has an empty part, from a / at the end or two in a row"
    else:
        bad = next((part for part in parts if not _DATA_PART.fullmatch(part)), None)
        if bad is None:
            return None
        if bad.startswith("."):
            why = f"its part {bad!r} starts with a dot"
        elif len(bad) > 100:
            why = f"its part {bad[:20]!r}... is longer than 100 characters"
        else:
            why = f"its part {bad!r} has a character other than letters, digits, ., _ and -"
    return (
        f"{name!r} can't be a data file: {why}. Use up to {_DATA_PARTS} parts joined by /, each "
        "made of letters, digits, ., _ and -, not starting with a dot, such as stock.csv or "
        "2026/january.csv."
    )


# A part of a pattern: `**`, or a part of a name, not starting with a dot, with one `*` at most.
_PATTERN_PART = re.compile(r"\*\*|(?!\.)[A-Za-z0-9._-]*\*?[A-Za-z0-9._-]*")
# What a `*` matches: any characters a part of a name may have.
_ANY = "[A-Za-z0-9._-]*"
# What a `**` matches: any whole parts, none at all included.
_FOLDERS = "(?:[A-Za-z0-9._-]+/)*"


def is_pattern(entry: str) -> bool:
    """True when `entry`, one of the files an app declares, is a pattern rather than a name."""
    return "*" in entry


def declared_problem(entry: str) -> str | None:
    """Why `entry` can't be one of the files an app declares, or None when it can: a data
    file's name, or a pattern with at most one `**`, a whole part that matches any folders, and
    one `*` inside a part, which matches any characters but /, as `Path.glob` means them."""
    if not is_pattern(entry):
        return data_name_problem(entry)
    parts = entry.split("/")
    stars = entry.replace("**", "").count("*")
    if len(entry) > _DATA_LENGTH:
        why = f"it is longer than {_DATA_LENGTH} characters"
    elif entry.startswith("/"):
        why = "it starts with /, but it must be a path inside the app's data folder"
    elif len(parts) > _DATA_PARTS:
        why = f"it has more than {_DATA_PARTS} parts"
    elif "" in parts:
        why = "it has an empty part, from a / at the end or two in a row"
    elif any("**" in part and part != "**" for part in parts):
        why = "its ** is inside a part, but it must be a whole part, such as **/*.csv"
    elif entry.count("**") > 1 or stars > 1:
        why = "it has more than one ** or more than one *"
    elif not parts[-1].replace("*", ""):
        why = (
            "its last part is only a wildcard, which would match any file, such as a "
            "database's journal or a cache: end it with a name's end, such as *.csv"
        )
    else:
        bad = next((p for p in parts if not _PATTERN_PART.fullmatch(p) or len(p) > 100), None)
        if bad is None:
            return None
        if bad.startswith("."):
            why = f"its part {bad!r} starts with a dot"
        elif len(bad) > 100:
            why = f"its part {bad[:20]!r}... is longer than 100 characters"
        else:
            why = f"its part {bad!r} has a character other than letters, digits, ., _, - and *"
    return (
        f"{entry!r} can't be declared: {why}. Declare a data file's name, such as stock.csv, "
        "or a pattern of names with at most one ** as a whole part, for any folders, and one * "
        "inside a part, ending in a name's end, such as **/*.beancount or 2026/*.csv."
    )


@functools.cache
def _compiled(pattern: str) -> re.Pattern[str]:
    """The regular expression of a pattern that `declared_problem` accepts."""
    parts = pattern.split("/")
    expression = "".join(
        _FOLDERS if part == "**" else _ANY.join(map(re.escape, part.split("*"))) + "/"
        for part in parts
    )
    return re.compile(expression.removesuffix("/"))


def matches(entry: str, name: str) -> bool:
    """True when `entry`, one of the files an app declares, declares the data file `name`:
    the same name, or one its pattern matches."""
    if not is_pattern(entry):
        return name == entry
    return _compiled(entry).fullmatch(name) is not None and data_name_problem(name) is None


def declares(declared: Iterable[str], name: str) -> bool:
    """True when one of the files an app declares, `declared`, declares the data file `name`."""
    return any(matches(entry, name) for entry in declared)


LOCK = "app.py.lock"
"""The lock of `app.py`'s environment, written by Cliquet when it builds the app."""
MIGRATION = "migrate.py"
"""The one-off migration that changes the data when a new version goes live."""

_CODE = re.compile(r"app\.py|instructions\.md|tests/[A-Za-z0-9_]+\.py")


class Kind(StrEnum):
    """The kind of one of an app's files."""

    CODE = "code"
    """`app.py`, `instructions.md` and `tests/<name>.py`: what the version id is a hash of."""
    LOCK = "lock"
    """`app.py.lock`: sent with a version and kept with it, but out of its id."""
    MIGRATION = "migration"
    """`migrate.py`: sent with a version, out of its id, and run once."""
    DATA = "data"
    """`data/<name>`: never part of a version."""


VERSION_KINDS = frozenset({Kind.CODE, Kind.LOCK, Kind.MIGRATION})
"""The kinds of the files a version is built from: everything but the data."""


def kind_of(path: str) -> Kind | None:
    """The kind of the app's file at `path`, relative to its folder, or None for no file of it."""
    if _CODE.fullmatch(path):
        return Kind.CODE
    if path == LOCK:
        return Kind.LOCK
    if path == MIGRATION:
        return Kind.MIGRATION
    if path.startswith("data/") and data_name_problem(path.removeprefix("data/")) is None:
        return Kind.DATA
    return None


def versioned(path: str) -> bool:
    """True when the file at `path` counts in the version id: only the code does."""
    return kind_of(path) is Kind.CODE


def path_problem(path: str) -> str | None:
    """Why `path` names no file of an app, saying which paths do, or None when it names one."""
    if kind_of(path) is not None:
        return None
    if path.startswith("data/"):
        return data_name_problem(path.removeprefix("data/"))
    return (
        "Code: app.py, instructions.md, and tests/<name>.py. "
        f"The lock: {LOCK}, written by Cliquet. "
        f"A migration: {MIGRATION}. "
        "Data: data/<path>."
    )


# A file an action takes: the person uploads it, and the action reads it in its working copy.

FILE_FORMAT = "app_file"
"""The JSON schema's `format` of a file argument: a string, the reference to the file that
`app_ask_for_file` gives."""
FILE_SCHEMA: dict[str, Any] = {"type": "string", "format": FILE_FORMAT}

File = Annotated[Path, WithJsonSchema(FILE_SCHEMA)]
"""The type of an action's argument that is a file the person uploads, such as
`statement: File`. The action gets its path, relative to its working folder, read-only, and
reads it as an ordinary file. The file is gone after the call: an action that keeps it writes
it to a declared data file. An action takes one at most, `File | None = None` included."""

INPUTS = ".app_input"
"""The folder of a call's working copy that holds the file an action takes, apart from the
data, since no data file's name starts with a dot. It is removed before the invariants run."""

_UNNAMED = "file"


def input_name(name: str) -> str:
    """The name a file the person uploaded gets in `INPUTS`: its last part, in ASCII letters,
    digits, `.`, `_` and `-`, not starting with a dot, and at most 100 characters, keeping its
    end, where its extension is."""
    last = name.replace("\\", "/").rsplit("/", 1)[-1]
    letters = unicodedata.normalize("NFKD", last)
    plain = "".join(c for c in letters if not unicodedata.combining(c))
    plain = re.sub(r"[^A-Za-z0-9._-]+", "_", plain).lstrip("._")
    return plain[-100:].lstrip("._") or _UNNAMED


def is_file_schema(schema: object) -> bool:
    """Whether a property's schema is a file argument's: `File`, or `File | None`."""
    if not isinstance(schema, dict):
        return False
    if schema.get("format") == FILE_FORMAT:
        return True
    options = schema.get("anyOf")
    if not isinstance(options, list):
        return False
    files = [one for one in options if isinstance(one, dict) and one.get("format") == FILE_FORMAT]
    rest = [one for one in options if one not in files]
    return len(files) == 1 and rest == [{"type": "null"}]


def holds_file(schema: object) -> bool:
    """Whether a schema holds a file anywhere, at any depth."""
    if isinstance(schema, dict):
        return schema.get("format") == FILE_FORMAT or any(map(holds_file, schema.values()))
    if isinstance(schema, list):
        return any(map(holds_file, schema))
    return False
