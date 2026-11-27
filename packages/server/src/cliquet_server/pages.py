"""Rendered pages shared by the web app and its sign-in."""

import re
from pathlib import Path
from typing import Any

from markupsafe import Markup
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.templating import Jinja2Templates

from cliquet import views
from cliquet_server import __version__
from cliquet_server.endpoints import rpc_error
from cliquet_server.icons import icon_svg
from cliquet_server.sign_in import LockedOut


def _privacy(request: Request) -> dict[str, Any]:
    """The operator's privacy notice, which every page links to when the settings name one."""
    app = request.scope.get("app")
    settings = getattr(getattr(app, "state", None), "settings", None)
    return {"privacy_url": getattr(settings, "privacy_url", None)}


TEMPLATES = Jinja2Templates(
    directory=Path(__file__).parent / "templates", context_processors=[_privacy]
)
# An app's pages take its accent as its screens do, by the SDK's one rule. It is Cliquet's own
# CSS, written out as it is.
_ACCENT_CSS = Markup(views.ACCENT_CSS)


def render(request: Request, name: str, context: dict[str, Any]) -> Response:
    """Render one of the server's templates."""
    # `icon` draws one of Lucide's icons by name on the server's own pages.
    defaults = {"version": __version__, "accent_css": _ACCENT_CSS, "icon": icon_svg}
    return TEMPLATES.TemplateResponse(request, name, {**defaults, **context})


def problem(
    request: Request,
    status: int,
    title: str,
    sentence: str,
    action: tuple[str, str] = ("/", "Back to your apps"),
    *,
    signed_in: str | None = None,
    template: str = "problem.html",
) -> Response:
    """A small page for what can't be shown, with one button: `action`, its address and
    label. With `signed_in`, the session's form token, it links to the person's account too,
    and signs them out. `template` may extend `problem.html`."""
    href, label = action
    context = {"version": __version__, "title": title, "sentence": sentence}
    return TEMPLATES.TemplateResponse(
        request,
        template,
        {**context, "action_href": href, "action_label": label, "signed_in": signed_in},
        status_code=status,
    )


# The calls that answer JSON: an app's MCP endpoint, its screens' calls, each role's web app
# manifest, and a draft's screens' calls on its preview. The path's whole end, since an app or
# a role may be called `mcp` or `rpc`.
_CALLS = re.compile(
    r"/(?:apps/[^/]+/[^/]+/(mcp|[^/]+/rpc|[^/]+/manifest\.webmanifest)"
    r"|build/[^/]+/preview/[^/]+/rpc)$"
)


async def starting(request: Request, exc: Exception) -> Response:
    """The answer while the app a request needs starts in the background (`AppStarting`): a
    JSON-RPC error at an app's MCP endpoint, JSON for a screen's call or a web app's manifest,
    else a page. A page to GET shows a spinner and asks for itself again until the app has
    started, then opens; a form sent isn't sent again by itself."""
    retry = {"Retry-After": "30"}
    if called := _CALLS.search(request.url.path):
        error = "the app is starting: try again in a minute"
        if called[1] == "mcp":
            return rpc_error(503, error, retry)
        return JSONResponse({"error": error}, status_code=503, headers=retry)
    if request.method in {"GET", "HEAD"}:
        sentence = (
            "It is usually ready in a few seconds, or in a minute or so after an update, and "
            "opens here by itself."
        )
        response = problem(request, 503, "The app is starting", sentence, template="starting.html")
    else:
        sentence = (
            "It gets ready in a minute or so, after an update. Go back and send it again then."
        )
        response = problem(request, 503, "The app is starting", sentence)
    response.headers.update(retry)
    return response


async def verify_email(request: Request, exc: Exception) -> Response:
    """The answer to every page and screen call of a person whose time to verify their email
    is over (`LockedOut`): JSON for a screen's call, a draft's on its preview too, or a web
    app's manifest, else a page that signs them in again, links to their account page, where
    they still download their apps or delete their account, and signs them out."""
    assert isinstance(exc, LockedOut)
    if _CALLS.search(request.url.path):
        return JSONResponse({"error": exc.sentence}, status_code=403)
    again = ("/login", "Sign in again")
    return problem(request, 403, "Verify your email", exc.sentence, again, signed_in=exc.csrf)


async def not_found(request: Request, exc: Exception) -> Response:
    """The page for an address that isn't a page."""
    return no_page(request)


def no_page(request: Request) -> Response:
    """The page for an address that isn't a page, or not one for this person."""
    return problem(request, 404, "Not found", "There is no page at this address.")
