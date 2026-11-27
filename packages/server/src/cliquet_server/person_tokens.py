"""A person's API tokens: where they may make one work, making one, and where each works, the
same on the console's Tokens page and on the person's endpoint.

`Database.create_token` checks that a member holds each role a token is limited to. An owner
holds every role their app declares, which only the app knows: `places` and `named_places`
read them, as `Apps.declared_roles` does, and a token's limits are their places' own,
chosen as `limit_for` chooses one.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import anyio

from cliquet_server import tokens as api_tokens
from cliquet_server.app_auth import app_address, limits_label, maker_expiring
from cliquet_server.apps import Apps, AppStarting, UnknownApp
from cliquet_server.arguments import Refusal, chosen_role, no_app, pick_app
from cliquet_server.db import AppInfo, MakerExpiring, PersonInfo, TokenInfo, TokenLimit
from cliquet_server.endpoints import why_not_started
from cliquet_server.executor import SandboxError
from cliquet_server.sign_in import verify_first
from cliquet_server.web.common import roles_by_app


@dataclass(frozen=True)
class Place:
    """A published app a person may make a token work for, as owner/name, with one limit for
    each role they hold in it: `-` for an app without roles."""

    app: str
    limits: tuple[TokenLimit, ...]


async def places(apps: Apps, person: PersonInfo, only: str | None = None) -> list[Place]:
    """Where the person may make a token work, as the Tokens page lists it: each published app
    they may use, or only the one `only` names as owner/name, with each role they hold in it.
    An owner's app whose roles aren't kept yet is started to read them, all together, and
    left out if it can't be started now."""
    published, held = await _usable(apps, person)
    if only is not None:
        published = [info for info in published if f"{info.login}/{info.name}" == only]
    found = await asyncio.gather(
        *(_place(apps, person, info, held) for info in published), return_exceptions=True
    )
    kept: list[Place] = []
    for place in found:
        if isinstance(place, UnknownApp | SandboxError | AppStarting):
            continue
        if isinstance(place, BaseException):
            raise place
        if place.limits:
            kept.append(place)
    return kept


async def named_places(apps: Apps, person: PersonInfo, wanted: list[str]) -> list[Place]:
    """The places `wanted` names, each as owner/name or its name alone, in its order: only
    their roles are read. Raises `Refusal` for one that isn't a published app the person may
    use or can't be started now, and `AppStarting` while one starts."""
    published, held = await _usable(apps, person)
    by_app = {(info.login, info.name): info for info in published}
    found: list[Place] = []
    for app in wanted:
        login, name = pick_app(app, by_app)
        try:
            found.append(await _place(apps, person, by_app[(login, name)], held))
        except UnknownApp:
            raise Refusal(no_app(f"{login}/{name}")) from None
        except SandboxError as error:
            why = why_not_started(error, person.login, login, name)
            raise Refusal(f"{login}/{name} can't be started: {why}") from None
    return found


async def _usable(
    apps: Apps, person: PersonInfo
) -> tuple[list[AppInfo], dict[tuple[str, str], list[str]]]:
    """The published apps the person may use, and the roles of their memberships, by app."""
    database = apps.database
    visible = await anyio.to_thread.run_sync(database.visible_apps, person.login)
    memberships = await anyio.to_thread.run_sync(database.memberships, person.id)
    held = roles_by_app((one.app.login, one.app.name, one.role) for one in memberships)
    return [info for info in visible if info.published_at is not None], held


async def _place(
    apps: Apps, person: PersonInfo, info: AppInfo, held: dict[tuple[str, str], list[str]]
) -> Place:
    """An app the person may use, with the roles of their memberships in it, or for its
    owner every role it declares: `-` for an app without roles."""
    login, name = info.login, info.name
    if login != person.login:
        roles = held.get((login, name), [])
    else:
        roles = await apps.declared_roles(login, name) or ["-"]
    return Place(f"{login}/{name}", tuple(TokenLimit(login, name, role) for role in roles))


async def create_token(
    apps: Apps,
    person: PersonInfo,
    name: str,
    limits: Sequence[TokenLimit] | None,
    made_by: str | None = None,
    days: int | None = api_tokens.DEFAULT_DAYS,
) -> tuple[TokenInfo, str]:
    """Make a named token for the person, and return it with the token itself, shown once:
    for everywhere with no `limits`, or limited to those, each one of their places' limits,
    from `places` or `limit_for`. It works for `days`, or for good with None. With `made_by`,
    the public id of the token the person calls with, it is made by that token, limited,
    goes when that one is revoked, and never outlives it, as `Database.create_token` says.
    Raises `ValueError` with the sentence that says why not, such as for a person whose email
    isn't verified."""
    _verified(apps, person)
    try:
        return await anyio.to_thread.run_sync(
            apps.database.create_token, person.id, name, apps.now(), limits, made_by, days
        )
    except MakerExpiring as expiring:
        raise ValueError(maker_expiring(apps.settings.base_url, expiring.at)) from None


async def check_token(
    apps: Apps, person: PersonInfo, name: str, limits: Sequence[TokenLimit]
) -> str:
    """Check a limited token as `create_token` would, without making it, for the person to
    make it themselves: its name, trimmed. Raises `ValueError` as `create_token` does."""
    _verified(apps, person)
    return await anyio.to_thread.run_sync(
        apps.database.check_token, person.id, name, limits, apps.now()
    )


def _verified(apps: Apps, person: PersonInfo) -> None:
    """Refuse a token to a person whose email isn't verified: an account nobody can trace
    hands out no long-lived access. Raises `ValueError` with what to do."""
    if not person.email_verified:
        raise ValueError(verify_first(apps.settings, "make an API token"))


def limit_for(place: Place, role: str | None) -> TokenLimit:
    """The limit for one of the person's places, as `role`, which they may leave out when they
    hold one role there or the app has none. Raises `Refusal` with the sentence that says why
    not."""
    held = [one.role for one in place.limits if one.role != "-"]
    chosen = chosen_role(place.app, role, held)
    return next(one for one in place.limits if one.role == chosen)


def where(base_url: str, limits: tuple[TokenLimit, ...] | None) -> dict[str, Any]:
    """Where a token works, as a person reads it, and the address of each of its apps."""
    if limits is None:
        return {"label": "Works everywhere you may go", "addresses": []}
    if not limits:
        return {"label": "Works nowhere: its apps were deleted", "addresses": []}
    return {
        "label": f"Works only for {limits_label(limits)}",
        "addresses": [
            {
                "app": f"{one.login}/{one.name}",
                "address": app_address(base_url, one.login, one.name),
            }
            for one in limits
        ],
    }
