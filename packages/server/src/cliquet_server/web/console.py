"""The console: the home page with its guide to building an app, the settings' Assistants and
API tokens tabs, and the pages where an owner manages an app, its sharing forms included."""

from collections import Counter
from datetime import datetime, timedelta
from functools import partial
from typing import Any
from urllib.parse import quote

import anyio
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from cliquet.files import is_pattern, matches
from cliquet.tools import REVERT, NoSuchRole, access, app_role, listed
from cliquet_server import person_tokens, sharing
from cliquet_server import tokens as api_tokens
from cliquet_server.app_auth import limit_value
from cliquet_server.app_errors import summary
from cliquet_server.apps import Apps, AppStarting, Live, UnknownApp
from cliquet_server.arguments import Refusal
from cliquet_server.builder import Builder, BuilderError, builder_enabled
from cliquet_server.db import (
    ERRORS_KEPT,
    ERRORS_KEPT_FOR,
    AppInfo,
    Database,
    ErrorInfo,
    PersonInfo,
    Right,
    TokenInfo,
    TokenLimit,
    may,
)
from cliquet_server.diffs import change_entry
from cliquet_server.endpoints import Registry, app_key, role_label, role_of
from cliquet_server.executor import SandboxError
from cliquet_server.manifest import rule_texts, tool_rows
from cliquet_server.pages import problem, render
from cliquet_server.repos import is_publish
from cliquet_server.settings import is_local
from cliquet_server.web_sign_in import WebAuth

from .changes import change_page, latest_changes, versions
from .common import (
    Site,
    addresses,
    form_expired,
    form_value,
    manage_context,
    manage_header,
    no_such_app,
    readable_size,
    readable_time,
    refusal_text,
    roles_by_app,
    to_login,
    view_labels,
)

# How many of the latest changes the overview lists.
_RECENT = 3
# The widths the preview offers: a phone, a conversation, and the whole window.
_WIDTHS = {"390": "Phone", "768": "Conversation", "full": "Full width"}


class Console:
    """The console's pages."""

    def __init__(self, site: Site, builder_core: Builder | None, registry: Registry) -> None:
        self.site = site
        # The apps' MCP servers, through which the owner undoes a change as `app_revert` does.
        self.registry = registry
        # The builder's drafts, through which a version is restored.
        self.builder_core = builder_core
        self.apps = site.apps
        self.settings = site.settings
        self.auth = site.auth
        settings = site.settings
        # The login anyone at a public address builds as, on a demo with the development login.
        demo = builder_enabled(settings) and settings.allow_remote_dev_login
        self.demo = settings.dev_login if demo and not is_local(settings.base_url) else None

    async def home(self, request: Request) -> Response:
        """The signed-in person's home, or sign-in: the website says what Cliquet is."""
        person = await self.auth.person(request)
        if person is None:
            return to_login(request)
        pending = await anyio.to_thread.run_sync(self.apps.database.pending_shares, person.id)
        # One row per app: accepting it takes every role shared there.
        apps = roles_by_app((share.login, share.name, role_label(share.role)) for share in pending)
        context = {
            "here": "/",
            # The guide to building an app, which shows until the first is published.
            "asked": "new" in request.query_params,
            "demo": self.demo,
            "pending": [
                {"login": login, "name": name, "roles": roles}
                for (login, name), roles in apps.items()
            ],
            "notice": _HOME_NOTICES.get(request.query_params.get("notice") or "", ""),
        }
        return await self.site.console(request, person, "home.html", context)

    async def accept_share(self, request: Request) -> Response:
        """Accept an app shared with the signed-in person: open it if asked, else go home."""
        posted = await self._posted(request)
        if isinstance(posted, Response):
            return posted
        person, form = posted
        login, name = request.path_params["login"], request.path_params["app"]
        roles = await anyio.to_thread.run_sync(
            self.apps.database.accept_share, person.id, login, name, self.apps.now()
        )
        if not roles:
            return _nothing_waits(request, login, name)
        if form_value(form.get("open")):
            return RedirectResponse(f"/apps/{login}/{name}", status_code=303)
        return RedirectResponse("/?notice=accepted", status_code=303)

    async def decline_share(self, request: Request) -> Response:
        """Decline an app someone shared with the signed-in person."""
        posted = await self._posted(request)
        if isinstance(posted, Response):
            return posted
        person, _form = posted
        login, name = request.path_params["login"], request.path_params["app"]
        declined = await anyio.to_thread.run_sync(
            self.apps.database.decline_share, person.id, login, name
        )
        if not declined:
            return _nothing_waits(request, login, name)
        return RedirectResponse("/?notice=declined", status_code=303)

    async def _posted(self, request: Request) -> tuple[PersonInfo, FormData] | Response:
        """The signed-in person and the form they sent with its token, or the answer that
        refuses them: sign-in first, or the page of a form that expired."""
        person = await self.auth.person(request)
        if person is None:
            return to_login(request)
        form = await request.form()
        if not self.auth.csrf_ok(request, form_value(form.get("csrf"))):
            return form_expired(request)
        return person, form

    async def build(self, request: Request) -> Response:
        """Where the build page was: the home page's guide to building an app."""
        return RedirectResponse("/?new#new-app", status_code=303)

    async def assistants(self, request: Request) -> Response:
        """The settings' tab that connects an assistant: the person's own address, and how to
        add it to each assistant."""
        person = await self.auth.person(request)
        if person is None:
            return to_login(request)
        context = {"here": "/assistants", "demo": self.demo}
        return await self.site.console(request, person, "assistants.html", context)

    async def tokens(self, request: Request) -> Response:
        """The person's API tokens, and the form that makes one: filled in with the name, the
        roles and the lifetime the address gives, as `filled_tokens_link` writes it."""
        person = await self.auth.person(request)
        if person is None:
            return to_login(request)
        query = request.query_params
        notice = _TOKEN_NOTICES.get(query.get("notice") or "", "")
        return await self._tokens_page(
            request,
            person,
            notice=notice,
            only=query.get("app") or None,
            name=query.get("name", "")[: api_tokens.MAX_NAME],
            chosen=frozenset(query.getlist("limit")),
            days=query.get("days", ""),
        )

    async def make_token(self, request: Request) -> Response:
        """Make a named token, and show it this once."""
        posted = await self._posted(request)
        if isinstance(posted, Response):
            return posted
        person, form = posted
        only = form_value(form.get("only")) or None
        places = await person_tokens.places(self.apps, person, only)
        # The person chooses where it works: never everywhere by default.
        wanted = [form_value(one) for one in form.getlist("limit")]
        # From an app's page, a role of that app: never everywhere.
        chosen = _CHOOSE if only and _EVERYWHERE in wanted else _chosen_limits(places, wanted)
        days = form_value(form.get("days")) or str(api_tokens.DEFAULT_DAYS)
        name = form_value(form.get("name"))
        # A refused form comes back as the person filled it.
        again = partial(
            self._tokens_page,
            request,
            person,
            status=400,
            only=only,
            places=places,
            name=name[: api_tokens.MAX_NAME],
            chosen=frozenset(wanted),
            days=days,
        )
        if isinstance(chosen, str) or days not in _LIFETIMES:
            return await again(
                notice=chosen if isinstance(chosen, str) else "Choose when the token expires."
            )
        try:
            info, token = await person_tokens.create_token(
                self.apps, person, name, chosen, days=None if days == _NEVER else int(days)
            )
        except ValueError as error:
            return await again(notice=str(error))
        made = {
            "name": info.name,
            "token": token,
            "prefix": api_tokens.PREFIX,
            "where": person_tokens.where(self.settings.base_url, info.limits),
            "expires": readable_time(info.expires_at.isoformat()) if info.expires_at else "",
        }
        return await self._tokens_page(request, person, made=made, only=only, places=places)

    async def revoke_token(self, request: Request) -> Response:
        """Revoke one of the person's tokens."""
        posted = await self._posted(request)
        if isinstance(posted, Response):
            return posted
        person, _form = posted
        revoked = await anyio.to_thread.run_sync(
            self.apps.database.revoke_token, person.id, request.path_params["token_id"]
        )
        notice = "revoked" if revoked is not None else "missing"
        return RedirectResponse(f"/tokens?notice={notice}", status_code=303)

    async def _tokens_page(
        self,
        request: Request,
        person: PersonInfo,
        *,
        notice: str = "",
        made: dict[str, Any] | None = None,
        status: int = 200,
        only: str | None = None,
        places: list[person_tokens.Place] | None = None,
        name: str = "",
        chosen: frozenset[str] = frozenset(),
        days: str = "",
    ) -> Response:
        listed = await anyio.to_thread.run_sync(self.apps.database.tokens, person.id)
        now = self.apps.now()
        days = days if days in _LIFETIMES else str(api_tokens.DEFAULT_DAYS)
        # How many tokens each token made: a name is the person's one token of that name.
        makes = Counter(one.made_by for one in listed)
        if places is None:
            places = await person_tokens.places(self.apps, person, only)
        # From an app's page: the roles of that app only, and the person picks one.
        app_places = [place for place in places if place.app == only] if only else []
        if only and not app_places and not notice:
            notice = "You can't make a token for that app: you don't use it, or it is starting."
        context = {
            "here": "/tokens",
            "notice": notice[:300],
            "made": made,
            "max_name": api_tokens.MAX_NAME,
            "name": name,
            "only": only if app_places else None,
            "places": [_place_choices(place, chosen) for place in app_places or places],
            "everywhere": _EVERYWHERE,
            "lifetimes": [
                {"value": value, "label": label, "selected": value == days}
                for value, label in _LIFETIMES.items()
            ],
            "tokens": [
                {
                    "id": one.id,
                    "name": one.name,
                    "public_id": one.public_id,
                    "made": readable_time(one.created_at.isoformat()),
                    "used": readable_time(one.last_used_at.isoformat()) if one.last_used_at else "",
                    **_expiry(one, now),
                    "where": person_tokens.where(self.settings.base_url, one.limits),
                    "made_by": one.made_by,
                    "confirm": _revoke_question(one.name, makes[one.name]),
                }
                for one in listed
            ],
        }
        response = await self.site.console(request, person, "tokens.html", context)
        response.status_code = status
        # The page may hold a token: no cache keeps it.
        response.headers["Cache-Control"] = "no-store"
        return response

    async def overview(self, request: Request) -> Response:
        """The app's overview: its tools, rules, members, files and latest change."""
        found = await self.site.owned(request)
        if isinstance(found, Response):
            return found
        person, live = found
        # The database, git and the data files: read in a thread, off the event loop.
        context = await anyio.to_thread.run_sync(self._overview, live, person)
        return await self.site.console(request, person, "manage_overview.html", context)

    def _overview(self, live: Live, person: PersonInfo) -> dict[str, Any]:
        """The overview page's context: what happened lately first, then what the app is."""
        manifest = live.runtime.manifest
        roles = list(manifest["roles"])
        login, name = live.info.login, live.info.name
        database = self.apps.database
        members = database.members(login, name)
        invited = len(database.pending_invitations(login, name)) + len(
            database.waiting_shares(login, name)
        )
        now = self.apps.now()
        latest = database.errors(login, name, now, limit=1)
        counts = [
            (kind["title"], len(kind["tools"]))
            for kind in _kinds(tool_rows(manifest, None), roles, None)
        ]
        counts.append(("Rules", len(rule_texts(manifest))))
        context = manage_context(live, "overview")
        context.update(
            {
                "recent": latest_changes(live, person, _RECENT),
                "errors": {
                    "count": database.error_count(login, name, now) if latest else 0,
                    "latest": _error_row(latest[0]) if latest else None,
                    "days": ERRORS_KEPT_FOR.days,
                },
                "counts": counts,
                "people": _people(len(members), invited),
                **_files(self.apps, live),
                "roles": [_role_summary(live, role, members) for role in roles or ["-"]],
            }
        )
        context.update(addresses(self.settings, live))
        return context

    async def delete_page(self, request: Request) -> Response:
        """The page that deletes one of the owner's apps, published or a draft, once they type
        its name: it needs no start, so an app that can't start is deleted too."""
        found = await self._deletable(request)
        if isinstance(found, Response):
            return found
        person, info = found
        return await self._delete_page(request, person, info)

    async def delete_app(self, request: Request) -> Response:
        """Delete one of the owner's apps, with its data, history, draft and shares, as the
        builder's `delete_app` does, once they typed its name exactly. Then go home."""
        found = await self._deletable(request)
        if isinstance(found, Response):
            return found
        person, info = found
        form = await request.form()
        if not self.auth.csrf_ok(request, form_value(form.get("csrf"))):
            return form_expired(request)
        login, name = info.login, info.name
        typed = form_value(form.get("name"))
        if typed != name:
            refusal = f"Nothing was deleted: type the app's name, {name}, exactly as it is."
            return await self._delete_page(request, person, info, refusal, 400)
        assert self.builder_core is not None
        try:
            await anyio.to_thread.run_sync(self.builder_core.delete_app, login, name, typed)
        except BuilderError as error:
            return await self._delete_page(
                request, person, info, f"Nothing was deleted: {error}", 409
            )
        except AppStarting, SandboxError:
            # The sandbox comes first, so one still starting, or out of reach, deletes nothing.
            refusal = "Nothing was deleted: your sandbox can't be reached now. Try again soon."
            return await self._delete_page(request, person, info, refusal, 503)
        return RedirectResponse("/?notice=deleted", status_code=303)

    async def _deletable(self, request: Request) -> tuple[PersonInfo, AppInfo] | Response:
        """The signed-in owner and their app, published or a draft, or the page that refuses
        them: only an owner who may build deletes an app here, as only they manage it."""
        login, name = request.path_params["login"], request.path_params["app"]
        person = await self.auth.person(request)
        if person is None:
            return to_login(request)
        if self.builder_core is None or person.login != login or not may(person, Right.BUILD):
            return no_such_app(request, login, name)
        info = await anyio.to_thread.run_sync(self.apps.database.app, login, name)
        if info is None:
            return no_such_app(request, login, name)
        return person, info

    async def _delete_page(
        self,
        request: Request,
        person: PersonInfo,
        info: AppInfo,
        refusal: str = "",
        status: int = 200,
    ) -> Response:
        context = {**manage_header(info, ""), "refusal": refusal}
        response = await self.site.console(request, person, "delete_app.html", context)
        response.status_code = status
        return response

    async def history(self, request: Request) -> Response:
        """The app's latest changes."""
        found = await self.site.owned(request)
        if isinstance(found, Response):
            return found
        person, live = found
        notice = _HISTORY_NOTICES.get(request.query_params.get("notice", ""), "")
        return await self._history_page(request, person, live, notice)

    async def _history_page(
        self, request: Request, person: PersonInfo, live: Live, notice: str, failed: bool = False
    ) -> Response:
        """The History tab, with what the last restore said, if anything."""
        context = await anyio.to_thread.run_sync(_history, self.apps, live, person)
        context.update(
            {"can_restore": self.builder_core is not None, "notice": notice, "failed": failed}
        )
        response = await self.site.console(request, person, "manage_history.html", context)
        if failed:
            response.status_code = 409
        return response

    async def restore(self, request: Request) -> Response:
        """Restore an earlier version and publish it, with every check publish runs: for the
        owner only."""
        found = await self.site.owned(request)
        if isinstance(found, Response):
            return found
        person, live = found
        form = await request.form()
        if not self.auth.csrf_ok(request, form_value(form.get("csrf"))):
            return form_expired(request)
        login, name = live.info.login, live.info.name
        if self.builder_core is None:
            return no_such_app(request, login, name)
        # A restore publishes: it builds and tests in the sandbox, as a builder's tool does.
        over = self.apps.over_budget(login)
        if over is not None:
            message = f"Not restored: {over}"
            return await self._history_page(request, person, live, message, failed=True)
        repo = self.apps.repo(login, name)
        before = await anyio.to_thread.run_sync(repo.published)
        done = RedirectResponse(f"/manage/{login}/{name}/history?notice=restored", 303)
        try:
            answer = await anyio.to_thread.run_sync(
                self.builder_core.restore_and_publish, login, name, request.path_params["commit"]
            )
        except (BuilderError, AppStarting, SandboxError, UnknownApp) as error:
            # A version that went live, then failed to start again, is restored all the same.
            if await anyio.to_thread.run_sync(repo.published) != before:
                return done
            if isinstance(error, BuilderError):
                message = f"Not restored: {error}"
            else:
                message = "Not restored: the app or its sandbox can't be reached now. Try again."
        else:
            if answer["published"]:
                return done
            message = "Not restored: " + " ".join(
                f"{check['check']}: no. {check['detail']}"
                for check in answer["checks"]
                if not check["ok"]
            )
        # The version that is live, which the restore didn't replace.
        return await self._history_page(request, person, live, message, failed=True)

    async def change(self, request: Request) -> Response:
        """One change of the app's history, with its diff: for the owner only."""
        found = await self.site.owned(request)
        if isinstance(found, Response):
            return found
        person, live = found
        return await self._change_page(request, person, live, "")

    async def _change_page(
        self, request: Request, person: PersonInfo, live: Live, notice: str, failed: bool = False
    ) -> Response:
        """The page of one change, with what the last undo said, if anything."""
        # Git is read in a thread, off the event loop.
        shown = await anyio.to_thread.run_sync(change_page, live, request.path_params["commit"])
        if shown is None:
            app = f"{live.info.login}/{live.info.name}"
            return problem(request, 404, "No such change", f"There is no such change in {app}.")
        context = manage_context(live, "history")
        context.update(shown)
        context.update({"notice": notice[:300], "failed": failed})
        response = await self.site.console(request, person, "manage_change.html", context)
        if failed:
            response.status_code = 409
        return response

    async def undo(self, request: Request) -> Response:
        """Undo one change, as `app_revert` does over MCP: through the app's rules, kept as a
        new change. For the owner only, and never a publish, which a restore undoes."""
        found = await self.site.owned(request)
        if isinstance(found, Response):
            return found
        person, live = found
        form = await request.form()
        if not self.auth.csrf_ok(request, form_value(form.get("csrf"))):
            return form_expired(request)
        login, name = live.info.login, live.info.name
        commit = request.path_params["commit"]
        entry = await anyio.to_thread.run_sync(change_entry, live.runtime.store, commit)
        if entry is None:
            why = f"There is no such change in {login}/{name}."
            return problem(request, 404, "No such change", why)
        if is_publish(entry):
            message = "A publish isn't undone here: restore an earlier version from History."
            return await self._change_page(request, person, live, message, failed=True)
        role = _undoing_role(live.runtime.manifest, entry.action or "")
        try:
            server = await self.registry.app_server(login, name, role, person.login)
        except AppStarting, SandboxError, UnknownApp:
            message = "Not undone: the app can't be reached now. Try again."
            return await self._change_page(request, person, live, message, failed=True)
        # Counted as a call of the app's screens, as the owner's other calls from a page are.
        key, who = app_key(login, name), person.login
        with self.site.screen_call(key, f"{login}/{name}", who, login) as over:
            if over is not None:
                message = f"Not undone: {refusal_text(over)}"
                return await self._change_page(request, person, live, message, failed=True)
            answer = await anyio.to_thread.run_sync(
                server.call, REVERT, {"commit": commit}, limiter=server.limiter
            )
        if answer.is_error:
            said = " ".join(part.text for part in answer.content if part.type == "text")
            return await self._change_page(request, person, live, said, failed=True)
        made = (answer.structured_content or {}).get("commit")
        notice = "undone" if made else "already-undone"
        return RedirectResponse(f"/manage/{login}/{name}/history?notice={notice}", 303)

    async def views_tab(self, request: Request) -> Response:
        """The app's screens, as one role, at one width."""
        found = await self.site.owned(request)
        if isinstance(found, Response):
            return found
        person, live = found
        manifest = live.runtime.manifest
        roles = list(manifest["roles"]) or ["-"]
        role = request.query_params.get("role") or roles[0]
        try:
            app_role(manifest, role_of(role))
        except NoSuchRole:
            return problem(request, 404, "No such role", f"There is no role {role} here.")
        views = [
            tool["name"] for tool in tool_rows(manifest, role_of(role)) if tool["kind"] == "view"
        ]
        view = request.query_params.get("view") or (views[0] if views else None)
        if views and view not in views:
            return problem(request, 404, "No such screen", f"{role} has no screen {view}.")
        width = request.query_params.get("width") or "390"
        if width not in _WIDTHS:
            width = "390"
        context = manage_context(live, "views")
        labels = view_labels(manifest)
        context.update(
            {
                "roles": roles,
                "role": role,
                "views": [{"name": one, "label": labels[one]} for one in views],
                "view": view,
                "widths": _WIDTHS,
                "width": width,
            }
        )
        return await self.site.console(request, person, "manage_views.html", context)

    async def old_views_tab(self, request: Request) -> Response:
        """The Views tab was the Preview tab: send its old address on."""
        target = f"/manage/{request.path_params['login']}/{request.path_params['app']}/views"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(target, status_code=308)

    async def sharing(self, request: Request) -> Response:
        """Who the app is shared with, and the forms that change it."""
        found = await self.site.owned(request)
        if isinstance(found, Response):
            return found
        person, live = found
        context = manage_context(live, "sharing")
        context["sharing"] = await anyio.to_thread.run_sync(
            _sharing_context, self.apps.database, live, request, self.auth
        )
        return await self.site.console(request, person, "manage_sharing.html", context)

    async def tools(self, request: Request) -> Response:
        """The app's actions, queries, screens and rules, for every role or for one."""
        found = await self.site.owned(request)
        if isinstance(found, Response):
            return found
        person, live = found
        manifest = live.runtime.manifest
        roles = list(manifest["roles"])
        role = request.query_params.get("role") or None
        if role is not None and role not in roles:
            return problem(request, 404, "No such role", f"There is no role {role} here.")
        context = manage_context(live, "tools")
        context.update(
            {
                "roles": roles,
                "role": role,
                "kinds": _kinds(tool_rows(manifest, role), roles, role),
                "rules": rule_texts(manifest),
            }
        )
        return await self.site.console(request, person, "manage_tools.html", context)

    async def errors(self, request: Request) -> Response:
        """The calls the app's code failed and its failed starts, kept for its owner: the
        recent ones, newest first. From the database only: the app may not start."""
        found = await self.site.owner_of(request)
        if isinstance(found, Response):
            return found
        person, info = found
        kept = await anyio.to_thread.run_sync(
            self.apps.database.errors, info.login, info.name, self.apps.now()
        )
        context = manage_context(info, "errors")
        context.update(
            {
                "errors": [_error_row(one) for one in kept],
                "kept_days": ERRORS_KEPT_FOR.days,
                "kept_most": ERRORS_KEPT,
            }
        )
        return await self.site.console(request, person, "manage_errors.html", context)

    async def error(self, request: Request) -> Response:
        """One call the app's code failed, or one failed start, found by the error's id, with
        its whole text. From the database only: the app may not start."""
        found = await self.site.owner_of(request)
        if isinstance(found, Response):
            return found
        person, info = found
        kept = await anyio.to_thread.run_sync(
            self.apps.database.error,
            info.login,
            info.name,
            request.path_params["error"],
            self.apps.now(),
        )
        if kept is None:
            return problem(
                request,
                404,
                "No such error",
                f"This app has no error with this id. An error is kept {ERRORS_KEPT_FOR.days} "
                "days: open the app's Errors tab to see the ones it has.",
            )
        context = manage_context(info, "errors")
        context["error"] = {**_error_row(kept), "text": kept.text}
        return await self.site.console(request, person, "manage_error.html", context)

    async def share(self, request: Request) -> Response:
        """Share the app with an email address, as one role."""

        async def invite(live: Live, form: Any) -> Said:
            role = form_value(form.get("role"))
            email = form_value(form.get("email")).strip()
            try:
                return await sharing.share(self.apps, live.info, email, role)
            except (ValueError, Refusal) as error:
                return None, str(error)

        return await self._change_sharing(request, invite)

    async def remove_member(self, request: Request) -> Response:
        """Take one role of the app back from a member: their row's, the others stay."""

        async def remove(live: Live, form: Any) -> Said:
            role = request.path_params["role"]
            try:
                person_id = int(request.path_params["person_id"])
                await anyio.to_thread.run_sync(
                    self.apps.database.remove_member,
                    live.info.login,
                    live.info.name,
                    person_id,
                    role,
                )
            except ValueError as error:
                return None, str(error)
            return _said("member-removed")

        return await self._change_sharing(request, remove)

    async def remove_share(self, request: Request) -> Response:
        """Cancel a share not yet accepted, named by its email, as `sharing.waiting` lists it:
        whether it waits for a person or for the email's first sign-in."""

        async def remove(live: Live, form: Any) -> Said:
            email, role = request.path_params["email"], request.path_params["role"]
            try:
                await anyio.to_thread.run_sync(
                    sharing.revoke, self.apps.database, live.info, email, role
                )
            except ValueError as error:
                return None, str(error)
            return _said("share-removed")

        return await self._change_sharing(request, remove)

    async def _change_sharing(self, request: Request, change: Any) -> Response:
        """Run one change to the app's members, then show the sharing part again.

        htmx gets the sharing part with what the change said. Without htmx, a change that was
        made sends the page back with a notice code, as the address carries nothing else, and
        one that was refused shows the page again with the reason.
        """
        found = await self.site.owned(request)
        if isinstance(found, Response):
            return found
        person, live = found
        form = await request.form()
        partial = request.headers.get("HX-Request") == "true"
        if not self.auth.csrf_ok(request, form_value(form.get("csrf"))):
            if not partial:
                return form_expired(request)
            code, message = None, "The form expired. Reload the page and try again."
        else:
            code, message = await change(live, form)
        if not partial and code is not None:
            done = f"/manage/{live.info.login}/{live.info.name}/sharing?notice={code}"
            return RedirectResponse(done, status_code=303)
        context = manage_context(live, "sharing")
        context["sharing"] = await anyio.to_thread.run_sync(
            _sharing_context, self.apps.database, live, request, self.auth, message
        )
        context["sharing"]["refused"] = code is None
        if partial:
            return render(request, "sharing.html", context)
        page = await self.site.console(request, person, "manage_sharing.html", context)
        page.status_code = 400
        return page


# What a page that its own redirect sends a notice to says: a fixed sentence for each code,
# so an address can't put words of its own on a page of Cliquet's.
_HOME_NOTICES = {
    "accepted": "You accepted the share. The app is under Shared with you now.",
    "declined": "You declined the share. It gives you nothing.",
    "deleted": "The app is deleted, with its data and its history.",
}
_HISTORY_NOTICES = {
    "restored": "The version you chose is live again.",
    "undone": "Undone. The undo is the newest change, and can be undone in turn.",
    "already-undone": "Nothing changed: what that change did is undone already.",
}
# The place of a token that works everywhere, in the tokens form.
_EVERYWHERE = "everywhere"
# The lifetimes the Tokens form offers, by the value it sends: one that never expires last.
_NEVER = "never"
_LIFETIMES = {str(days): f"{days} days" for days in api_tokens.LIFETIMES} | {_NEVER: "Never"}
# How soon a token's expiry is marked on the Tokens page: a 7-day token from the start.
_SOON = timedelta(days=7)
_CHOOSE = "Choose where the token works: everywhere, or a role in each app it is for."
_TOKEN_NOTICES = {
    "revoked": "Token revoked. It no longer works.",
    "missing": "There is no such token.",
}
# What a change to the app's sharing said: a notice code and the sentence htmx shows, or no
# code and why it was refused.
type Said = tuple[str | None, str]
# What the Sharing tab says after a change made without htmx: a fixed sentence for each code.
_SHARING_NOTICES = {
    # The same words whether or not the email has an account: see `sharing`.
    "shared": f"Shared. {sharing.WAITS}",
    "owner": "That is the owner's email: the owner holds every role.",
    "member-removed": "Member removed.",
    "share-removed": "Share removed.",
}


def _nothing_waits(request: Request, login: str, name: str) -> Response:
    """The page for a share that doesn't wait for the person."""
    return problem(
        request, 404, "Nothing to accept", f"Nothing of {login}/{name} waits for you here."
    )


def _history(apps: Apps, live: Live, person: PersonInfo) -> dict[str, Any]:
    """The history page's context: the latest changes and publishes, read from git."""
    context = manage_context(live, "history")
    context["entries"] = latest_changes(live, person, 50)
    context["versions"] = versions(apps.repo(live.info.login, live.info.name), 20)
    return context


def _undoing_role(manifest: dict[str, Any], action: str) -> str:
    """The role the owner undoes a change of `action` as: the first of the app's roles that
    may call it, or that may call an action at all for an undo's own change. The owner holds
    every role. `-` for an app without roles."""
    roles = list(manifest["roles"])
    may = [access(manifest, role).actions for role in roles]
    found = next(
        (role for role, actions in zip(roles, may, strict=True) if action in actions), None
    )
    found = found or next((role for role, actions in zip(roles, may, strict=True) if actions), None)
    return found or (roles[0] if roles else "-")


def _uses(live: Live, role: str) -> str:
    """What a role may use, counted by kind: "3 actions, 4 queries, 3 screens"."""
    manifest = live.runtime.manifest
    tools = tool_rows(manifest, role_of(role))
    uses = [
        f"{len(kind['tools'])} {kind['title'].lower()}"
        for kind in _kinds(tools, list(manifest["roles"]), None)
        if kind["tools"]
    ]
    return ", ".join(uses) or "no tool"


def _role_summary(live: Live, role: str, members: list[Any]) -> dict[str, Any]:
    """One role on the overview: what it may use, and who holds it."""
    held = sum(1 for member in members if member.role == role)
    return {
        "role": role,
        "uses": _uses(live, role),
        "people": "you" + (f" and {held} member{'s' if held != 1 else ''}" if held else ""),
    }


def _people(members: int, invited: int) -> str:
    """Who holds the app, as the overview says it: "You, 2 members and 1 invited"."""
    said = ["You"]
    if members:
        said.append(f"{members} member{'s' if members != 1 else ''}")
    if invited:
        said.append(f"{invited} invited")
    return listed(said)


# The most data files the overview lists. A pattern may match any number of them.
_SHOWN_FILES = 100


def _files(apps: Apps, live: Live) -> dict[str, Any]:
    """The app's data files, with their size as the last change left them, in the order the
    app declares them: each file a pattern matches, or one row for a pattern that matches
    none yet. At most `_SHOWN_FILES`, as `files`, and how many more there are, as `more_files`."""
    repo = apps.repo(live.info.login, live.info.name)
    folder = repo.folder / "data"
    declared = live.runtime.manifest["files"]
    names = repo.data_names() if any(is_pattern(entry) for entry in declared) else []
    # Each row's name, and whether it stands for a pattern that matches no file yet.
    listed: dict[str, bool] = {}
    for entry in declared:
        found = [name for name in names if matches(entry, name)] if is_pattern(entry) else [entry]
        listed.update({name: False for name in found if name not in listed})
        if not found:
            listed[entry] = True
    files = []
    # Only the rows shown are read from disk: a pattern may match thousands of files.
    for name, empty in list(listed.items())[:_SHOWN_FILES]:
        path = folder / name
        created = not empty and path.is_file()
        if created:
            shown = readable_size(path.stat().st_size)
        else:
            shown = "none yet" if empty else "not created yet"
        files.append({"name": name, "size": shown, "created": created})
    return {"files": files, "more_files": max(0, len(listed) - _SHOWN_FILES)}


# The four kinds of things an app is made of, in the order the console shows them.
_KINDS = [
    ("action", "Actions", "Change the data. Each change is checked against the rules, then kept."),
    ("query", "Queries", "Answer questions. They read the data and change nothing."),
    ("view", "Screens", "What people see, on the app's pages and in a conversation."),
]


def _kinds(tools: list[dict[str, Any]], roles: list[str], role: str | None) -> list[dict[str, Any]]:
    """The app's tools grouped by kind. A screen links to its preview, as the chosen role or
    the first role that sees it."""
    kinds = []
    for kind, title, sentence in _KINDS:
        rows = [tool for tool in tools if tool["kind"] == kind]
        for row in rows:
            allowed = row["roles"] or roles or ["-"]
            first = next((one for one in roles or ["-"] if one in allowed), None)
            row["preview_role"] = role or first
        kinds.append({"kind": kind, "title": title, "sentence": sentence, "tools": rows})
    return kinds


def _sharing_context(
    database: Database, live: Live, request: Request, auth: WebAuth, notice: str | None = None
) -> dict[str, Any]:
    roles = list(live.runtime.manifest["roles"]) or ["-"]
    login, name = live.info.login, live.info.name
    base = f"/manage/{login}/{name}"
    # Each person who holds the app, or whom it waits for, and the form that removes them.
    people = [
        _person(m.person.login, m.person.email, m.role, "member", f"members/{m.person.id}/{m.role}")
        for m in database.members(login, name)
    ]
    # A share not yet accepted shows only its email, whether or not that email has an account.
    people += [
        _person(w.who, None, w.role, "waiting", f"shares/{quote(w.who, safe='')}/{w.role}")
        for w in sharing.waiting(database, login, name)
    ]
    for person in people:
        person["remove"] = f"{base}/{person['remove']}/remove"
    if notice is None:
        notice = _SHARING_NOTICES.get(request.query_params.get("notice") or "", "")
    return {
        "csrf": auth.csrf_token(request) or "",
        "roles": [
            {"value": role, "label": role_label(role), "uses": _uses(live, role)} for role in roles
        ],
        "people": people,
        "notice": notice[:300],
    }


def _person(who: str, email: str | None, role: str, status: str, remove: str) -> dict[str, Any]:
    """One row of the Sharing tab's people: who, where they stand, and their remove form's
    path under the app's page."""
    return {
        "who": who,
        "email": email or "",
        "role": role_label(role),
        "status": status,
        "remove": remove,
    }


def _said(code: str) -> Said:
    """A change that was made: its notice code and fixed sentence."""
    return code, _SHARING_NOTICES[code]


def _error_row(error: ErrorInfo) -> dict[str, Any]:
    """One error of the app's code, as the console shows it, without its traceback."""
    return {**summary(error), "when": readable_time(error.at.isoformat())}


def _revoke_question(name: str, made: int) -> str:
    """What revoking a token asks first: with the tokens it made, which go with it."""
    if not made:
        return f"Revoke {name}? Anything that uses it stops working at once."
    tokens = "the token" if made == 1 else f"the {made} tokens"
    return f"Revoke {name} and {tokens} it made? Anything that uses them stops working at once."


def _place_choices(place: person_tokens.Place, chosen: frozenset[str]) -> dict[str, Any]:
    """A place in the tokens form: its app, and a choice for each of the person's roles, the
    one in `chosen` selected."""
    roles = [
        {
            "value": limit_value(limit),
            "label": "Without a role" if limit.role == "-" else f"As {limit.role}",
            "selected": limit_value(limit) in chosen,
        }
        for limit in place.limits
    ]
    return {"app": place.app, "roles": roles, "chosen": any(one["selected"] for one in roles)}


def _expiry(token: TokenInfo, now: datetime) -> dict[str, Any]:
    """When a token expires, as the Tokens page shows it: empty for one that never does, and
    whether it expired or soon will."""
    at = token.expires_at
    return {
        "expires": readable_time(at.isoformat()) if at else "",
        "expired": token.expired(now),
        "soon": at is not None and now < at <= now + _SOON,
    }


def _chosen_limits(
    places: list[person_tokens.Place], wanted: list[str]
) -> tuple[TokenLimit, ...] | str | None:
    """The limits a person chose in the tokens form, None for everywhere, or why the choice
    is refused. Each app's own field sends one of its roles, or nothing. The database refuses
    two roles in one app."""
    wanted = [value for value in wanted if value]
    roles = {limit_value(one): one for place in places for one in place.limits}
    if wanted == [_EVERYWHERE]:
        return None
    if _EVERYWHERE in wanted:
        return "A token works everywhere, or only for the apps you choose: not both."
    if not wanted or any(value not in roles for value in wanted):
        return _CHOOSE
    return tuple(roles[value] for value in wanted)
