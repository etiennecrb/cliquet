"""An app's own pages, where its owner and members use it: a page for each role, the screen
that draws a view, the screen's calls, and the web app a phone installs for each role."""

from collections.abc import Mapping
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from cliquet.host import answer_rpc, host_page
from cliquet.tools import listed
from cliquet_server.app_icon import APPLE_ICON, HOME_ICONS, PNG, icon_path
from cliquet_server.db import PersonInfo, Right, may
from cliquet_server.endpoints import (
    NoRole,
    NotStarted,
    Opened,
    Registry,
    ShareWaiting,
    UnknownRole,
    Unopened,
    app_key,
    role_label,
)
from cliquet_server.manifest import accent, language
from cliquet_server.pages import problem, render

from .common import (
    Site,
    addresses,
    app_identity,
    foreign_call,
    no_such_app,
    pick_view,
    to_login,
)

# The web app's manifest of each role's page, under the page's address: the name has a dot,
# so no role takes it.
WEB_MANIFEST = "manifest.webmanifest"
# Where the installed app opens before its page draws: the page's own background, daisyUI's
# light base.
_WHITE = "#ffffff"
# The one answer for anyone who can't open the role, so it says nothing of the app.
_NO_MANIFEST = {"error": "there is no web app at this address"}
# A manifest is the person's own, as the page it describes: no shared cache keeps it.
_PRIVATE = {"cache-control": "private"}


def home_name(title: str, role: str, held: list[str]) -> str:
    """The name of a role's page on a home screen: the app's title for a person with one
    role, else the title and the role, so the apps they add for each role tell apart."""
    return title if len(held) <= 1 else f"{title} · {role_label(role)}"


def _json_refusal(
    error: Unopened, login: str, name: str, role: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    """Why the person can't open the app as `role`, as JSON, for a call no person reads."""
    if isinstance(error, NoRole):
        status, message = 403, f"you don't have the role {role} for this app"
    elif isinstance(error, UnknownRole):
        status, message = 404, str(error)
    elif isinstance(error, NotStarted) and error.retry_after is not None:
        # The month's sandbox time is used: the sentence says until when.
        status, message = 429, error.why
        headers = {**(headers or {}), "Retry-After": str(error.retry_after)}
    elif isinstance(error, NotStarted):
        status, message = 503, f"the app can't be started: {error.why}"
    else:
        status, message = 404, f"there is no app {login}/{name}"
    return JSONResponse({"error": message}, status_code=status, headers=headers)


class AppPages:
    """The pages of an app, for each person who may open it."""

    def __init__(self, site: Site, registry: Registry) -> None:
        self.apps = site.apps
        self.settings = site.settings
        self.auth = site.auth
        self.site = site
        self.registry = registry

    async def open_app(self, request: Request) -> Response:
        """Open the app on the first role the person holds."""
        login, name = request.path_params["login"], request.path_params["app"]
        person = await self.auth.person(request)
        if person is None:
            return to_login(request)
        try:
            opened = await self.registry.open_for(person, login, name, None)
        except NoRole:
            return problem(request, 403, "You don't have a role", "You don't have a role here.")
        except Unopened as error:
            return self._refusal(request, error, prompt=True)
        return RedirectResponse(f"/apps/{login}/{name}/{opened.address}", status_code=303)

    async def role_page(self, request: Request) -> Response:
        """The app's page for one role: its screens, tools, rules and MCP address."""
        login, name = request.path_params["login"], request.path_params["app"]
        role = request.path_params["role"]
        found = await self._opened(request, login, name, role, prompt=True)
        if isinstance(found, Response):
            return found
        person, opened = found
        server, live, held = opened.server, opened.live, opened.held
        picked = pick_view(request, server)
        if isinstance(picked, Response):
            return picked
        # The app the server was made from, if a publish just replaced it.
        if live.runtime is not server.runtime:
            current = self.apps.current(login, name)
            live = current if current is not None else await self.apps.started(login, name)
            held = await anyio.to_thread.run_sync(self.apps.held_roles, person, live)
        manifest = server.runtime.manifest
        identity = app_identity(live.info, manifest)
        here = f"/apps/{login}/{name}"
        context = {
            **identity,
            # The icon its own page links in a browser's tab: the one its endpoint gives a
            # client, or none for Cliquet's mark.
            "icon_href": icon_path(live.info),
            # What a phone adds to its home screen: the page as a web app of its own.
            "manifest_href": f"{here}/{role}/{WEB_MANIFEST}",
            "home_icon": f"{here}/{APPLE_ICON}",
            "home_name": home_name(identity["title"], role, held),
            "accent": accent(manifest),
            "role": role,
            "roles": held,
            # The owner's link to manage the app, while they may build.
            "owner": person.login == login and may(person, Right.BUILD),
            **picked,
            **addresses(self.settings, live),
        }
        return render(request, "app.html", context)

    async def web_manifest(self, request: Request) -> Response:
        """The role's page as a web app a phone installs: its name, address, colours and
        icons. Only for a person who may open the page, as its session says: anyone else is
        told there is none, with nothing of the app, never sent to sign in."""
        login, name = request.path_params["login"], request.path_params["app"]
        role = request.path_params["role"]
        person = await self.auth.person(request)
        if person is None:
            return JSONResponse(_NO_MANIFEST, status_code=404, headers=_PRIVATE)
        try:
            opened = await self.registry.open_for(person, login, name, role)
        except NotStarted as error:
            return _json_refusal(error, login, name, role, _PRIVATE)
        except Unopened:
            return JSONResponse(_NO_MANIFEST, status_code=404, headers=_PRIVATE)
        manifest = opened.server.runtime.manifest
        title = app_identity(opened.live.info, manifest)["title"]
        tint = accent(manifest)
        here = f"/apps/{login}/{name}"
        # The page itself, with no trailing slash: where the app opens, what it is known by,
        # and every address it keeps in its own window, its screens' frames included.
        page = f"{here}/{role}"
        named = home_name(title, role, opened.held)
        icons = [
            {
                "src": f"{here}/{file}",
                "sizes": f"{icon.size}x{icon.size}",
                "type": PNG,
                "purpose": purpose,
            }
            for purpose in ("any", "maskable")
            for file, icon in HOME_ICONS.items()
            if icon.listed
        ]
        content: dict[str, Any] = {
            "id": page,
            "name": named,
            "short_name": named,
            "start_url": page,
            "scope": page,
            "display": "standalone",
            "background_color": _WHITE,
            "icons": icons,
        }
        # An app's bar takes its accent, as the page's own meta does, else the browser's.
        if tint is not None:
            content["theme_color"] = tint
        # The app's screens are in English when it declares no language.
        content["lang"] = language(manifest) or "en"
        return JSONResponse(content, media_type="application/manifest+json", headers=_PRIVATE)

    async def screen(self, request: Request) -> Response:
        """The frame that draws the app's screens for one role."""
        login, name = request.path_params["login"], request.path_params["app"]
        role = request.path_params["role"]
        found = await self._opened(request, login, name, role)
        if isinstance(found, Response):
            return found
        return HTMLResponse(host_page())

    async def rpc(self, request: Request) -> Response:
        """Answer a call from a screen, as the signed-in person and role."""
        login, name = request.path_params["login"], request.path_params["app"]
        role = request.path_params["role"]
        refused = foreign_call(request, self.settings)
        if refused is not None:
            return JSONResponse({"error": refused}, status_code=403)
        person = await self.auth.person(request)
        if person is None:
            return JSONResponse({"error": "sign in to use this app"}, status_code=401)
        try:
            opened = await self.registry.open_for(person, login, name, role)
        except Unopened as error:
            return _json_refusal(error, login, name, role)
        key, who = app_key(login, name), person.login
        with self.site.screen_call(key, f"{login}/{name}", who, login) as over:
            if over is not None:
                return over
            return await answer_rpc(opened.server, await request.body())

    async def _opened(
        self, request: Request, login: str, name: str, role: str, *, prompt: bool = False
    ) -> tuple[PersonInfo, Opened] | Response:
        """The person and the app opened for them as this role, or the page that refuses it.
        With `prompt`, a share that waits for the person is shown, for them to accept."""
        person = await self.auth.person(request)
        if person is None:
            return to_login(request)
        try:
            return person, await self.registry.open_for(person, login, name, role)
        except Unopened as error:
            return self._refusal(request, error, prompt=prompt)

    def _refusal(self, request: Request, error: Unopened, *, prompt: bool) -> Response:
        """The page that says why the person can't open the app. With `prompt`, a share that
        waits for them, with only what Cliquet knows itself; else the page of a missing app."""
        login, name = request.path_params["login"], request.path_params["app"]
        if isinstance(error, ShareWaiting) and prompt:
            context = {
                "login": login,
                "name": name,
                "roles": listed([role_label(role) for role in error.roles]),
                "csrf": self.auth.csrf_token(request) or "",
            }
            return render(request, "accept_share.html", context)
        if isinstance(error, NoRole):
            why = f"You don't have the role {error.role} for this app."
            return problem(request, 403, "That role isn't yours", why)
        if isinstance(error, UnknownRole):
            why = error.listed or f"There is no role {error.role} here."
            return problem(request, 404, "No such role", why)
        if isinstance(error, NotStarted):
            return problem(request, 503, "The app can't be started", error.why)
        return no_such_app(request, login, name)
