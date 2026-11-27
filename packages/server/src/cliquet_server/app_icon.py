"""An app's own icon: its Lucide drawing on its accent, drawn by the server and served at the
app's own address, for the clients of its MCP endpoint and for a phone's home screen.

It is drawn from the app's manifest and Lucide's drawings alone, never from a file of the
app's. A phone's home screen takes a raster, so the server draws that one as PNGs, at the
first request for a look, and keeps them in memory.
"""

import hashlib
from functools import lru_cache
from typing import NamedTuple

import mcp_types as types
import resvg_py
from markupsafe import Markup

from cliquet_server.db import AppInfo
from cliquet_server.icons import elements, framed
from cliquet_server.mark import APPLE_TOUCH_ICON, MARK_192, MARK_512, MarkFile, mark_icons

# The word under an app's address where its icon is served, which no role may take.
ICON = "icon.svg"
# The types an icon is answered with.
SVG = "image/svg+xml"
PNG = "image/png"


class HomeIcon(NamedTuple):
    """One of the icons a phone's home screen takes: the size it is drawn at, Cliquet's mark
    at that size for an app that names no icon or no accent, or whose icon can't be drawn
    yet, and whether a web app's manifest lists it, or only the page's link, for iOS."""

    size: int
    mark: MarkFile
    listed: bool


# The icons a phone's home screen takes, by their name under the app's address: the one iOS
# reads from the page's link, then the two a web app's manifest lists. Each name has a dot,
# so no role takes it.
APPLE_ICON = APPLE_TOUCH_ICON.name
HOME_ICONS = {
    APPLE_ICON: HomeIcon(180, APPLE_TOUCH_ICON, listed=False),
    "icon-192.png": HomeIcon(192, MARK_192, listed=True),
    "icon-512.png": HomeIcon(512, MARK_512, listed=True),
}
# An app's pages draw its mark as a 2rem square with a rounded corner, the accent behind and
# Lucide's drawing in white at 62.5% of it: `.mark[style]` and `.mark > svg` in `cliquet.css`.
# The corner is that file's `--radius-field`, 0.6rem of the 2rem and so 9.6 of 32, and the
# drawing keeps Lucide's 24 by 24 frame, in a 20 by 20 box at 6 in.
_SQUARE = Markup(
    '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="9.6" fill="{accent}"/>{inner}</svg>'
)


def _tile(info: AppInfo, frame: Markup, at: str, size: str) -> Markup | None:
    """The app's Lucide drawing in white, in a `size` box `at` in, on its accent in `frame`,
    or None when it names no icon or no accent."""
    if info.icon is None or info.accent is None:
        return None
    parts = elements(info.icon)
    if parts is None:
        return None
    inner = framed(parts, stroke="#fff", x=at, y=at, width=size, height=size)
    return frame.format(accent=info.accent, inner=inner)


def drawing(info: AppInfo) -> Markup | None:
    """The app's icon as a standalone SVG tile, or None when it names no icon or no accent."""
    return _tile(info, _SQUARE, "6", "20")


# A home screen's icon fills its square: iOS masks the corners itself and paints what is
# transparent black, and Android crops a maskable icon to its own shape. So the accent fills
# the whole square, and Lucide's drawing stays inside the 80% circle every mask keeps: its
# 24 by 24 frame, whose strokes reach 1 from each edge, in an 18 by 18 box at 7 in, reaches
# 11.7 from the centre, under the circle's 12.8.
_FULL = Markup(
    '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">'
    '<rect width="32" height="32" fill="{accent}"/>{inner}</svg>'
)


def home_drawing(info: AppInfo) -> Markup | None:
    """The app's icon as a home screen shows it, a full square, as SVG, or None when it names
    no icon or no accent."""
    return _tile(info, _FULL, "7", "18")


def look_key(drawing: str) -> str:
    """A short hash of what a home screen's icon is drawn from: a new icon, accent, Lucide
    version or layout gives a new one, so its PNGs are drawn again."""
    return hashlib.sha256(drawing.encode()).hexdigest()[:16]


@lru_cache(maxsize=256)
def home_png(drawing: str, size: int) -> bytes:
    """The PNG of `drawing` at `size`, drawn once per look and size, then kept in memory. It
    blocks: a handler calls it in a thread. Raises when it can't be drawn, which isn't kept,
    so the next request draws it again."""
    return resvg_py.svg_to_bytes(
        svg_string=drawing, width=size, height=size, skip_system_fonts=True
    )


def icon_path(info: AppInfo) -> str | None:
    """The path of the app's icon under the server's own address when it has one to show, else
    None: what an endpoint gives a client, and what the app's own page links in a browser's
    tab."""
    return None if drawing(info) is None else f"/apps/{info.login}/{info.name}/{ICON}"


def app_icons(info: AppInfo, base_url: str) -> list[types.Icon]:
    """What an app's MCP endpoint gives a client to show: the app's own icon when it draws one,
    else Cliquet's mark, as the person's endpoint gives it."""
    path = icon_path(info)
    if path is None:
        return mark_icons(base_url)
    return [types.Icon(src=f"{base_url}{path}", mime_type=SVG, sizes=["any"])]
