"""Sharing an app: its owner shares it, once published, with an email, as one of the roles
it declares, the same on the console's Sharing tab and on the person's endpoint, where
`people` lists who holds it, and `revoke` takes it back.

`Database.share` checks the owner and the email, and leaves the role to its caller: only the
app knows its roles, as `Apps.declared_roles` reads them.

Until a share is accepted, its owner knows only the email they typed: what sharing says, and
how a share waiting for its person is found again, never tell whether that email has an
account.
"""

from dataclasses import dataclass

import anyio

from cliquet_server.apps import Apps
from cliquet_server.arguments import chosen_role
from cliquet_server.db import AppInfo, Database, Shared
from cliquet_server.endpoints import role_label
from cliquet_server.sign_in import verify_first

# What a share waits for, whether or not its email has an account, a share already waits for
# it, or its person holds the role already.
WAITS = (
    "They get it once they sign in with this email and accept it, unless they hold it already. "
    "Cliquet sends no email: tell them."
)
SHARED = "Shared with {email} as {role}. " + WAITS
OWNERS = "{email} is the owner's email: the owner holds every role."


@dataclass(frozen=True)
class Held:
    """One role of an app someone holds, or that waits for them: a member, by login, or a
    share not yet accepted, by its email, whether it waits for a person or for the email's
    first sign-in. `person_id` is the member's, or the person's a share waits for."""

    who: str
    role: str
    accepted: bool
    person_id: int | None = None


async def share(apps: Apps, info: AppInfo, email: str, role: str | None) -> tuple[Shared, str]:
    """Share the owner's published app with an email as `role`, `-` for an app without roles,
    and say what happened: in the same words whatever the email. With no role, the app's one
    role. Raises `ValueError` or `Refusal` with the sentence that says why not, such as an
    owner whose email isn't verified."""
    login, name = info.login, info.name
    owner = await anyio.to_thread.run_sync(apps.database.person_by_login, login)
    # An account nobody can trace puts no app in front of anyone.
    if owner is not None and not owner.email_verified:
        raise ValueError(verify_first(apps.settings, "share an app"))
    if info.published_at is None:
        raise ValueError(f"{login}/{name} isn't published yet: publish it before you share it.")
    # The owner holds every role the app declares.
    role = chosen_role(f"{login}/{name}", role, await apps.declared_roles(login, name))
    done = await anyio.to_thread.run_sync(apps.database.share, login, name, email, role, apps.now())
    if done == "owner":
        return "owner", OWNERS.format(email=email)
    return "shared", SHARED.format(email=email, role=role_label(role))


def people(database: Database, login: str, name: str) -> list[Held]:
    """Who holds the owner's app, its members by login, then the shares not yet accepted, as
    `waiting` lists them."""
    held = [
        Held(one.person.login, one.role, True, one.person.id)
        for one in database.members(login, name)
    ]
    return held + waiting(database, login, name)


def waiting(database: Database, login: str, name: str) -> list[Held]:
    """The shares of the owner's app not yet accepted, by email and sorted, whether they wait
    for a person or for the email's first sign-in: until then, its owner knows only the email."""
    shares = [
        Held(one.email, one.role, False, one.person.id)
        for one in database.waiting_shares(login, name)
    ]
    shares += [
        Held(one.email, one.role, False) for one in database.pending_invitations(login, name)
    ]
    return sorted(shares, key=lambda one: (one.who, one.role))


def revoke(database: Database, info: AppInfo, who: str, role: str | None) -> list[str]:
    """Take back the owner's app from `who`, as `role` alone or as every role: from a member,
    named by login, with the shares still waiting for them when it is every role, or from a
    share not yet accepted, named by its email, as `people` lists them. Give the roles taken
    back: none when nothing was there."""
    login, name = info.login, info.name
    # Logins and emails are kept in lower case.
    given = who.strip().lower()
    gone = [
        one
        for one in people(database, login, name)
        if one.who == given and role in (None, one.role)
    ]
    for one in gone:
        if one.accepted and one.person_id is not None:
            # With no role, nothing is left that they could accept to come back.
            database.remove_member(login, name, one.person_id, role)
        elif one.person_id is not None:
            database.remove_share(login, name, one.person_id, one.role)
        else:
            database.remove_invitation(login, name, one.who, one.role)
    return sorted({one.role for one in gone})
