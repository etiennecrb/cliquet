import anyio
import pytest
from fastmcp import Client as BuilderClient

from cliquet import screenshot
from tests.server_helpers import (  # ty: ignore[import-not-found]
    app_client,
    app_token,
    me_server,
    text,
)

pytestmark = [pytest.mark.browser, pytest.mark.slow]

needs_browser = pytest.mark.skipif(
    screenshot.find_browser() is None, reason="needs a Chrome, Chromium or Edge"
)

APP = '''\
# /// script
# requires-python = ">=3.14"
# dependencies = ["cliquet-sdk>=0.0.1,<0.1"]
# ///
"""A reading list: books and whether they are finished."""

import csv
from pathlib import Path
from typing import Annotated

from cliquet import (
    ActionContext,
    App,
    BaseModel,
    Field,
    InvariantContext,
    QueryContext,
    Refused,
    Screen,
)

BOOKS = "books.csv"
COLUMNS = {BOOKS: ["title", "status"]}

reading = App("reading", files=[BOOKS])


class Book(BaseModel):
    title: str
    status: str


def _books() -> list[Book]:
    try:
        with open(BOOKS, newline="") as f:
            return [Book(**row) for row in csv.DictReader(f)]
    except FileNotFoundError:
        return []


def _append(book: Book) -> None:
    new = not Path(BOOKS).exists()
    if new:
        fields = COLUMNS[BOOKS]
    else:
        with open(BOOKS, newline="") as f:
            fields = next(csv.reader(f))
    with open(BOOKS, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(book.model_dump())


@reading.action
def add_book(
    ctx: ActionContext,
    title: Annotated[
        str, Field(pattern=r"\\S", description="The book's title, unique on the list")
    ],
) -> None:
    """Add a book to the reading list."""
    _append(Book(title=title.strip(), status="reading"))


@reading.action
def finish_book(
    ctx: ActionContext,
    title: Annotated[
        str, Field(pattern=r"\\S", description="The book's title, as listed by books")
    ],
) -> None:
    """Mark a book as finished."""
    rows = _books()
    found = False
    for row in rows:
        if row.title == title:
            row.status = "finished"
            found = True
    if not found:
        raise Refused(f"There is no book {title!r} on the list.")
    with open(BOOKS, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS[BOOKS])
        writer.writeheader()
        writer.writerows(row.model_dump() for row in rows)


@reading.invariant("A title is listed once")
def unique_title(ctx: InvariantContext) -> list[str]:
    seen = set()
    problems = []
    for book in _books():
        key = book.title.casefold()
        if key in seen:
            problems.append(f"{book.title!r} is listed twice")
        seen.add(key)
    return problems


@reading.query
def books(ctx: QueryContext) -> list[Book]:
    """Every book on the list."""
    return _books()


SHELF = """
<header>
  <h2>Reading list</h2>
  <p class="text-base-content/60">What you are reading, and what you finished</p>
</header>
<div class="stats card-border">
  <div class="stat">
    <div class="stat-title">Books</div>
    <div class="stat-value">{{ books | length }}</div>
  </div>
</div>
<section class="card card-border">
  <div class="card-body">
    <h3 class="card-title">Books</h3>
    {% if books %}
    <ul class="list">
      {% for book in books %}
      <li class="list-row">
        <span class="list-col-grow">{{ book.title }}</span>
        <span class="badge badge-soft">{{ book.status }}</span>
      </li>
      {% endfor %}
    </ul>
    {% else %}
    <p class="text-base-content/60">No book on the list yet.</p>
    {% endif %}
    <form class="layout-row" data-call="add_book">
      <input class="input layout-grow" name="title" placeholder="A book" aria-label="A book"
             required>
      <button class="btn btn-primary">Add book</button>
    </form>
  </div>
</section>
"""


@reading.view
def shelf(ctx: QueryContext) -> Screen:
    """The reading list, with a form to add a book."""
    return Screen(SHELF, books=books(ctx))
'''

TESTS = """\
from datetime import UTC, datetime

import pytest
from app import add_book, books, finish_book, reading, unique_title

from cliquet.testing import InvalidArguments, given

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_adds_a_book() -> None:
    person = given(reading, {"books.csv": "title,status\\nDune,reading\\n"}, now=NOW).connect()
    person.call(add_book, title="Emma").expect_ok()
    assert [book.title for book in person.query(books)] == ["Dune", "Emma"]


def test_a_title_needs_text() -> None:
    person = given(reading, {}, now=NOW).connect()
    with pytest.raises(InvalidArguments):
        person.call(add_book, title=" ")


def test_refuses_a_title_listed_twice() -> None:
    person = given(reading, {"books.csv": "title,status\\nDune,reading\\n"}, now=NOW).connect()
    person.call(add_book, title="Dune").expect_refused(by=unique_title)


def test_finishes_a_book() -> None:
    person = given(reading, {"books.csv": "title,status\\nDune,reading\\n"}, now=NOW).connect()
    person.call(finish_book, title="Dune").expect_ok()
    assert person.query(books)[0].status == "finished"
"""

MIGRATION = '''\
import csv


def migrate() -> None:
    """Add the note column to the live books."""
    with open("books.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row["note"] = row.get("note", "")
    with open("books.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["title", "status", "note"])
        writer.writeheader()
        writer.writerows(rows)
'''


@needs_browser
def test_an_assistant_builds_an_app(server, address) -> None:
    async def main() -> None:
        async with BuilderClient(me_server(server.state.apps)) as client:
            start = await client.session.call_tool("read_guide", {"topic": "start"})
            assert "create_app" in text(start)

            await client.call_tool("create_app", {"app": "reading"})
            await client.call_tool(
                "write_file", {"app": "reading", "path": "app.py", "content": APP}
            )
            await client.call_tool(
                "write_file", {"app": "reading", "path": "tests/test_reading.py", "content": TESTS}
            )
            await client.call_tool(
                "write_file",
                {
                    "app": "reading",
                    "path": "instructions.md",
                    "content": (
                        "# Reading list\n\nAdd a book with `add_book`; read them with `books`."
                    ),
                },
            )
            await client.call_tool(
                "write_file",
                {
                    "app": "reading",
                    "path": "data/books.csv",
                    "content": "title,status\nDune,reading\n",
                },
            )
            await client.call_tool("delete_file", {"app": "reading", "path": "tests/test_app.py"})

            checked = await client.call_tool("check_app", {"app": "reading"})
            assert checked.data["ok"] is True
            assert "add_book" in {tool["name"] for tool in checked.data["definition"]["tools"]}
            tested = await client.call_tool("run_tests", {"app": "reading"})
            assert tested.data["passed"] is True

            started = await client.call_tool("start_preview", {"app": "reading"})
            assert started.data["files"] == [{"path": "data/books.csv", "size": 26}]
            pictures = await client.call_tool("take_screenshots", {"app": "reading"})
            assert [part.type for part in pictures.content] == [
                "text",
                "image",
                "text",
                "image",
                "text",
            ]

            screen = await client.call_tool(
                "try_view",
                {
                    "app": "reading",
                    "view": "shelf",
                    "steps": [
                        {"fill": "A book", "with": "Emma"},
                        {"click": "Add book"},
                    ],
                },
            )
            assert "add_book" in screen.content[0].text
            assert "No error." in screen.content[0].text

            # The script's click is kept in the preview: Emma is on the shelf already.
            again = await client.call_tool(
                "try_call", {"app": "reading", "tool": "add_book", "arguments": {"title": "Emma"}}
            )
            assert again.data["ok"] is False
            assert "A title is listed once" in again.data["text"]

            published = await client.call_tool(
                "publish_app", {"app": "reading", "remark": "First version"}
            )
            assert published.data["published"] is True
            assert published.data["data"] == "First data: books.csv."

            await client.call_tool(
                "edit_file",
                {
                    "app": "reading",
                    "path": "app.py",
                    "old": 'COLUMNS = {BOOKS: ["title", "status"]}',
                    "new": 'COLUMNS = {BOOKS: ["title", "status", "note"]}',
                },
            )
            await client.call_tool(
                "edit_file",
                {
                    "app": "reading",
                    "path": "app.py",
                    "old": "class Book(BaseModel):\n    title: str\n    status: str",
                    "new": (
                        "class Book(BaseModel):\n    title: str\n    status: str\n"
                        '    note: str = ""'
                    ),
                },
            )
            await client.call_tool(
                "write_file", {"app": "reading", "path": "migrate.py", "content": MIGRATION}
            )
            checked = await client.call_tool("check_app", {"app": "reading"})
            assert checked.data["migration"] == "The migration rewrites: books.csv."
            assert checked.data["data"] == "The migration rewrites: books.csv."
            report = await client.call_tool(
                "publish_app", {"app": "reading", "remark": "Add notes"}
            )
            assert report.data["published"] is True
            assert report.data["data"] == "The migration rewrites: books.csv."

            info = await client.call_tool("read_app_overview", {"app": "reading"})
            assert info.data["latest_changes"][0]["action"] == "app_publish"

            token = app_token(server, "-", app="reading")
            async with app_client(address, "/apps/etienne/reading/mcp", token) as app:
                names = {tool.name for tool in (await app.list_tools()).tools}
                assert "add_book" in names
                answer = await app.call_tool("books", {})
            assert answer.structured_content is not None
            rows = answer.structured_content
            if isinstance(rows, dict) and "result" in rows:
                rows = rows["result"]
            assert rows and "note" in rows[0]

    anyio.run(main)
