"""The draft's files: list, read, write, edit and delete them, and the upload page's part."""

import io
import itertools
import os
from typing import Any, BinaryIO, NamedTuple
from urllib.parse import quote

from cliquet.errors import shortened
from cliquet.files import Kind
from cliquet.limits import MAX_DATA_FILE, amount_text
from cliquet_server.db import AppInfo

from .drafts import MAX_FILE, check_path
from .errors import BuilderError, LiveData
from .workshop import Workshop

# The refusal of a data file over the limit: the upload page, the panel and upload_file say it.
TOO_BIG = (
    f"A data file is at most {amount_text(MAX_DATA_FILE)} ({MAX_DATA_FILE:,} bytes). "
    "This one is bigger, and nothing was written."
)

# What one read_file returns at most: lines, and characters.
READ_LINES = 1_000
MAX_READ_LINES = 2_000
MAX_READ_CHARS = 40_000


class LineRange(NamedTuple):
    """Lines `first` to `last` of a file, counted from 1, with the text read on those two.

    The text guards the edit: a range read before another edit moved the lines no longer
    reads the same, so it is refused rather than applied to the wrong lines.
    """

    first: int
    last: int
    first_text: str
    last_text: str


class DraftFiles:
    """The builder's tools for a draft's files."""

    def __init__(self, workshop: Workshop) -> None:
        self.workshop = workshop
        self.apps = workshop.apps
        self.drafts = workshop.drafts

    def list_files(self, login: str, app: str) -> dict[str, Any]:
        """The draft's files, its live data, and whether it differs from the live version."""
        info = self.workshop.ensure_draft(login, app)
        published = info.published_at is not None
        live_data = []
        if published:
            repo = self.apps.repo(login, app)
            names = repo.data_names()
            contents = repo.data(names)
            live_data = [{"path": f"data/{name}", "size": len(contents[name])} for name in names]
        return {
            "app": app,
            "published": published,
            "changed": self.workshop.changed(login, info),
            "draft": [
                {"path": path, "size": size} for path, size in self.drafts.sizes(login, app).items()
            ],
            "live_data": live_data,
        }

    def read_file(
        self, login: str, app: str, path: str, offset: int = 1, limit: int = READ_LINES
    ) -> dict[str, Any]:
        """Read lines of a draft file, or of the live copy of a published app's data."""
        if not 1 <= limit <= MAX_READ_LINES:
            raise BuilderError(f"limit is a number of lines from 1 to {MAX_READ_LINES:,}.")
        if offset < 1:
            raise BuilderError("offset is the first line to read, counted from 1.")
        info = self.workshop.ensure_draft(login, app)
        kind = check_path(path)
        if kind is Kind.DATA and info.published_at is not None:
            name = path[len("data/") :]
            content = self.apps.repo(login, app).data([name]).get(name)
            if content is None:
                raise BuilderError(f"{path} isn't in the app. Call list_files to see its data.")
            return {
                "path": path,
                "size": len(content),
                **_lines(io.BytesIO(content), path, offset, limit),
            }
        target = self.drafts.folder(login, app) / path
        if not target.is_file():
            raise BuilderError(f"{path} isn't in the draft. Call list_files to see the files.")
        with target.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            return {"path": path, "size": size, **_lines(stream, path, offset, limit)}

    def ask_for_file(self, login: str, app: str, path: str) -> dict[str, Any]:
        """The link where the person uploads a data file into the draft, and what the upload
        panel shows."""
        info = self.workshop.find(login, app)
        self.check_upload(info, path)
        return {
            "app": app,
            "path": path,
            "link": self.workshop.build_url(app, f"upload?path={quote(path)}"),
            "limit": MAX_DATA_FILE,
            "files": self.uploaded(info),
            "next": "Once the person says the file is uploaded, call list_files. Look at the "
            "file's start with read_file, then check it with check_app, and ask the app's "
            "queries about it with start_preview and try_call.",
        }

    def check_upload(self, info: AppInfo, path: str) -> None:
        """Refuse an upload to a path that isn't data, to an app that was published, or to no
        draft."""
        if check_path(path) is not Kind.DATA:
            raise BuilderError(
                f"{path} isn't a data file. An uploaded file goes under data/, "
                "such as data/ledger.beancount."
            )
        _may_write(info, path, Kind.DATA)
        if not self.drafts.exists(info.login, info.name):
            raise BuilderError(f"{info.name} has no draft. Call list_files to see its files.")

    def upload(self, info: AppInfo, path: str, source: BinaryIO, size: int) -> None:
        """Copy a data file the person sent from the upload page into the draft.

        The checks run again under the draft's lock, on the app as it is now, so that a draft
        deleted meanwhile is not made again, and a file sent while the app is first published
        goes live with it, or is refused: publish holds the same lock.
        """
        with self.drafts.locked(info.login, info.name):
            self.check_upload(self.workshop.find(info.login, info.name), path)
            self.drafts.copy(info.login, info.name, path, source, size)

    def uploaded(self, info: AppInfo) -> list[dict[str, Any]]:
        """The draft's data files, with their sizes, for the upload page."""
        sizes = self.drafts.sizes(info.login, info.name, kind=Kind.DATA)
        return [{"path": path, "size": size} for path, size in sizes.items()]

    def write_file(self, login: str, app: str, path: str, content: str) -> dict[str, Any]:
        """Create or replace a draft file the owner may write."""
        # Checked under the draft's lock, on the app as it is then: see `upload`.
        with self.drafts.locked(login, app):
            info = self.workshop.ensure_draft(login, app)
            kind = check_path(path)
            _may_write(info, path, kind)
            data = content.encode()
            _typed("write_file", path, kind, len(data))
            self.drafts.write(login, app, path, data)
            return {
                "path": path,
                "size": len(data),
                "next": "Call check_app when the files are ready.",
            }

    def edit_file(
        self,
        login: str,
        app: str,
        path: str,
        old: str,
        new: str,
        replace_all: bool = False,
        lines: LineRange | None = None,
        size: int | None = None,
    ) -> dict[str, Any]:
        """Replace `old`, or a range of lines, by `new` in a draft file: refused when `size`,
        the file's size as read_file gave it, says the file changed since."""
        if lines is not None and (old or replace_all):
            raise BuilderError(
                "Give either `old` or a range of lines, not both. replace_all is for `old` only."
            )
        if lines is None and not old:
            raise BuilderError(
                "`old` can't be empty: give the text to replace, or a range of lines."
            )
        # Read, checked and written under the draft's lock: see `upload`.
        with self.drafts.locked(login, app):
            info = self.workshop.ensure_draft(login, app)
            kind = check_path(path)
            _may_write(info, path, kind)
            target = self.drafts.folder(login, app) / path
            if not target.is_file():
                raise BuilderError(f"{path} isn't in the draft. Read it again with read_file.")
            found = target.stat().st_size
            if size is not None and size != found:
                raise BuilderError(
                    f"{path} is {found:,} bytes now, not {size:,}: it changed since you read it. "
                    "Read it again with read_file."
                )
            _typed("edit_file", path, kind, found)
            # As read_file reads it: UTF-8, with each line's own ending kept.
            try:
                text = target.read_bytes().decode()
            except UnicodeDecodeError:
                raise BuilderError(
                    f"{path} isn't UTF-8 text, so edit_file can't change it. Replace it with "
                    "write_file, or have the person upload a data file with ask_for_file."
                ) from None
            if lines is None:
                changed, done = _replace_text(text, path, old, new, replace_all)
            else:
                changed, done = _replace_lines(text, path, lines, new)
            data = changed.encode()
            _typed("edit_file", path, kind, len(data))
            self.drafts.write(login, app, path, data)
            return {
                "path": path,
                "size": len(data),
                **done,
                "next": "Call check_app when the files are ready.",
            }

    def delete_file(self, login: str, app: str, path: str) -> dict[str, Any]:
        """Remove a draft file the owner may write."""
        # Checked under the draft's lock, on the app as it is then: see `upload`.
        with self.drafts.locked(login, app):
            info = self.workshop.ensure_draft(login, app)
            if path == "app.py":
                raise BuilderError("app.py can't be deleted. Write it instead.")
            _may_write(info, path, check_path(path))
            self.drafts.delete(login, app, path)
            return {"path": path, "next": "Call list_files to see what is left."}


def _replace_text(
    text: str, path: str, old: str, new: str, replace_all: bool
) -> tuple[str, dict[str, Any]]:
    """The text with `old` replaced by `new`, once or everywhere, and how many were replaced."""
    count = text.count(old)
    if count == 0:
        raise BuilderError(f"{old!r} was not found in {path}. Read the file again with read_file.")
    if count > 1 and not replace_all:
        raise BuilderError(
            f"{old!r} appears {count} times in {path}. Give more of the text around it, "
            "or pass replace_all."
        )
    changed = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    return changed, {"replaced": count if replace_all else 1}


def _replace_lines(text: str, path: str, lines: LineRange, new: str) -> tuple[str, dict[str, Any]]:
    """The text with the range of lines replaced by `new`, and how many lines it has now.

    Lines end at "\\n" only, and keep it, as read_file counts them. When the range's last line
    ends with "\\n", so does `new`, so that it doesn't run into the line after it.
    """
    if not 1 <= lines.first <= lines.last:
        raise BuilderError("first_line is counted from 1, and last_line is at least first_line.")
    rows = _split_lines(text)
    if lines.last > len(rows):
        raise BuilderError(
            f"{path} has {len(rows):,} lines, so it has no line {lines.last:,}. "
            "Read the lines again with read_file."
        )
    for number, given in ((lines.first, lines.first_text), (lines.last, lines.last_text)):
        found = rows[number - 1].removesuffix("\n")
        if given.removesuffix("\n") != found:
            raise BuilderError(
                f"Line {number:,} of {path} reads {shortened(found, 200)!r}, not "
                f"{shortened(given, 200)!r}. Read the lines again with read_file, then give "
                "each line's whole text."
            )
    if new and rows[lines.last - 1].endswith("\n") and not new.endswith("\n"):
        new += "\n"
    rows[lines.first - 1 : lines.last] = [new]
    changed = "".join(rows)
    return changed, {"removed": lines.last - lines.first + 1, "lines": len(_split_lines(changed))}


def _split_lines(text: str) -> list[str]:
    """The text's lines, each with its "\\n" but the last, which may have none."""
    rows = text.split("\n")
    return [row + "\n" for row in rows[:-1]] + ([rows[-1]] if rows[-1] else [])


def _may_write(info: AppInfo, path: str, kind: Kind) -> None:
    """Refuse the lock, which Cliquet writes, and a published app's live data."""
    if kind is Kind.LOCK:
        raise BuilderError("app.py.lock is written by Cliquet when the app is built.")
    if kind is Kind.DATA and info.published_at is not None:
        raise LiveData(
            f"{path} is live data. Change it through the app's actions, "
            'or with a migration: see read_guide("publishing").',
            info,
        )


def _typed(tool: str, path: str, kind: Kind, size: int) -> None:
    """Refuse a file the assistant would write or edit past MAX_FILE: what it types passes
    through the model. A bigger data file comes from the person, through the upload page."""
    if size <= MAX_FILE:
        return
    sentence = f"{tool} takes a file of at most {MAX_FILE:,} bytes; {path} would be {size:,}."
    if kind is Kind.DATA:
        sentence += (
            " A data file this big comes from the person: have them upload it with ask_for_file."
        )
    raise BuilderError(sentence)


def _lines(stream: BinaryIO, path: str, offset: int, limit: int) -> dict[str, Any]:
    """Up to `limit` lines of the file from line `offset`, and at most MAX_READ_CHARS
    characters.

    Lines end at "\\n" only, and keep it, so that the parts put together give the file back.
    The lines are counted on the bytes, and only those returned are decoded.
    """
    count, last = 0, b""
    while chunk := stream.read(1024 * 1024):
        count += chunk.count(b"\n")
        last = chunk[-1:]
    if last not in (b"", b"\n"):
        count += 1
    if offset > max(count, 1):
        raise BuilderError(f"The file has {count:,} lines; offset {offset:,} is past its end.")
    stream.seek(0)
    taken: list[str] = []
    used = 0
    cut = False
    for raw in itertools.islice(stream, offset - 1, offset - 1 + limit):
        try:
            line = raw.decode()
        except UnicodeDecodeError:
            raise BuilderError(
                f"{path} isn't UTF-8 text, so read_file can't show it. Ask the app's queries "
                "about it with try_call instead."
            ) from None
        if used + len(line) > MAX_READ_CHARS:
            if not taken:
                taken.append(line[:MAX_READ_CHARS])
                cut = True
            break
        taken.append(line)
        used += len(line)
    last_read = offset - 1 + len(taken)
    answer: dict[str, Any] = {
        "from": offset,
        "to": last_read,
        "lines": count,
        "text": "".join(taken),
    }
    notes = []
    if cut:
        notes.append(
            f"Line {offset:,} is longer than {MAX_READ_CHARS:,} characters: only its start is "
            "here, and read_file can't read the rest of that line. Ask the app's queries about "
            "it with try_call instead."
        )
    if last_read < count:
        notes.append(
            f"Lines {last_read + 1:,} to {count:,} weren't read. Read more with "
            f"offset={last_read + 1}, or ask the app's queries with try_call."
        )
    if notes:
        answer["next"] = " ".join(notes)
    return answer
