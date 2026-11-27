"""What every page of the web app shares: who may see an app, how a page is rendered, the pages
that refuse, and the checks on forms and calls."""

import contextlib
import json
import re
from collections.abc import Generator, Iterable, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from cliquet_server.app_auth import app_address, me_address, tokens_link
from cliquet_server.apps import Apps, Live, UnknownApp
from cliquet_server.builder.previews import Previews
from cliquet_server.db import AppInfo, Database, PersonInfo, Right, may
from cliquet_server.endpoints import role_label
from cliquet_server.executor import SandboxError
from cliquet_server.icons import icon_svg
from cliquet_server.limiter import Limits, admitted, logged, why
from cliquet_server.manifest import accent, checked_title, icon, label, title
from cliquet_server.pages import problem, render
from cliquet_server.settings import Settings
from cliquet_server.sign_in import days_left, reminder
from cliquet_server.web_sign_in import WebAuth

_LENGTH = re.compile(r"[0-9]{1,20}")
# The settings' tabs, in order, by their address and label. A person whose time to verify their
# email is over opens only the last, their account.
_SETTINGS_TABS = (("/assistants", "Assistants"), ("/tokens", "API tokens"), ("/account", "Account"))


def content_length(headers: Mapping[str, str]) -> int | None:
    """The length of the body a request's headers state, or None when they state none.
    Raises `ValueError` when it isn't a plain number."""
    length = headers.get("content-length")
    if length is None:
        return None
    if not _LENGTH.fullmatch(length):
        raise ValueError(f"the content length {length[:20]!r} isn't a number")
    return int(length)


class Site:
    """What every page needs: the apps, the settings and the sign-in, and what limits the
    calls of the apps' screens, if anything does."""

    def __init__(
        self,
        apps: Apps,
        settings: Settings,
        auth: WebAuth,
        limits: Limits | None = None,
        previews: Previews | None = None,
    ) -> None:
        self.apps = apps
        self.settings = settings
        self.auth = auth
        self.limits = limits
        # The builder's previews, which the console links to while one is open.
        self.previews = previews

    @contextlib.contextmanager
    def screen_call(
        self, key: str, what: str, who: str, owner: str
    ) -> Generator[JSONResponse | None]:
        """Run the block as one call of a screen, made by `who` in an app of `owner`'s,
        counted for `key`, yielding None, or yield the 429 that refuses it: once the month's
        sandbox time that holds the owner's sandbox is used, or when `what` got too many
        calls."""
        over = self.apps.over_budget(owner)
        if over is not None:
            headers = {"Retry-After": str(over.retry_after)}
            yield JSONResponse({"error": over.told(who == owner)}, 429, headers)
            return
        limits = self.limits
        if limits is None:
            yield None
            return
        with admitted(limits.calls, key) as refusal:
            if refusal is None:
                yield None
                return
        logged(refusal, limits.logged, who)
        headers = {"Retry-After": str(refusal.retry_after)}
        yield JSONResponse({"error": why(refusal, what)}, status_code=429, headers=headers)

    async def console(
        self, request: Request, person: PersonInfo, name: str, context: dict[str, Any]
    ) -> Response:
        """Render a console page, with the sidebar's apps and drafts, the links to build
        and manage them for a person who may build, and what Cliquet tells the person."""
        builds = may(person, Right.BUILD)
        # Without the builder there is no draft to preview or fill: the console shows none.
        previewing = self.previews.open_for(person.login) if self.previews and builds else None
        nav = await anyio.to_thread.run_sync(_nav, self.apps.database, person, previewing)
        csrf = self.auth.csrf_token(request) or ""
        # What a sign-in found, once. The starting page's probe of whether a page is ready
        # shows it to nobody: it takes none.
        if request.headers.get("Cliquet-Probe") is not None:
            told = []
        else:
            told = self.apps.notices.pop(person.id)
        # The days left to verify their email, on every page, with the button that sends a
        # new email when the server can.
        left = days_left(self.settings, person, self.apps.now())
        page = {
            "person": person,
            "nav": nav,
            "csrf": csrf,
            "builds": builds,
            # Whether the person makes apps here: they may build, and the builder is on.
            "makes_apps": builds and self.previews is not None,
            # Their own address, which their assistant connects to.
            "me_address": me_address(self.settings.base_url),
            "settings_tabs": _SETTINGS_TABS[-1:]
            if self.auth.locked_out(person)
            else _SETTINGS_TABS,
            "told": told,
            "reminder": reminder(self.settings, left, lost_email=False) if left else None,
            "send_email": self.auth.offers_email(person),
            # Where the button comes back to: a page a form's POST drew, such as a refusal,
            # can't be asked for again, so its section is.
            "send_email_next": (
                request.url.path if request.method == "GET" else context.get("here", "/")
            ),
        }
        return render(request, name, {**page, **context})

    async def owner_of(
        self, request: Request, *, builds: bool = True
    ) -> tuple[PersonInfo, AppInfo] | Response:
        """The signed-in owner and their published app, or the page that refuses it. Only a
        person who may build manages an app: anyone else is told it is missing. Not `builds`,
        as for the download, any owner gets their own app, a former builder too, and one whose
        time to verify their email is over.

        It reads only the database, and never starts the app.
        """
        login, name = request.path_params["login"], request.path_params["app"]
        person = await self.auth.person(request, even_locked_out=not builds)
        if person is None:
            return to_login(request)
        if person.login != login or (builds and not may(person, Right.BUILD)):
            return no_such_app(request, login, name)
        info = await anyio.to_thread.run_sync(self.apps.database.app, login, name)
        if info is None or info.published_at is None:
            return no_such_app(request, login, name)
        return person, info

    async def owned(self, request: Request) -> tuple[PersonInfo, Live] | Response:
        """The signed-in owner and their app, started, or the page that refuses it. One that
        can't be started says why, and where its error is kept."""
        found = await self.owner_of(request)
        if isinstance(found, Response):
            return found
        person, info = found
        login, name = info.login, info.name
        try:
            live = await self.apps.started(login, name)
        except UnknownApp:
            return no_such_app(request, login, name)
        except SandboxError as error:
            # The header from the app's record, on the tab the address names. The Errors tab
            # needs no start, so it is never this page.
            context = manage_header(info, "")
            tab = request.url.path.removeprefix(context["here"]).strip("/").partition("/")[0]
            # A data file's page belongs to the overview, which lists the files.
            context["tab"] = "overview" if tab in ("", "data") else tab
            context.update({"why": str(error), "error_id": error.error_id})
            response = await self.console(request, person, "manage_not_started.html", context)
            response.status_code = 503
            return response
        return person, live

    async def draft(
        self, request: Request, json: bool = False
    ) -> tuple[PersonInfo, AppInfo] | Response:
        """The signed-in owner of the draft and the app, or the answer that refuses them.

        Anyone but the owner, and an owner who may not build, is told the app is missing, as a
        page, or as JSON for a call. Nothing here starts a preview.
        """
        name = request.path_params["app"]
        person = await self.auth.person(request)
        if person is None:
            if json:
                return JSONResponse({"error": "sign in to use this app"}, status_code=401)
            return to_login(request)
        info = None
        if may(person, Right.BUILD):
            info = await anyio.to_thread.run_sync(self.apps.database.app, person.login, name)
        if info is None:
            if json:
                return JSONResponse(
                    {"error": f"there is no app {person.login}/{name}"}, status_code=404
                )
            return no_such_app(request, person.login, name)
        return person, info


def _nav(
    database: Database, person: PersonInfo, previewing: set[str] | None
) -> dict[str, list[dict[str, Any]]]:
    """The person's published apps, the published apps shared with them, with roles, and the
    person's drafts that were never published. An app of `previewing` has a preview open; with
    None, the builder is off or the person may not build, and no draft is listed. Only a
    person who may build has their own apps link to the pages that manage them."""
    roles = roles_by_app(
        (held.app.login, held.app.name, role_label(held.role))
        for held in database.memberships(person.id)
    )
    own, shared, drafts = [], [], []
    manages = may(person, Right.BUILD)
    for info in database.visible_apps(person.login):
        mine = info.login == person.login
        draft: dict[str, Any] = {}
        if mine and previewing is not None:
            draft = {
                "preview": f"/build/{info.name}/preview",
                "upload": f"/build/{info.name}/upload",
                "previewing": info.name in previewing,
            }
        if info.published_at is None:
            if draft:
                drafts.append({**app_identity(info), **draft, "href": draft["preview"]})
            continue
        item = {**app_identity(info), "published": day(info.published_at)}
        if mine:
            href = f"/{'manage' if manages else 'apps'}/{info.login}/{info.name}"
            own.append({**item, **draft, "href": href, "roles": ["owner"]})
        else:
            held = roles.get((info.login, info.name), [])
            shared.append({**item, "href": f"/apps/{info.login}/{info.name}", "roles": held})
    return {"own": own, "shared": shared, "drafts": drafts}


def roles_by_app(roles: Iterable[tuple[str, str, str]]) -> dict[tuple[str, str], list[str]]:
    """Group (owner, app, role) into one entry per app, with its roles in the order given."""
    apps: dict[tuple[str, str], list[str]] = {}
    for login, name, role in roles:
        apps.setdefault((login, name), []).append(role)
    return apps


def app_identity(info: AppInfo, manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """What names an app on a page: its owner, name, title, tile and purpose. The title and
    the tile take the look of `manifest`, the version the page shows, else the registry's: the
    app's own title or its name's label, its accent, and its icon drawn inline, or its
    letter."""
    if manifest is not None:
        tint, drawn, words = accent(manifest), icon(manifest), title(manifest)
    else:
        tint, drawn, words = info.accent, info.icon, info.title
    return {
        "login": info.login,
        "name": info.name,
        "title": words or label(info.name),
        "tile": {
            # The title's first letter or digit, so "(Beta) Stock" shows B, else the name's.
            "letter": next((c for c in words or "" if c.isalnum()), info.name[:1]).upper(),
            "accent": tint,
            "icon": icon_svg(drawn),
        },
        "description": info.description,
    }


def manage_header(info: AppInfo, tab: str) -> dict[str, Any]:
    """What every console page of one app shows, its header and its tabs, from the app's
    record: so a page that needs no start shows it while the app can't be started."""
    published = info.published_at
    return {
        **app_identity(info),
        "published": day(published) if published else "",
        "tab": tab,
        "here": f"/manage/{info.login}/{info.name}",
    }


def manage_context(app: Live | AppInfo, tab: str) -> dict[str, Any]:
    """The header and tabs of one app's console page, from the app or, unstarted, its record."""
    # A page that needs no start, such as the Errors tab, gives the record: its description
    # is cut.
    if isinstance(app, AppInfo):
        return manage_header(app, tab)
    return {**manage_header(app.info, tab), "description": app.runtime.manifest["description"]}


def addresses(settings: Settings, live: Live) -> dict[str, str]:
    """Where to reach the app over MCP: the person's own endpoint for their assistant, the
    app's address for an agent that runs on its own, and the page that makes its token."""
    login, name = live.info.login, live.info.name
    return {
        "me_address": me_address(settings.base_url),
        "app_address": app_address(settings.base_url, login, name),
        "token_link": tokens_link(login, name),
    }


def pick_view(request: Request, server: Any) -> dict[str, Any] | Response:
    """The server's views as tabs, and the one `?view=` picks, or the page saying it is
    missing."""
    views = [tool for tool in server.tools if tool.meta is not None and "ui" in tool.meta]
    view = request.query_params.get("view") or (views[0].name if views else None)
    if views and view not in {tool.name for tool in views}:
        return problem(request, 404, "No such screen", f"There is no screen {view}.")
    labels = {tool.name: view_label(tool.name, tool.title) for tool in views}
    return {
        "views": [
            {"name": tool.name, "label": labels[tool.name], "on": tool.name == view}
            for tool in views
        ],
        "view": view,
        "label": labels[view] if view else "",
    }


def foreign_call(request: Request, settings: Settings) -> str | None:
    """Why a screen's call didn't come from this server's own page, or None when it did.

    The server's own origin is the one in `CLIQUET_BASE_URL`, or the address the request was
    sent to, so that a local server on another port or host name still draws its screens.
    """
    if other_site(request, settings):
        return "this call came from another site; open the app on its own page"
    kind = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if kind != "application/json":
        return "a screen's call is JSON: send it with the content type application/json"
    return None


def other_site(request: Request, settings: Settings) -> bool:
    """Whether the browser says the request came from another site's page."""
    origin = request.headers.get("origin")
    own = {_origin(settings.base_url), _origin(str(request.url))}
    return origin is not None and origin.rstrip("/").lower() not in own


def _origin(url: str) -> str:
    """The scheme, host and port of an address, as a browser's Origin header says them."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def refusal_text(refused: JSONResponse) -> str:
    """The sentence a JSON refusal, such as `Site.screen_call`'s, says."""
    return json.loads(bytes(refused.body))["error"]


def form_expired(request: Request) -> Response:
    """The page for a form whose token doesn't match."""
    return problem(
        request, 403, "The form expired", "The form expired. Reload the page and try again."
    )


def no_such_app(request: Request, login: str, name: str) -> Response:
    """The page for an app that is missing, or not the person's."""
    return problem(request, 404, "No such app", f"There is no app {login}/{name} here.")


def to_login(request: Request) -> Response:
    """Send the person to sign in, then back to this page."""
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(f"/login?{urlencode({'next': target})}", status_code=303)


def form_value(value: Any) -> str:
    """A form's field as text: empty when it is missing or a file."""
    return value if isinstance(value, str) else ""


def readable_size(size: int) -> str:
    """A number of bytes, as a person reads it."""
    if size < 1024:
        return f"{size} bytes"
    return f"{size / 1024:,.0f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:,.1f} MB"


def app_label(info: AppInfo) -> str:
    """An app's name as people read it: the title it gave, or its name as a page shows it."""
    return info.title or label(info.name)


def view_label(name: str, title: object) -> str:
    """A view's tab: the title its app gave it, or its name as a page shows it."""
    return checked_title(title) or label(name)


def view_labels(manifest: Mapping[str, Any]) -> dict[str, str]:
    """Each view's tab, by the view's name, from the app's manifest."""
    return {view["name"]: view_label(view["name"], view.get("title")) for view in manifest["views"]}


def day(moment: datetime) -> str:
    """A day, as a page shows it."""
    return f"{moment.day} {moment:%B %Y}"


def readable_time(text: str) -> str:
    """A time in ISO 8601, as a page shows it, to the minute."""
    moment = datetime.fromisoformat(text)
    return f"{moment.day} {moment:%b %Y, %H:%M}"
