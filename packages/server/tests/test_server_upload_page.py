"""The upload page in a real browser: a file over the limit is never sent."""

import json
from collections.abc import Iterator
from contextlib import closing
from urllib.parse import quote

import pytest

from cliquet import screenshot
from cliquet.limits import MAX_DATA_FILE

from .server_helpers import draft_files, wait_for

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(screenshot.find_browser() is None, reason="needs a Chrome or Chromium"),
]

PAGE = "/build/books/upload?path=data/ledger.beancount"
# The message under the file field, once the page shows one.
SAID = "document.getElementById('upload-too-big')"


def choose(browser: screenshot.Browser, size: int) -> None:
    """Choose a file of `size` bytes in the page's file field, as the chooser would."""
    browser.evaluate(
        "(() => { const input = document.getElementById('upload-file');"
        " const chosen = new DataTransfer();"
        f" chosen.items.add(new File([new Uint8Array({size}).fill(120)], 'ledger.beancount'));"
        " input.files = chosen.files;"
        " input.dispatchEvent(new Event('change', {bubbles: true})); })()"
    )


@pytest.fixture
def browser(server, address: str) -> Iterator[screenshot.Browser]:
    """A browser signed in as the owner, on the upload page of an unpublished draft."""
    server.state.builder.create_app("etienne", "books")
    path = screenshot.find_browser()
    assert path is not None
    with closing(screenshot.Browser(path)) as opened:
        opened.visit(f"{address}/login?next={quote(PAGE)}", 768)
        wait_for(opened, "!!document.getElementById('upload-file')")
        yield opened


def test_a_file_over_the_limit_is_not_sent(server, browser: screenshot.Browser) -> None:
    before = draft_files(server.state.builder.drafts, "etienne", "books")
    choose(browser, MAX_DATA_FILE + 1)
    shown = wait_for(browser, f"!{SAID}.hidden && {SAID}.textContent")
    assert shown == (
        "A data file is at most 19.1 MB (20,000,000 bytes). "
        "This one is bigger, and nothing was written."
    )
    # Heard after the page's own listener: whether the browser goes on to send the form.
    browser.evaluate(
        "document.querySelector('.upload-form').addEventListener('submit',"
        " (event) => { window.sent = !event.defaultPrevented; event.preventDefault(); })"
    )
    browser.evaluate("document.querySelector('.upload-form button[type=submit]').click()")
    assert browser.evaluate("window.sent") is False
    assert browser.evaluate(f"{SAID}.hidden") is False
    assert draft_files(server.state.builder.drafts, "etienne", "books") == before


def test_a_file_under_the_limit_is_sent(server, browser: screenshot.Browser) -> None:
    choose(browser, 3_000)
    assert browser.evaluate(f"{SAID}.hidden") is True
    browser.evaluate("document.querySelector('.upload-form button[type=submit]').click()")
    shown = wait_for(browser, "document.querySelector('.alert')?.textContent")
    assert "data/ledger.beancount is in the draft" in json.dumps(shown)
    files = draft_files(server.state.builder.drafts, "etienne", "books")
    assert files["data/ledger.beancount"] == b"x" * 3_000
