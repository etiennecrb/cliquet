import json
import time

import pytest

from cliquet import screenshot

from .server_helpers import NOW, member

pytestmark = [pytest.mark.browser]

needs_browser = pytest.mark.skipif(
    screenshot.find_browser() is None, reason="needs a Chrome, Chromium or Edge"
)

PHONE = 390
# How far the page scrolls sideways, if it does.
WIDE = "document.documentElement.scrollWidth - window.innerWidth"
# The controls of the page that are too small to tap: buttons, menus, fields and tabs.
SMALL = """JSON.stringify([...document.querySelectorAll(
    'button, summary, select, input:not([type=hidden]), .btn, nav a')]
  .filter((e) => e.getClientRects().length && e.getBoundingClientRect().height < 40)
  .map((e) => e.outerHTML.slice(0, 80)))"""
# Whether the screen's host page in the frame has drawn the view.
DRAWN = "!!document.getElementById('screen')?.contentWindow?.cliquetDrawn"
SCREEN_WIDE = (
    "(() => { const w = document.getElementById('screen').contentWindow;"
    " return w.document.documentElement.scrollWidth - w.innerWidth; })()"
)


def wait_for_screen(browser: screenshot.Browser) -> None:
    deadline = time.monotonic() + 40
    while not browser.evaluate(DRAWN):
        assert time.monotonic() < deadline, "the screen wasn't drawn in time"
        time.sleep(0.2)


@needs_browser
def test_the_pages_fit_a_phone(server, address: str) -> None:
    database = server.state.apps.database
    sam = database.person_for(
        "https://example.eu.auth0.com/", "auth0|sam", "sam@example.com", "sam", NOW
    )
    member(database, sam, "etienne", "shop", "staff")
    cookie = server.state.web_auth.session_cookie(sam)
    server.state.builder.start_preview("etienne", "shop")
    path = screenshot.find_browser()
    assert path is not None
    browser = screenshot.Browser(path)
    wide: dict[str, int] = {}
    small: dict[str, list[str]] = {}
    try:
        browser.visit(f"{address}/", PHONE)
        wide["/ signed out"] = browser.evaluate(WIDE)

        # The member's pages, with their session.
        browser.evaluate(f"document.cookie = 'session={cookie}; path=/'")
        for page in ["/", "/apps/etienne/shop/staff"]:
            browser.visit(f"{address}{page}", PHONE)
            wide[f"{page} as sam"] = browser.evaluate(WIDE)
        small["/apps/etienne/shop/staff as sam"] = json.loads(browser.evaluate(SMALL))
        wait_for_screen(browser)
        wide["the screen as sam"] = browser.evaluate(SCREEN_WIDE)

        # The owner's pages, through the development login.
        browser.visit(f"{address}/", PHONE)
        signed_out = browser.evaluate(
            "fetch('/logout', {method: 'POST',"
            " body: new FormData(document.querySelector('form.sign-out'))})"
            ".then((response) => response.ok)"
        )
        assert signed_out is True
        browser.visit(f"{address}/login?next=/", PHONE)
        for page in [
            "/",
            "/assistants",
            "/manage/etienne/shop",
            "/manage/etienne/shop/views?width=768",
            "/build/shop/preview/owner",
            "/manage/etienne/shop/sharing",
            "/manage/etienne/shop/tools",
            "/apps/etienne/shop/owner",
        ]:
            browser.visit(f"{address}{page}", PHONE)
            wide[page] = browser.evaluate(WIDE)
        small["/apps/etienne/shop/owner"] = json.loads(browser.evaluate(SMALL))
        browser.visit(f"{address}/build/shop/preview/owner", PHONE)
        small["/build/shop/preview/owner"] = json.loads(browser.evaluate(SMALL))
    finally:
        browser.close()

    assert {page: px for page, px in wide.items() if px > 0} == {}
    assert {page: found for page, found in small.items() if found} == {}
