"""The app's own icon at its own address: what its MCP endpoint gives a client to show, and
what a phone's home screen shows.

It is public, as the mark's files are, since a client that fetches an icon sends no token,
and a phone fetches a home screen's icon without the person's session.
"""

import logging
from collections.abc import Awaitable, Callable
from functools import cache

import anyio
from starlette.requests import Request
from starlette.responses import Response

from cliquet_server import __version__
from cliquet_server.app_icon import PNG, SVG, HomeIcon, drawing, home_drawing, home_png, look_key
from cliquet_server.apps import Apps
from cliquet_server.db import AppInfo
from cliquet_server.mark import STATIC, MarkFile

# The answer where there is no icon to draw: the app is missing or unpublished, or names no
# icon or no accent, and nothing links its icon then.
_NO_ICON = "There is no icon at this address."

_log = logging.getLogger(__name__)


@cache
def _mark_png(file: MarkFile) -> bytes:
    """One of Cliquet's marks as a home screen's icon, read once from the static folder."""
    return (STATIC / file.name).read_bytes()


def _matches(header: str | None, etag: str) -> bool:
    """Whether an `If-None-Match` header names this validator, compared weakly as RFC 9110
    asks, so a proxy that weakens it, or a list of validators, still revalidates."""
    return any(one.strip().removeprefix("W/") in (etag, "*") for one in (header or "").split(","))


def _send(request: Request, body: bytes | str, media_type: str, etag: str) -> Response:
    """`body` with its validator, or a 304 when the request already holds it."""
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={"etag": etag})
    return Response(body, media_type=media_type, headers={"etag": etag})


class AppIcon:
    """The route that draws an app's icon from its published record: it never starts the app,
    and the database is called in a thread, only for an app that isn't started."""

    def __init__(self, apps: Apps) -> None:
        self.apps = apps

    async def _info(self, request: Request) -> AppInfo | None:
        """The app's record: the live app's, else the database's, read in a thread."""
        login, name = request.path_params["login"], request.path_params["app"]
        live = self.apps.current(login, name)
        if live is not None:
            return live.info
        return await anyio.to_thread.run_sync(self.apps.database.app, login, name)

    async def answer(self, request: Request) -> Response:
        """The app's icon as an SVG tile, with a validator of what it is drawn from and by, so
        a client's cached icon is dropped when either changes."""
        info = await self._info(request)
        icon = None if info is None else drawing(info)
        if info is None or icon is None:
            return Response(_NO_ICON, status_code=404, media_type="text/plain")
        return _send(request, icon, SVG, f'"{__version__}-{info.icon}-{info.accent}"')

    def home(self, icon: HomeIcon) -> Callable[[Request], Awaitable[Response]]:
        """The route's endpoint for one of the icons a phone's home screen takes."""

        async def answer(request: Request) -> Response:
            """The app's icon as a phone's home screen shows it, a PNG at `icon`'s size: drawn
            once per look, in a thread, then kept in memory. Cliquet's mark when the app names
            no icon or no accent, or when its icon can't be drawn, which is tried again at the
            next request."""
            info = await self._info(request)
            if info is None:
                return Response(_NO_ICON, status_code=404, media_type="text/plain")
            svg = home_drawing(info)
            if svg is not None:
                etag = f'"{look_key(svg)}-{icon.size}"'
                if _matches(request.headers.get("if-none-match"), etag):
                    return Response(status_code=304, headers={"etag": etag})
                try:
                    png = await anyio.to_thread.run_sync(home_png, svg, icon.size)
                except Exception as error:
                    _log.warning(
                        "%s/%s: its home screen icon can't be drawn, so the mark is shown: %s",
                        info.login,
                        info.name,
                        error,
                    )
                else:
                    return Response(png, media_type=PNG, headers={"etag": etag})
            mark = await anyio.to_thread.run_sync(_mark_png, icon.mark)
            return _send(request, mark, PNG, f'"mark-{__version__}-{icon.size}"')

        return answer
