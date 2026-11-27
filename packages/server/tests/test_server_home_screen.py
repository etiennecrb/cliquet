"""Each role's page is a web app a phone adds to its home screen: its manifest, which only a
person who may open the page reads, and the app's icon as PNGs, drawn once per look and kept
in memory."""

import json
import logging
import struct
import zlib
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from cliquet import screenshot
from cliquet_server import app_icon
from cliquet_server.app_icon import home_drawing
from cliquet_server.db import AppInfo
from cliquet_server.icons import elements
from cliquet_server.mark import STATIC
from cliquet_server.web.app_pages import home_name

from .server_helpers import (
    NOW,
    a_shop_member,
    act_as,
    published_with_icon,
    signed_in,
    with_accent,
)

MANIFEST = "/apps/etienne/shop/{role}/manifest.webmanifest"
ICONS = {"apple-touch-icon.png": 180, "icon-192.png": 192, "icon-512.png": 512}
NOTHING = {"error": "there is no web app at this address"}
# Apply the page's rules for an installed app as if it were one: headless Chrome emulates no
# display mode. Gives how many rules it applied.
STANDALONE = """(() => {
  const rules = [...document.styleSheets].flatMap((sheet) => [...sheet.cssRules])
    .filter((rule) => rule.conditionText === '(display-mode: standalone)')
    .flatMap((rule) => [...rule.cssRules].map((inner) => inner.cssText));
  const style = document.createElement('style');
  style.textContent = rules.join('\\n');
  document.head.append(style);
  return rules.length;
})()"""


def shown(browser: screenshot.Browser, selector: str) -> bool:
    """Whether the element `selector` finds takes room on the page."""
    found = json.dumps(selector)
    return browser.evaluate(f"document.querySelector({found}).getClientRects().length > 0")


def png_size(png: bytes) -> tuple[int, int]:
    """The width and height a PNG's header says."""
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def corner(png: bytes) -> str:
    """The colour of a PNG's top-left pixel, as `#rrggbbaa`, for an 8-bit RGBA one. Whatever
    the first row's filter, its first pixel is stored as it is."""
    assert png[24:26] == b"\x08\x06", "an 8-bit RGBA PNG"
    data, at = b"", 8
    while at < len(png):
        (length,) = struct.unpack(">I", png[at : at + 4])
        kind = png[at + 4 : at + 8]
        if kind == b"IDAT":
            data += png[at + 8 : at + 8 + length]
        at += 12 + length
    return "#" + zlib.decompress(data)[1:5].hex()


@pytest.fixture(autouse=True)
def nothing_drawn() -> None:
    """Start each test with no PNG kept from another's look."""
    app_icon.home_png.cache_clear()


@pytest.fixture
def draws(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The size of each PNG the server draws, in order."""
    drawn: list[int] = []
    draw = app_icon.resvg_py.svg_to_bytes

    def counted(**options: Any) -> bytes:
        drawn.append(options["width"])
        return draw(**options)

    monkeypatch.setattr(app_icon.resvg_py, "svg_to_bytes", counted)
    return drawn


def test_a_role_s_page_is_a_web_app(server: Starlette) -> None:
    with signed_in(server) as client:
        answer = client.get(MANIFEST.format(role="staff"))
    assert answer.status_code == 200
    assert answer.headers["content-type"] == "application/manifest+json"
    assert answer.headers["cache-control"] == "private"
    manifest = answer.json()
    # The owner holds both roles, so the name says which one this app is.
    page = "/apps/etienne/shop/staff"
    assert manifest == {
        "id": page,
        "name": "Shop · staff",
        "short_name": "Shop · staff",
        "start_url": page,
        "scope": page,
        "display": "standalone",
        "lang": "en",
        "theme_color": "#0f766e",
        "background_color": "#ffffff",
        "icons": [
            {"src": f"/apps/etienne/shop/{file}", "sizes": size, "type": "image/png", "purpose": p}
            for p in ("any", "maskable")
            for file, size in (("icon-192.png", "192x192"), ("icon-512.png", "512x512"))
        ],
    }


def test_a_person_with_one_role_installs_the_app_by_its_title(server: Starlette) -> None:
    sam = a_shop_member(server, "staff")
    with TestClient(server) as client:
        act_as(server, client, sam)
        manifest = client.get(MANIFEST.format(role="staff")).json()
        page = client.get("/apps/etienne/shop/staff").text
    assert (manifest["name"], manifest["short_name"]) == ("Shop", "Shop")
    assert '<meta name="apple-mobile-web-app-title" content="Shop">' in page
    assert home_name("Stock", "staff", ["staff"]) == "Stock"
    assert home_name("Stock", "staff", ["owner", "staff"]) == "Stock · staff"


def test_only_who_may_open_the_page_reads_its_manifest(server: Starlette) -> None:
    database = server.state.apps.database
    sam = a_shop_member(server, None)
    with TestClient(server) as client:
        # Signed out, nobody is sent to sign in: a phone fetches a manifest, never a page.
        answers = {"signed out": client.get(MANIFEST.format(role="staff"), follow_redirects=False)}
        act_as(server, client, sam)
        answers["not shared"] = client.get(MANIFEST.format(role="staff"))
        database.share("etienne", "shop", "sam@example.com", "staff", NOW)
        answers["share waits"] = client.get(MANIFEST.format(role="staff"))
        database.accept_share(sam.id, "etienne", "shop", NOW)
        answers["role not held"] = client.get(MANIFEST.format(role="owner"))
        answers["no such role"] = client.get(MANIFEST.format(role="nobody"))
        answers["no such app"] = client.get("/apps/etienne/nothing/staff/manifest.webmanifest")
        assert client.get(MANIFEST.format(role="staff")).status_code == 200
    for why, answer in answers.items():
        assert answer.status_code == 404, why
        # The same words for every refusal: nothing of the app, its title included.
        assert answer.json() == NOTHING, why
        assert answer.headers["cache-control"] == "private", why


def test_a_role_s_page_links_its_web_app(server: Starlette) -> None:
    with signed_in(server) as client:
        page = client.get("/apps/etienne/shop/staff").text
    href = "/apps/etienne/shop/staff/manifest.webmanifest"
    assert f'<link rel="manifest" href="{href}" crossorigin="use-credentials">' in page
    # The app's own home screen icon, which is the mark's while it names no icon.
    assert '<link rel="apple-touch-icon" href="/apps/etienne/shop/apple-touch-icon.png">' in page
    assert '<link rel="apple-touch-icon" href="/apple-touch-icon.png">' not in page
    assert '<meta name="apple-mobile-web-app-title" content="Shop · staff">' in page
    assert '<meta name="theme-color" content="#0f766e">' in page


def test_an_app_without_an_icon_shows_the_mark_on_a_home_screen(
    server: Starlette, draws: list[int]
) -> None:
    with TestClient(server) as client:
        for file, size in ICONS.items():
            answer = client.get(f"/apps/etienne/shop/{file}")
            assert answer.status_code == 200, file
            assert answer.headers["content-type"] == "image/png", file
            mark = STATIC / ("apple-touch-icon.png" if size == 180 else f"mark-{size}.png")
            assert answer.content == mark.read_bytes(), file
            assert png_size(answer.content) == (size, size), file
            again = client.get(
                f"/apps/etienne/shop/{file}", headers={"if-none-match": answer.headers["etag"]}
            )
            assert again.status_code == 304, file
        assert client.get("/apps/etienne/nothing/icon-192.png").status_code == 404
    assert draws == []


def test_the_mark_fills_its_square(server: Starlette) -> None:
    # A home screen masks an icon itself, and iOS paints what is transparent black.
    for size in (192, 512):
        png = (STATIC / f"mark-{size}.png").read_bytes()
        assert png_size(png) == (size, size)
        assert corner(png) == "#5b3fd1ff"


def test_an_app_s_icon_is_drawn_once_per_look(server: Starlette, draws: list[int]) -> None:
    published_with_icon(server)
    apps = server.state.apps
    # A phone fetches the icon with no session, and that never starts the app.
    apps.forget("etienne", "shop")
    with TestClient(server) as client:
        first = {file: client.get(f"/apps/etienne/shop/{file}") for file in ICONS}
        assert apps.current("etienne", "shop") is None
        for file, size in ICONS.items():
            answer = first[file]
            assert answer.status_code == 200, file
            assert answer.headers["content-type"] == "image/png", file
            assert png_size(answer.content) == (size, size), file
            # It fills its square with the accent: no rounded corner for iOS to paint black.
            assert corner(answer.content) == "#0f766eff", file
        assert sorted(draws) == [180, 192, 512]
        # Kept in memory from then on.
        again = client.get("/apps/etienne/shop/icon-512.png")
        assert again.content == first["icon-512.png"].content
        etag = first["icon-512.png"].headers["etag"]
        fresh = client.get("/apps/etienne/shop/icon-512.png", headers={"if-none-match": etag})
        assert fresh.status_code == 304
        # The same look published again draws nothing new.
        published_with_icon(server)
        client.get("/apps/etienne/shop/icon-512.png")
        assert len(draws) == 3
        # A new accent draws it again, with a new validator.
        with_accent(server, "#b45309")
        changed = client.get("/apps/etienne/shop/icon-512.png", headers={"if-none-match": etag})
        assert changed.status_code == 200
        assert changed.headers["etag"] != etag
        assert corner(changed.content) == "#b45309ff"
        assert draws[3:] == [512]


def test_an_icon_that_can_t_be_drawn_shows_the_mark_until_it_can(
    server: Starlette, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    draw = app_icon.resvg_py.svg_to_bytes

    def failing(**options: object) -> bytes:
        raise ValueError("the document does not have a root node")

    monkeypatch.setattr(app_icon.resvg_py, "svg_to_bytes", failing)
    # Nothing is drawn as the app is published: its publish never fails on its icon.
    published_with_icon(server)
    with TestClient(server) as client, caplog.at_level(logging.WARNING):
        answer = client.get("/apps/etienne/shop/icon-192.png")
        assert answer.status_code == 200
        assert answer.content == (STATIC / "mark-192.png").read_bytes()
        (record,) = [one for one in caplog.records if "home screen icon" in one.getMessage()]
        assert record.levelno == logging.WARNING
        # Tried again at the next request.
        monkeypatch.setattr(app_icon.resvg_py, "svg_to_bytes", draw)
        etag = answer.headers["etag"]
        again = client.get("/apps/etienne/shop/icon-192.png", headers={"if-none-match": etag})
    assert again.status_code == 200
    assert corner(again.content) == "#0f766eff"


def test_a_home_screen_icon_keeps_the_drawing_inside_the_mask() -> None:
    info = AppInfo("etienne", "shop", "", NOW, NOW, "#0f766e", "leaf", None)
    drawing = home_drawing(info)
    assert drawing is not None
    # A full square, no rounded corner, and Lucide's drawing in white in the middle.
    assert '<rect width="32" height="32" fill="#0f766e"/>' in drawing
    assert 'x="7" y="7" width="18" height="18"' in drawing
    assert str(elements("leaf")) in drawing
    for icon, accent in (None, "#0f766e"), ("leaf", None):
        assert home_drawing(AppInfo("etienne", "shop", "", NOW, NOW, accent, icon, None)) is None


@pytest.mark.browser
@pytest.mark.skipif(screenshot.find_browser() is None, reason="needs a Chrome, Chromium or Edge")
def test_chrome_can_install_a_role_s_page(server: Starlette, address: str) -> None:
    published_with_icon(server)
    path = screenshot.find_browser()
    assert path is not None
    browser = screenshot.Browser(path)
    try:
        browser.visit(f"{address}/login?next=/apps/etienne/shop/staff", 390)
        assert browser.evaluate("location.pathname") == "/apps/etienne/shop/staff"
        # Chrome reads the manifest with the person's session, and finds nothing missing.
        read = browser._send("Page.getAppManifest", {})
        assert read["errors"] == []
        assert read["manifest"]["name"] == "Shop · staff"
        assert read["manifest"]["display"] == "kStandalone"
        assert browser._send("Page.getInstallabilityErrors", {})["installabilityErrors"] == []
        # Installed, the page keeps only its tabs, and the screen under them.
        assert shown(browser, ".app-bar-row")
        assert browser.evaluate(STANDALONE) > 0
        assert not shown(browser, ".app-bar-row")
        assert shown(browser, ".app-tabs")
        assert shown(browser, "#screen")
        # With a single screen there are no tabs, and no bar at all.
        browser.evaluate("document.querySelector('.app-tabs').remove()")
        assert not shown(browser, ".app-bar")
        assert shown(browser, "#screen")
    finally:
        browser.close()
