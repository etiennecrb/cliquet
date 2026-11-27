"""What one change did to an app's data: its diff, read from git and kept bounded.

A diff quotes the data's lines, so it is for the app's owner only: see "Who sees what" in
ARCHITECTURE.md. Anyone else gets counts at most, from `GitStore.counts`.
"""

import csv
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import IO, Literal

from cliquet.store import GIT_FLAGS, Entry, GitStore, StoreError

# A change is named by its commit's full id, as the history page links to it. Anything else
# git reads, such as `HEAD~2` or `:/text`, never reaches git.
COMMIT = re.compile(r"[0-9a-f]{40}")
# How many lines of one change's diff are kept. The rest is only counted.
MAX_LINES = 2000
# How many bytes of one line are kept: a data file may be one very long line.
MAX_LINE = 2000
# What an answer writes after a line cut at `MAX_LINE` bytes, as the History tab says it, and
# counts in `max_chars`.
LONG_LINE = " … the rest of this long line is left out"

Kind = Literal["hunk", "context", "added", "removed", "note"]
_KINDS: dict[bytes, Kind] = {b" ": "context", b"+": "added", b"-": "removed", b"\\": "note"}
# A control character would break a line or vanish on a page: its picture shows instead,
# such as ␍ for a carriage return. A tab stays a tab.
_PICTURES: dict[int, int | str] = {
    **{code: 0x2400 + code for code in range(0x20) if code != 0x09},
    0x7F: 0x2421,
}
# The other characters that hide or reorder what is around them show as their code: the later
# control characters, the line and paragraph separators, and the ones that turn the text's
# direction, which could make a line read as another.
_HIDDEN = [*range(0x80, 0xA0), 0x2028, 0x2029, *range(0x202A, 0x202F), *range(0x2066, 0x206A)]
_PICTURES |= {code: f"<U+{code:04X}>" for code in _HIDDEN}


@dataclass(frozen=True)
class Line:
    """One line of a diff: its kind, and its text as a unified diff has it, sign first. `cut`
    says the text is only the line's start."""

    kind: Kind
    text: str
    cut: bool = False


@dataclass
class FileDiff:
    """What one commit did to one data file.

    `lines` holds the lines of its diff that are kept, and `left_out` counts the others.
    `added` and `removed` count every line added and removed, kept or not. A file git reads
    as binary has no line: `before` and `after` are its sizes in bytes, None when it wasn't
    there. `blobs` names its content before and after, as git does.
    """

    name: str
    status: Literal["added", "removed", "changed"] = "changed"
    binary: bool = False
    before: int | None = None
    after: int | None = None
    added: int = 0
    removed: int = 0
    lines: list[Line] = field(default_factory=list)
    left_out: int = 0
    blobs: tuple[str, str] = ("", "")
    # The change as rows, for a CSV file a person reads better so: see `csv_table`.
    table: Table | None = None


@dataclass(frozen=True)
class Row:
    """One row of a CSV file's change: its kind, its cells, and whether rows the change left
    alone come between it and the row above."""

    kind: Kind
    cells: list[str]
    gap: bool = False


@dataclass(frozen=True)
class Table:
    """A CSV file's change as rows under the file's header, for a person to read, with how
    many rows it added and removed."""

    header: list[str]
    rows: list[Row]
    added: int
    removed: int


def csv_table(store: GitStore, file: FileDiff) -> Table | None:
    """A CSV file's change as a table, or None when its lines read better: a file that isn't
    CSV, one whose diff was cut or left out, one whose header changed, or one with no row."""
    if file.binary or not file.name.lower().endswith(".csv") or file.left_out:
        return None
    if any(line.cut for line in file.lines):
        return None
    before, after = file.blobs
    header_line = _first_line(store, before if file.status == "removed" else after)
    if header_line is None:
        return None
    if file.status == "changed" and _first_line(store, before) != header_line:
        return None
    header = _cells(header_line)
    if header is None:
        return None
    rows: list[Row] = []
    gap = False
    # Where the next line is in the file before and after the change: the header is line 1.
    old = new = 0
    for line in file.lines:
        if line.kind == "hunk":
            found = _HUNK.match(line.text)
            if found is None:
                return None
            old, new = int(found[1]), int(found[2])
            # Rows left alone above this part of the change, other than the header.
            gap = max(old, new) > 2 or bool(rows)
            continue
        if line.kind == "note":
            continue
        top = (old if line.kind == "removed" else new) == 1
        old += line.kind != "added"
        new += line.kind != "removed"
        if top:
            continue
        cells = _cells(line.text[1:])
        if cells is None:
            return None
        rows.append(Row(line.kind, cells, gap))
        gap = False
    if not rows:
        return None
    added = sum(row.kind == "added" for row in rows)
    return Table(header, rows, added, sum(row.kind == "removed" for row in rows))


# Where a hunk starts in the file before and after, as git writes it: `@@ -12,3 +12,4 @@`.
_HUNK = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _cells(text: str) -> list[str] | None:
    """A line of CSV as its cells, or None when it isn't one whole record: a quoted field
    that runs onto another line leaves an odd number of quotes on each of its lines."""
    if text.count('"') % 2:
        return None
    try:
        (cells,) = list(csv.reader([text], strict=True))
    except csv.Error, ValueError:
        return None
    return cells


def _first_line(store: GitStore, blob: str) -> str | None:
    """The first line of a blob, read from git without reading the rest, or None when there
    is no blob or the line is too long to keep."""
    if not blob.strip("0"):
        return None
    process = subprocess.Popen(
        ["git", *GIT_FLAGS, "cat-file", "blob", blob],
        cwd=store.root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    try:
        first = next(_lines(process.stdout), None)
    finally:
        process.kill()
        process.stdout.close()
        process.wait()
    if first is None or first[1]:
        return None
    return _text(first[0])


def change_entry(store: GitStore, commit: str) -> Entry | None:
    """The change the app's history lists under `commit`, or None when there is none: the
    commit isn't one, isn't in the live history, or didn't change the data."""
    if not COMMIT.fullmatch(commit):
        return None
    # A full id that names no commit is an ancestor of nothing.
    if not store.is_ancestor(commit, "HEAD"):
        return None
    return store.entry(commit)


def read_diff(
    store: GitStore, commit: str, max_lines: int = MAX_LINES, max_chars: int | None = None
) -> list[FileDiff]:
    """What a commit, named by its full id, did to each data file, as a unified diff.

    At most `max_lines` lines are kept, over all the files, each cut at `MAX_LINE` bytes, and
    with `max_chars`, only while their text, `LONG_LINE` for a line cut, and a line feed each
    fit in it: a file's line that
    doesn't leaves out the rest of that file. git's answer is read as it comes and never held
    whole. A line whose end alone changed is no
    change: from a line feed to a carriage return and a line feed or back, or the last line's
    to none. Python's `csv` writes the second, so an app's first write of a file would show
    its every line. A file with no other change is left out. Raises `StoreError` when git fails.
    """
    process = subprocess.Popen(
        [
            *["git", *GIT_FLAGS, "diff-tree", "-r", "--root", "--no-commit-id", "--patch"],
            *["--full-index", "--no-renames", "--no-color", "--no-ext-diff", "--no-textconv"],
            *["--ignore-cr-at-eol", "--src-prefix=a/", "--dst-prefix=b/"],
            *[commit, "--", store.prefix],
        ],
        cwd=store.root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    try:
        files = _parse(process.stdout, store.prefix, max_lines, max_chars)
    except BaseException:
        process.kill()
        raise
    finally:
        process.stdout.close()
        process.wait()
    if process.returncode != 0:
        raise StoreError(f"git couldn't read what {commit} changed")
    _measure(store, [file for file in files if file.binary])
    return files


def _parse(stream: IO[bytes], prefix: str, max_lines: int, max_chars: int | None) -> list[FileDiff]:
    """The files of a patch as git writes it, reading the whole stream. Only the lines kept
    are read as text."""
    files: list[FileDiff] = []
    kept = 0
    room = max_chars
    in_hunk = full = False
    for line, cut in _lines(stream):
        # A line of a file's content starts with its sign, so none looks like a header.
        if line.startswith(b"diff --git "):
            files.append(FileDiff(_name(line, prefix)))
            in_hunk = full = False
            continue
        if not files:
            continue
        file = files[-1]
        if line.startswith(b"@@"):
            in_hunk = True
            kind: Kind = "hunk"
            # Where the hunk is, without the line git guesses is its heading: for data it is
            # any line above.
            line = line[: line.find(b" @@", 2) + 3]
        elif in_hunk:
            kind = _KINDS.get(line[:1], "context")
            file.added += kind == "added"
            file.removed += kind == "removed"
        else:
            _header(file, line)
            continue
        if kept < max_lines and not full:
            text = _text(line, cut)
            size = len(text) + (len(LONG_LINE) if cut else 0) + 1
            if room is None or size <= room:
                kept += 1
                room = None if room is None else room - size
                file.lines.append(Line(kind, text, cut))
                continue
            full = True
        file.left_out += 1
    return files


def _lines(stream: IO[bytes]) -> Iterator[tuple[bytes, bool]]:
    """Each line of a stream without its end, and whether it was cut at `MAX_LINE` bytes. The
    rest of a cut line is read and dropped."""
    while line := stream.readline(MAX_LINE + 1):
        cut = len(line) > MAX_LINE and not line.endswith(b"\n")
        if cut:
            while (rest := stream.readline(1 << 16)) and not rest.endswith(b"\n"):
                pass
        yield line.removesuffix(b"\n")[:MAX_LINE], cut


def _name(header: bytes, prefix: str) -> str:
    """A file's name inside the data folder, from `diff --git a/<path> b/<path>`: with no
    rename the path is there twice, so its length says where it ends, whatever it holds."""
    paths = header[len(b"diff --git ") :]
    path = paths[: (len(paths) - 1) // 2]
    if path.startswith(b'"'):
        # Git writes a path in quotes, as C would, when it holds a quote, a backslash, a
        # control character or, by default, a character that isn't ASCII.
        path = path[1:-1].decode("unicode_escape", errors="replace").encode("latin-1", "replace")
    name = _text(path[len(b"a/") :])
    return name[len(prefix) + 1 :] if prefix != "." else name


def _header(file: FileDiff, line: bytes) -> None:
    """Read one line of a file's header, above its first hunk."""
    if line.startswith(b"new file mode"):
        file.status = "added"
    elif line.startswith(b"deleted file mode"):
        file.status = "removed"
    elif line.startswith(b"index "):
        before, _, after = line.split()[1].decode().partition("..")
        file.blobs = (before, after)
    elif line.startswith(b"Binary files "):
        file.binary = True


def _text(line: bytes, cut: bool = False) -> str:
    """A line of a data file as a page can show it: never its bytes as they are. The
    carriage return that ends a line is part of its end, and is dropped. So is the part of a
    character a `cut` line ends with."""
    text = line.removesuffix(b"\r").decode(errors="replace")
    return readable(text.removesuffix("\ufffd") if cut else text)


def readable(text: str) -> str:
    """Text from a data file with each hidden or control character shown as a picture or code."""
    return text.translate(_PICTURES)


def _measure(store: GitStore, files: list[FileDiff]) -> None:
    """Give each binary file its size before and after, read from git without its bytes."""
    blobs = sorted({blob for file in files for blob in file.blobs if blob.strip("0")})
    if not blobs:
        return
    answer = subprocess.run(
        ["git", *GIT_FLAGS, "cat-file", "--batch-check"],
        cwd=store.root,
        input="".join(f"{blob}\n" for blob in blobs).encode(),
        capture_output=True,
        check=False,
    )
    sizes: dict[str, int] = {}
    for line in answer.stdout.decode().splitlines():
        blob, _, size = line.partition(" blob ")
        if size.isdecimal():
            sizes[blob] = int(size)
    for file in files:
        file.before, file.after = (sizes.get(blob) for blob in file.blobs)
