"""What an MCP client is told about Cliquet's servers: the icon each shows and the address of
its site, on the person's endpoint and on each app's."""

from pathlib import Path

import anyio
import httpx
from starlette.applications import Starlette

from cliquet_server.app import create_app
from cliquet_server.app_icon import app_icons, drawing
from cliquet_server.db import AppInfo
from cliquet_server.executor import LocalExecutor
from cliquet_server.icons import elements
from cliquet_server.settings import Settings

from .server_helpers import APPS, NOW, app_client, app_token, published_with_icon, running

SHOP = "/apps/etienne/shop/mcp"
ME = "/me/mcp"
# The mark's files as each endpoint gives them: their name at the server's root, their type,
# and the sizes the file is drawn at.
MARK = {
    "favicon.svg": ("image/svg+xml", ["any"]),
    "apple-touch-icon.png": ("image/png", ["180x180"]),
}


def test_both_endpoints_show_the_mark_and_the_site(server: Starlette, address: str) -> None:
    settings = server.state.apps.settings
    token = app_token(server, "staff")

    async def scenario() -> None:
        for path, bearer in ((SHOP, token), (ME, "dev")):
            async with app_client(address, path, bearer) as client:
                info = client.server_info
            assert info is not None, path
            # Nothing sets the site apart from the server: it is the server's own address.
            assert info.website_url == settings.website_url == settings.base_url, path
            icons = {
                (icon.src, icon.mime_type, tuple(icon.sizes or [])) for icon in info.icons or []
            }
            assert icons == {
                (f"{settings.base_url}/{name}", mime_type, tuple(sizes))
                for name, (mime_type, sizes) in MARK.items()
            }, path

    anyio.run(scenario)


def test_an_app_that_names_an_icon_gives_it_to_its_client(server: Starlette, address: str) -> None:
    settings = server.state.apps.settings
    published_with_icon(server)
    token = app_token(server, "staff")

    async def scenario() -> None:
        async with app_client(address, SHOP, token) as client:
            info = client.server_info
        assert info is not None
        (icon,) = info.icons or []
        assert icon.src == f"{settings.base_url}/apps/etienne/shop/icon.svg"
        assert (icon.mime_type, icon.sizes) == ("image/svg+xml", ["any"])
        # A client fetching an icon sends no token.
        with httpx.Client(base_url=address) as http:
            answer = http.get(icon.src.removeprefix(settings.base_url))
        assert answer.status_code == 200
        assert answer.headers["content-type"] == "image/svg+xml"
        # The app's own accent behind, and the drawing its manifest names.
        assert "#0f766e" in answer.text
        assert str(elements("leaf")) in answer.text
        # Its validator says what it is drawn from, so a client's copy is dropped when the app
        # changes either.
        path = icon.src.removeprefix(settings.base_url)
        with httpx.Client(base_url=address) as http:
            again = http.get(path, headers={"if-none-match": answer.headers["etag"]})
            weak = http.get(path, headers={"if-none-match": f"W/{answer.headers['etag']}"})
            published_with_icon(server, "star")
            changed = http.get(path)
        assert again.status_code == 304
        assert again.content == b""
        # A proxy may weaken the tag, and the answer still revalidates.
        assert weak.status_code == 304
        assert changed.status_code == 200
        assert changed.headers["etag"] != answer.headers["etag"]
        assert str(elements("star")) in changed.text

    anyio.run(scenario)


def test_an_app_that_names_no_icon_keeps_the_mark(server: Starlette, address: str) -> None:
    # The stock example names an accent but no icon: it keeps Cliquet's mark, as it does today.
    settings = server.state.apps.settings
    token = app_token(server, "staff")

    async def scenario() -> None:
        async with app_client(address, SHOP, token) as client:
            info = client.server_info
        assert info is not None
        icons = {(icon.src, icon.mime_type, tuple(icon.sizes or [])) for icon in info.icons or []}
        assert icons == {
            (f"{settings.base_url}/{name}", mime_type, tuple(sizes))
            for name, (mime_type, sizes) in MARK.items()
        }

    anyio.run(scenario)


def test_the_icon_is_missing_without_an_app_or_without_an_icon(address: str) -> None:
    with httpx.Client(base_url=address) as http:
        # An app that names an accent but no icon: nothing links its icon.
        assert http.get("/apps/etienne/shop/icon.svg").status_code == 404
        assert http.get("/apps/etienne/nothing-here/icon.svg").status_code == 404


def test_the_site_can_be_another_address(tmp_path: Path, executor: LocalExecutor) -> None:
    settings = Settings.from_env(
        {
            "CLIQUET_DATA_DIR": str(tmp_path / "data"),
            "CLIQUET_BASE_URL": "https://cliquet.example",
            "CLIQUET_WEBSITE_URL": "https://cliquet.dev/",
            "CLIQUET_DEV_LOGIN": "etienne",
            "CLIQUET_ALLOW_REMOTE_DEV_LOGIN": "1",
            "CLIQUET_ALLOW_REMOTE_LOCAL_EXECUTOR": "1",
        }
    )
    app = create_app(settings, executor=executor, clock=lambda: NOW)
    app.state.apps.import_folder(APPS / "stock", "etienne", "shop")
    published_with_icon(app)
    token = app_token(app, "staff")

    async def scenario(address: str) -> None:
        # The server serves each icon itself, so its address is its own, never the site's.
        own = {"https://cliquet.example/apps/etienne/shop/icon.svg"}
        mark = {f"https://cliquet.example/{name}" for name in MARK}
        for path, bearer, icons in ((SHOP, token, own), (ME, "dev", mark)):
            async with app_client(address, path, bearer) as client:
                info = client.server_info
            assert info is not None, path
            assert info.website_url == "https://cliquet.dev", path
            assert {icon.src for icon in info.icons or []} == icons, path

    with running(app) as address:
        anyio.run(scenario, address)


def test_the_drawing_is_the_app_s_accent_and_its_icon() -> None:
    # The tile is the app's own pages' mark in a file of its own: the accent behind, and
    # Lucide's drawing in white, escaped as every value an app gives is.
    today = NOW
    leaf = AppInfo("etienne", "shop", "", today, today, "#0f766e", "leaf", None)
    drawn = drawing(leaf)
    assert drawn is not None
    assert 'fill="#0f766e"' in drawn
    assert 'stroke="#fff"' in drawn
    assert str(elements("leaf")) in drawn
    hostile = AppInfo("etienne", "shop", "", today, today, '"><script>', "leaf", None)
    escaped = drawing(hostile)
    assert escaped is not None
    assert "&#34;&gt;" in escaped


def test_an_app_without_an_icon_or_an_accent_keeps_the_mark() -> None:
    today = NOW
    accentless = AppInfo("etienne", "shop", "", today, today, None, "leaf", None)
    iconless = AppInfo("etienne", "shop", "", today, today, "#0f766e", None, None)
    both = AppInfo("etienne", "shop", "", today, today, "#0f766e", "leaf", None)
    base = "https://cliquet.example"
    mark = [f"{base}/favicon.svg", f"{base}/apple-touch-icon.png"]
    assert [icon.src for icon in app_icons(accentless, base)] == mark
    assert [icon.src for icon in app_icons(iconless, base)] == mark
    (icon,) = app_icons(both, base)
    assert (icon.src, icon.mime_type, icon.sizes) == (
        f"{base}/apps/etienne/shop/icon.svg",
        "image/svg+xml",
        ["any"],
    )
