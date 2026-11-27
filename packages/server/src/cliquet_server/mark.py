"""Cliquet's mark: its files, and what an MCP client is told about them."""

from pathlib import Path
from typing import NamedTuple

import mcp_types as types

# The server's static folder, where the mark's files are kept with the pages' other assets.
STATIC = Path(__file__).parent / "static"


class MarkFile(NamedTuple):
    """One of the mark's files: its name, the type it is served as, and the sizes it is drawn
    at."""

    name: str
    media_type: str
    sizes: tuple[str, ...]


# The mark as a PNG at 180, which iOS also puts on a home screen.
APPLE_TOUCH_ICON = MarkFile("apple-touch-icon.png", "image/png", ("180x180",))
# The mark's files that an MCP client shows, as the website keeps them, each with the type it
# is served as and the sizes it is drawn at.
ICONS = (MarkFile("favicon.svg", "image/svg+xml", ("any",)), APPLE_TOUCH_ICON)
# Every file the server serves at its root: the client's icons, then the icon a browser asks
# for by default, which no client is told about.
FILES = (*ICONS, MarkFile("favicon.ico", "image/x-icon", ()))
# The mark as a web app's home screen icon at 192 and 512, for an app that names no icon or
# no accent, or whose icon can't be drawn yet. They are in the static folder but not served
# at the root: an app's own address serves them. They were drawn once with resvg-py, as
# `apple-touch-icon.png` was: `favicon.svg`'s shape on a full square, at 78% of it around
# its centre, inside the 80% circle a maskable icon keeps.
MARK_192 = MarkFile("mark-192.png", "image/png", ("192x192",))
MARK_512 = MarkFile("mark-512.png", "image/png", ("512x512",))


def mark_icons(base_url: str) -> list[types.Icon]:
    """The mark's files at `base_url`, which a client reads from `serverInfo`."""
    return [
        types.Icon(src=f"{base_url}/{file.name}", mime_type=file.media_type, sizes=list(file.sizes))
        for file in ICONS
    ]
