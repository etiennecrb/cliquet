"""The preview of one data file, for the app's owner only: a table for a CSV, its lines for
other text, and only its size for a binary file. It is bounded, as a diff is.

A preview quotes the data, so it is for the owner only: see "Who sees what" in ARCHITECTURE.md.
The overview opens it in a panel, and the same address opens it as a page of its own.
"""

import csv
import itertools
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import Response

from cliquet.files import declares
from cliquet_server.apps import Apps, Live
from cliquet_server.diffs import MAX_LINE, MAX_LINES, readable
from cliquet_server.pages import problem, render

from .common import Site, manage_context, readable_size

# How many rows of a table show, its header row aside, and how many of its columns. A text
# file shows as many lines as a diff, each at most `MAX_LINE` characters.
MAX_ROWS = 500
MAX_COLUMNS = 50
# How many characters of a cell show: with the rows and columns, a table quotes no more of
# the data than a diff's lines can.
MAX_CELL = 160
# The files that show as a table, with the character between their cells.
_DELIMITERS = {".csv": ",", ".tsv": "\t"}


class DataFiles:
    """The route that previews one of an app's data files."""

    def __init__(self, site: Site) -> None:
        self.site = site

    async def show(self, request: Request) -> Response:
        """One data file as the last change left it: the panel's content for htmx, else a page."""
        response = await self._show(request)
        # The same address answers the panel's content or a whole page: no cache mixes them,
        # nor keeps what quotes the owner's data.
        response.headers["Vary"] = "HX-Request"
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _show(self, request: Request) -> Response:
        found = await self.site.owned(request)
        if isinstance(found, Response):
            return found
        person, live = found
        name = request.path_params["file"]
        shown = await anyio.to_thread.run_sync(_read, self.site.apps, live, name)
        if shown is None:
            app = f"{live.info.login}/{live.info.name}"
            return problem(
                request,
                404,
                "No such file",
                f"{app} has no data file {name[:200]} yet. Its overview lists the files it keeps.",
            )
        context = {**manage_context(live, "overview"), "file": shown}
        if request.headers.get("HX-Request") == "true":
            return render(request, "data_file.html", context)
        return await self.site.console(request, person, "manage_data_file.html", context)


def _read(apps: Apps, live: Live, name: str) -> dict[str, Any] | None:
    """The preview of a file the app declares, from the last commit, or None when the app
    declares no such file or hasn't created it yet."""
    if not declares(live.runtime.manifest["files"], name):
        return None
    content = apps.repo(live.info.login, live.info.name).data([name]).get(name)
    if content is None:
        return None
    return {"name": name, **preview(name, content)}


def preview(name: str, content: bytes) -> dict[str, Any]:
    """What the preview of a file shows: its kind, its size, and its rows or lines, bounded."""
    size = readable_size(len(content))
    if b"\0" in content:
        return {"kind": "binary", "size": size}
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return {"kind": "binary", "size": size}
    if not text:
        return {"kind": "empty", "size": size}
    delimiter = _DELIMITERS.get(PurePosixPath(name).suffix.lower())
    if delimiter is not None:
        try:
            return {"kind": "table", "size": size, **_table(text, delimiter)}
        except csv.Error:
            pass
    return {"kind": "text", "size": size, **_text(text)}


def _table(text: str, delimiter: str) -> dict[str, Any]:
    """A CSV's first row as its header, then its next rows, each cell cut and readable.
    Raises `csv.Error` when it isn't one, so that it shows as text."""
    reader = csv.reader(_lines(text), delimiter=delimiter)
    # A blank line is no row, above the header too.
    header = next((row for row in reader if row), [])
    # The rows past `MAX_ROWS` are only counted.
    rows: list[list[str]] = []
    left_out = 0
    for row in reader:
        if not row:
            continue
        if len(rows) < MAX_ROWS:
            rows.append(row)
        else:
            left_out += 1
    width = max([len(header), *(len(row) for row in rows)])
    return {
        "header": _cells(header, width),
        "rows": [_cells(row, width) for row in rows],
        "left_out": left_out,
        "columns_left_out": max(0, width - MAX_COLUMNS),
    }


def _cells(row: list[str], width: int) -> list[dict[str, Any]]:
    """A row's first `MAX_COLUMNS` cells, padded to the table's width, each cut and readable."""
    cells = (row + [""] * (width - len(row)))[:MAX_COLUMNS]
    return [_shown(cell, MAX_CELL) for cell in cells]


def _text(text: str) -> dict[str, Any]:
    """A text file's first lines, each cut and readable, and how many more it has."""
    shown = list(itertools.islice(_lines(text), MAX_LINES))
    end = sum(len(line) for line in shown)
    left_out = text.count("\n", end) + (not text.endswith("\n")) if end < len(text) else 0
    lines = [_shown(line.removesuffix("\n").removesuffix("\r")) for line in shown]
    return {"lines": lines, "left_out": left_out}


def _shown(text: str, most: int = MAX_LINE) -> dict[str, Any]:
    """A line or a cell as the preview shows it: its start, readable, and whether it was cut."""
    return {"text": readable(text[:most]), "cut": len(text) > most}


def _lines(text: str) -> Iterator[str]:
    """Each line of a text with its end, as a file gives them, without copying the whole text."""
    start = 0
    while start < len(text):
        end = text.find("\n", start) + 1 or len(text)
        yield text[start:end]
        start = end
