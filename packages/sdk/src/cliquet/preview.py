"""`cliquet preview`: see an app's views in a browser, without an assistant.

It serves `cliquet.host`'s page, which plays the MCP Apps host, and passes the view's tool
calls to the app through the same `AppServer` an assistant would use.
"""

import socket
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from cliquet.host import answer_rpc, host_page
from cliquet.limits import MAX_BODY, read_body
from cliquet.mcp_server import AppServer
from cliquet.screenshot import Theme


def preview_app(
    server: AppServer, views: list[str] | None = None, *, driver: bool = False
) -> Starlette:
    """The preview's web app: the host page at `/`, and the app's tools behind `/rpc`."""
    allowed = set(views) if views is not None else None

    async def page(request: Request) -> HTMLResponse:
        return HTMLResponse(host_page(driver))

    async def rpc(request: Request) -> JSONResponse:
        return await answer_rpc(server, await read_body(request, MAX_BODY), allowed)

    return Starlette(routes=[Route("/", page), Route("/rpc", rpc, methods=["POST"])])


def serve_preview(server: AppServer, host: str, port: int, views: list[str] | None = None) -> None:
    """Serve the preview at http://host:port/ until interrupted."""
    import uvicorn

    uvicorn.run(preview_app(server, views), host=host, port=port, log_level="warning")


@contextmanager
def preview_server(
    server: AppServer, views: list[str] | None = None, *, driver: bool = False
) -> Generator[str]:
    """Serve the preview on a free local port, and give its address.

    With `driver`, the page lets `cliquet preview --script` read the screen's document.
    """
    import uvicorn

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(
        preview_app(server, views, driver=driver), host="127.0.0.1", port=port, log_level="warning"
    )
    web = uvicorn.Server(config)
    thread = threading.Thread(target=web.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not web.started and time.monotonic() < deadline:
            time.sleep(0.05)
        yield f"http://127.0.0.1:{port}"
    finally:
        web.should_exit = True
        thread.join(timeout=5)


def screenshot_preview(
    server: AppServer,
    views: list[str],
    folder: Path,
    browser_path: str | None = None,
    no_sandbox: bool = False,
    theme: Theme = "light",
) -> list[tuple[Path, int]]:
    """Serve the preview for as long as it takes to save a picture of each view, in `theme`."""
    from cliquet.screenshot import take_screenshots

    with preview_server(server, views) as base_url:
        return take_screenshots(
            base_url, views, folder, browser_path=browser_path, no_sandbox=no_sandbox, theme=theme
        )
