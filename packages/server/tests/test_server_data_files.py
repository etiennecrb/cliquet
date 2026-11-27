import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.testclient import TestClient

from cliquet import screenshot
from cliquet_server.apps import Apps, Live
from cliquet_server.repos import AppRepo
from cliquet_server.web import console, data_files
from cliquet_server.web.data_files import preview

from .server_helpers import (
    a_shop_member,
    act_as,
    kept,
    needs_browser,
    signed_in,
    wait_for,
)

OVERVIEW = "/manage/etienne/shop"
PRODUCTS = f"{OVERVIEW}/data/products.csv"
PANEL = {"HX-Request": "true"}


def cells(page: str, tag: str) -> list[str]:
    """The text of each cell of one kind in a preview's table."""
    return re.findall(rf"<{tag}>(.*?)</{tag}>", page)


def test_the_overview_opens_each_data_file_in_the_panel(server) -> None:
    with signed_in(server) as client:
        text = client.get(OVERVIEW).text

    link = f'<a class="link" href="{PRODUCTS}" data-file-panel><code>products.csv</code></a>'
    assert link in text
    assert '<dialog class="file-panel" id="file-panel"' in text


def test_a_csv_shows_as_a_table(server) -> None:
    kept(server, {"products.csv": b'sku,name,price\r\nTEA-SEN,Sencha,8\r\nTEA-<b>,"A, B",9\r\n'})
    with signed_in(server) as client:
        panel = client.get(PRODUCTS, headers=PANEL)
        page = client.get(PRODUCTS)

    assert panel.status_code == 200
    # htmx gets the panel's content alone, a plain request the console's page around it.
    assert "<html" not in panel.text
    assert "HX-Request" in panel.headers["vary"] and "HX-Request" in page.headers["vary"]
    assert "<code>products.csv</code>" in panel.text
    assert cells(panel.text, "th") == ["sku", "name", "price"]
    assert cells(panel.text, "td") == ["TEA-SEN", "Sencha", "8", "TEA-&lt;b&gt;", "A, B", "9"]
    assert "2 rows shown." in panel.text
    assert page.status_code == 200
    assert "<html" in page.text and 'aria-current="page">Overview' in page.text
    assert cells(page.text, "th") == ["sku", "name", "price"]


def test_a_long_table_is_cut() -> None:
    rows = "".join(f"{n},{'x' * 3000 if n == 1 else n}\n" for n in range(data_files.MAX_ROWS + 7))
    shown = preview("big.csv", f"n,value\n{rows}".encode())

    assert shown["kind"] == "table"
    assert len(shown["rows"]) == data_files.MAX_ROWS
    assert shown["left_out"] == 7
    cell = shown["rows"][1][1]
    assert cell["cut"] and len(cell["text"]) == data_files.MAX_CELL
    wide = preview("wide.tsv", "\t".join(str(n) for n in range(60)).encode())
    assert len(wide["header"]) == data_files.MAX_COLUMNS
    assert wide["columns_left_out"] == 10


def test_text_shows_its_lines_readable_and_cut() -> None:
    lines = "".join(f"line {n}\n" for n in range(data_files.MAX_LINES + 3))
    shown = preview("ledger.beancount", f"﻿hidden\x1b[2J‮turned\r\n{lines}".encode())

    assert shown["kind"] == "text"
    assert len(shown["lines"]) == data_files.MAX_LINES
    assert shown["lines"][0]["text"] == "hidden␛[2J<U+202E>turned"
    assert shown["left_out"] == 4
    # A CSV that isn't one shows as text.
    assert preview("odd.csv", b'a,"b\n' + b"c" * 200_000)["kind"] == "text"


def test_a_binary_or_empty_file_shows_its_size_only() -> None:
    assert preview("photo.csv", b"\x89PNG\r\n\x1a\n\x00\x00") == {
        "kind": "binary",
        "size": "10 bytes",
    }
    assert preview("latin.csv", "café".encode("latin-1"))["kind"] == "binary"
    assert preview("products.csv", b"")["kind"] == "empty"
    # Blank lines aren't nothing: they show as lines.
    assert preview("notes.txt", b"\n\n  \n")["kind"] == "text"


def test_blank_lines_are_no_rows_above_the_header_too() -> None:
    shown = preview("products.csv", b"\n\nsku,name\n\nTEA,Sencha\n")

    assert [cell["text"] for cell in shown["header"]] == ["sku", "name"]
    assert [[cell["text"] for cell in row] for row in shown["rows"]] == [["TEA", "Sencha"]]


def test_the_preview_reads_the_last_commit_not_the_disk(server) -> None:
    folder = server.state.apps.repo("etienne", "shop").folder
    (folder / "data" / "products.csv").write_text("sku\nEDITED-BY-HAND\n")
    with signed_in(server) as client:
        text = client.get(PRODUCTS, headers=PANEL).text

    assert "EDITED-BY-HAND" not in text
    assert "TEA-SEN" in text


@pytest.mark.parametrize(
    "path",
    ["app.py", "../app.py", "%2e%2e/app.py", ".git/config", "data/products.csv", "notes.txt"],
)
def test_only_a_declared_data_file_shows(server, path: str) -> None:
    with signed_in(server) as client:
        answer = client.get(f"{OVERVIEW}/data/{path}")

    assert answer.status_code == 404
    assert "import json" not in answer.text and "[core]" not in answer.text
    # What reaches the route is kept by no cache. `..` never reaches it: the address is
    # read first.
    if "/data/" in answer.request.url.path:
        assert answer.headers["cache-control"] == "no-store"


def test_a_file_not_created_yet_has_no_preview(server) -> None:
    kept(server, {"movements.csv": None})
    with signed_in(server) as client:
        overview = client.get(OVERVIEW).text
        answer = client.get(f"{OVERVIEW}/data/movements.csv")

    assert "<code>movements.csv</code>" in overview
    assert f'href="{OVERVIEW}/data/movements.csv"' not in overview
    assert answer.status_code == 404
    assert "has no data file movements.csv yet" in answer.text


def ledger(tmp_path: Path, files: list[str], data: dict[str, bytes]) -> tuple[Apps, Live]:
    """A published ledger that declares `files`, with `data`, as the console reads it."""
    repo = AppRepo(tmp_path / "ledger")
    code: dict[str, bytes | None] = {"app.py": b"code"}
    code.update({f"data/{name}": content for name, content in data.items()})
    when = datetime(2026, 11, 10, tzinfo=UTC)
    repo.publish(None, code, version="0123456789abcdef", note="test", login="etienne", when=when)
    apps = SimpleNamespace(repo=lambda login, name: repo)
    live = SimpleNamespace(
        info=SimpleNamespace(login="etienne", name="ledger"),
        runtime=SimpleNamespace(manifest={"files": files}),
    )
    return cast(Apps, apps), cast(Live, live)


def rows(shown: dict[str, Any]) -> list[tuple[str, str, bool]]:
    return [(row["name"], row["size"], row["created"]) for row in shown["files"]]


def test_the_overview_lists_each_file_a_pattern_matches(tmp_path: Path) -> None:
    data = {"ledger.beancount": b"open\n", "2026/01.beancount": b"jan\n", "notes.txt": b"x\n"}
    apps, live = ledger(tmp_path, ["ledger.beancount", "**/*.beancount", "2027/*.csv"], data)

    shown = console._files(apps, live)

    # In the order the app declares them, each once, and a pattern that matches nothing yet
    # has a row of its own.
    assert rows(shown) == [
        ("ledger.beancount", "5 bytes", True),
        ("2026/01.beancount", "4 bytes", True),
        ("2027/*.csv", "none yet", False),
    ]
    assert shown["more_files"] == 0
    assert data_files._read(apps, live, "2026/01.beancount") is not None
    assert data_files._read(apps, live, "notes.txt") is None


def test_the_overview_lists_a_bounded_number_of_files(tmp_path: Path) -> None:
    data = {f"{n:03}.beancount": b"x" for n in range(console._SHOWN_FILES + 3)}
    apps, live = ledger(tmp_path, ["**/*.beancount"], data)

    shown = console._files(apps, live)

    assert len(shown["files"]) == console._SHOWN_FILES
    assert shown["more_files"] == 3


def test_only_the_owner_previews_a_data_file(server) -> None:
    person = a_shop_member(server, "owner")
    with TestClient(server) as client:
        signed_out = client.get(PRODUCTS, follow_redirects=False)
        act_as(server, client, person)
        member = client.get(PRODUCTS, headers=PANEL)

    assert signed_out.status_code == 303
    assert member.status_code == 404
    assert "TEA-SEN" not in member.text


# Whether the panel is open, and what it holds.
OPEN = "document.getElementById('file-panel').open"
ROWS = "document.querySelectorAll('#file-panel-body tbody tr').length"


@pytest.mark.browser
@needs_browser
@pytest.mark.parametrize("width", [390, 1280])
def test_the_panel_opens_and_closes_over_the_overview(server, address: str, width: int) -> None:
    path = screenshot.find_browser()
    assert path is not None
    browser = screenshot.Browser(path)
    try:
        browser.visit(f"{address}/login?next={OVERVIEW}", width)
        browser.evaluate("document.querySelector('a[data-file-panel]').click()")
        assert wait_for(browser, ROWS) > 0
        assert browser.evaluate(OPEN) is True
        assert browser.evaluate("location.pathname") == OVERVIEW
        # The panel fits the window, and its table scrolls inside it.
        assert browser.evaluate(
            "document.getElementById('file-panel').getBoundingClientRect().right"
            " <= window.innerWidth"
        )
        assert browser.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
        # A click inside the panel, below its table, leaves it open.
        below = (
            "(() => { const box = document.getElementById('file-panel').getBoundingClientRect();"
            " return document.elementFromPoint(box.left + 20, box.bottom - 20)"
            ".dispatchEvent(new MouseEvent('click', {bubbles: true,"
            " clientX: box.left + 20, clientY: box.bottom - 20})); })()"
        )
        browser.evaluate(below)
        assert browser.evaluate(OPEN) is True
        # A click on the shade closes it, where the window shows the shade.
        if width > 768:
            browser.evaluate(
                "document.getElementById('file-panel').dispatchEvent("
                "new MouseEvent('click', {bubbles: true, clientX: 10, clientY: 10}))"
            )
            assert browser.evaluate(OPEN) is False
            browser.evaluate("document.querySelector('a[data-file-panel]').click()")
            wait_for(browser, OPEN)
        # Its button closes it.
        browser.evaluate("document.querySelector('.file-panel-close button').click()")
        assert browser.evaluate(OPEN) is False
    finally:
        browser.close()
