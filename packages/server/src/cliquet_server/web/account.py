"""The account page: what the signed-in person owns and holds, the form that deletes their
account, with everything it owns, and the one that asks for a new verification email."""

import logging
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from cliquet_server.accounts import Accounts
from cliquet_server.db import Database, PersonInfo, Right, may
from cliquet_server.endpoints import role_label
from cliquet_server.pages import no_page, problem, render
from cliquet_server.sandbox_time import next_month, reset_words
from cliquet_server.web_sign_in import safe_next

from .common import (
    Site,
    app_identity,
    day,
    form_expired,
    form_value,
    roles_by_app,
    to_login,
)

_log = logging.getLogger(__name__)


class AccountPage:
    """The account page and its forms, for the signed-in person only: no login is in the
    address, so nobody's account but their own can be named."""

    def __init__(self, site: Site, accounts: Accounts) -> None:
        self.site = site
        self.accounts = accounts
        self.database = site.apps.database
        self.auth = site.auth

    async def show(self, request: Request) -> Response:
        """What the person owns, uses and made, what deleting the account removes and what
        stays, and the form that deletes it. It reads only the database, and starts no app.

        A person whose time to verify their email is over sees it too, so they still download
        their apps, or delete their account."""
        person = await self.auth.person(request, even_locked_out=True)
        if person is None:
            return to_login(request)
        return await self._page(request, person)

    async def send_email(self, request: Request) -> Response:
        """Ask the sign-in provider to send the signed-in person their verification email
        again, then go back where they were, which says what came of it. One whose time to
        verify their email is over asks too: they need it most."""
        person = await self.auth.person(request, even_locked_out=True)
        if person is None:
            return to_login(request)
        form = await request.form()
        if not self.auth.csrf_ok(request, form_value(form.get("csrf"))):
            return form_expired(request)
        emails = self.auth.emails
        if emails is None:
            return no_page(request)
        told = await emails.send(person)
        self.site.apps.notices.tell(person.id, told)
        # A page they may no longer see sends them to their account page, which they may.
        back = "/account" if self.auth.locked_out(person) else safe_next(form.get("next"))
        return RedirectResponse(back, status_code=303)

    async def delete(self, request: Request) -> Response:
        """Delete the signed-in person's account, once they typed their login exactly, and sign
        them out: one whose time to verify their email is over too."""
        person = await self.auth.person(request, even_locked_out=True)
        if person is None:
            return to_login(request)
        form = await request.form()
        if not self.auth.csrf_ok(request, form_value(form.get("csrf"))):
            return form_expired(request)
        if form_value(form.get("login")) != person.login:
            refusal = (
                f"Nothing was deleted: type your login, {person.login}, exactly as it is, to "
                "delete your account."
            )
            return await self._page(request, person, refusal, 400)
        login = person.login
        unfinished = False
        try:
            await anyio.to_thread.run_sync(self.accounts.delete, login)
        except Exception:
            _log.exception("unexpected: the account %s wasn't deleted", login)
            account = await anyio.to_thread.run_sync(self.database.account, login)
            if account is None or account.deleted_at is None:
                sentence = "Your account couldn't be deleted, and nothing was. Try again later."
                return problem(request, 500, "Not deleted", sentence)
            # Closed from its first step: the server finishes the rest as it next starts.
            unfinished = True
        response = render(request, "account_deleted.html", {"unfinished": unfinished})
        self.auth.clear(response)
        return response

    async def _page(
        self, request: Request, person: PersonInfo, refusal: str = "", status: int = 200
    ) -> Response:
        context = await anyio.to_thread.run_sync(_account, self.database, person)
        # Its button sends a new email, so it doesn't send them here again.
        locked_out = self.auth.locked_out(person, lost_email=False)
        context.update({"here": "/account", "refusal": refusal, "locked_out": locked_out})
        context["sandbox_time"] = self._sandbox_time(person, context)
        response = await self.site.console(request, person, "account.html", context)
        response.status_code = status
        return response

    def _sandbox_time(self, person: PersonInfo, context: dict[str, Any]) -> dict[str, Any] | None:
        """The sandbox time the person's apps used this month, in whole minutes, of their own
        budget, whether it is Plus's, and when it resets, from memory: None without a budget,
        and for a person who owns no app and may not build."""
        apps = self.site.apps
        meter = apps.meter
        if meter is None or (budget := meter.budget(person.login)) is None:
            return None
        if not (context["apps"] or context["drafts"] or may(person, Right.BUILD)):
            return None
        return {
            "used": int(meter.used(person.login) // 60),
            "budget": budget,
            "plus": meter.is_plus(person.login),
            "resets": reset_words(next_month(apps.now())),
        }


def _account(database: Database, person: PersonInfo) -> dict[str, Any]:
    """What the account page lists, from the database alone: the apps the person owns, each
    published one with its download, their drafts never published, the apps they use with
    their roles, the shares waiting for them and their tokens."""
    owned = database.apps(person.login)
    used = roles_by_app(
        (held.app.login, held.app.name, role_label(held.role))
        for held in database.memberships(person.id)
    )
    waiting = roles_by_app(
        (share.login, share.name, role_label(share.role))
        for share in database.pending_shares(person.id)
    )
    return {
        "apps": [
            {
                **app_identity(info),
                "published": day(info.published_at),
                "download": f"/manage/{info.login}/{info.name}/download",
            }
            for info in owned
            if info.published_at is not None
        ],
        "drafts": [app_identity(info) for info in owned if info.published_at is None],
        "used": [
            {"app": f"{login}/{name}", "roles": roles} for (login, name), roles in used.items()
        ],
        "waiting": [
            {"app": f"{login}/{name}", "roles": roles} for (login, name), roles in waiting.items()
        ],
        "tokens": [token.name for token in database.tokens(person.id)],
    }
