"""A newcomer on the person's endpoint: an identity the sign-in provider vouches for, that has
no person yet, such as someone who connects their assistant before they ever sign in on the
web. They choose their login with `CHOOSE_LOGIN`, which makes the person as the web's form
does. Until then they see it beside every tool they will have, each of which refuses them, so
a host that never lists the tools again still has them all once they have a login.

A newcomer whose email isn't verified becomes a person with no email. Each tool's answer then
tells them once a day, as `Reminders` adds it, how many days they have left to verify it, and
once what a sign-in found, such as that another account has their email.
"""

from collections.abc import Sequence
from typing import Annotated

import anyio
import mcp_types as mt
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool, ToolResult
from pydantic import Field

from cliquet.tools import listed
from cliquet_server.app_auth import CHOOSE_LOGIN
from cliquet_server.apps import Apps
from cliquet_server.callers import caller, token_newcomer
from cliquet_server.db import Database, EmailTaken, PendingShare, PersonInfo
from cliquet_server.endpoints import role_label
from cliquet_server.identity import MAX_LOGIN, MIN_LOGIN
from cliquet_server.settings import Settings
from cliquet_server.sign_in import EMAIL_ELSEWHERE, free_login
from cliquet_server.tooling import WRITES, answer, idempotent

_LOGIN = Annotated[str, Field(description="The login the person chose.")]


def description(settings: Settings, suggestion: str | None = None) -> str:
    """What `CHOOSE_LOGIN` tells the agent: to ask the person first, with a free `suggestion`,
    and what a login is."""
    offer = f", offering {suggestion}, which is free" if suggestion is not None else ""
    privacy = (
        f" How Cliquet keeps their email is in its privacy notice, {settings.privacy_url}."
        if settings.privacy_url
        else ""
    )
    return (
        "Choose the person's login on Cliquet: they have none yet, and every other tool waits "
        f"for it. Ask the person which login they want first{offer}. Call this only with the "
        "login they chose in this conversation. A login is permanent: it is in their "
        "apps' addresses and history, where the people they share with see it, and it is "
        f"never their email. It is {MIN_LOGIN} to {MAX_LOGIN} lower-case letters, digits and "
        "single hyphens, with no hyphen at either end, and it doesn't start with app- or name "
        f"one of Cliquet's own pages, such as me or tokens.{privacy}"
    )


class Newcomers(Middleware):
    """List `CHOOSE_LOGIN` to a newcomer only, with a free login to offer: the same one at each
    list, while it stays free."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self.database, self.settings = database, settings

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Every tool for a newcomer, `CHOOSE_LOGIN` with its suggestion, and the others only
        for anyone else."""
        listed_tools = await call_next(context)
        newcomer = token_newcomer()
        if newcomer is None:
            return [tool for tool in listed_tools if tool.name != CHOOSE_LOGIN]
        seed = f"{newcomer.issuer} {newcomer.subject}"
        suggestion = await anyio.to_thread.run_sync(free_login, self.database, newcomer.email, seed)
        offered = description(self.settings, suggestion)
        return [
            tool.model_copy(update={"description": offered}) if tool.name == CHOOSE_LOGIN else tool
            for tool in listed_tools
        ]


def add_newcomers(mcp: FastMCP, apps: Apps, settings: Settings) -> None:
    """Add `CHOOSE_LOGIN` to the person's endpoint, for newcomers only."""
    database = apps.database

    @mcp.tool(
        name=CHOOSE_LOGIN,
        title="Choose your login",
        annotations=idempotent(WRITES),
        description=description(settings),
    )
    async def choose_login(login: _LOGIN) -> mt.CallToolResult:
        newcomer = token_newcomer()
        if newcomer is None:
            raise ToolError(f"The person's login is {caller(apps).login}, and it can't change.")
        now = apps.now()

        def made() -> tuple[PersonInfo, list[PendingShare]]:
            person = database.person_for(
                newcomer.issuer,
                newcomer.subject,
                newcomer.email,
                login,
                now,
                builds=settings.grant_build_on_sign_up,
            )
            database.claim_invitations(person, now)
            return person, database.pending_shares(person.id)

        try:
            person, waiting = await anyio.to_thread.run_sync(made)
        except EmailTaken:
            # Verified meanwhile, with an email another account has.
            raise ToolError(EMAIL_ELSEWHERE) from None
        except ValueError as error:
            # A login refused, taken or kept, in the sentence that says what to choose.
            raise ToolError(str(error)) from None
        # The person's tools work now: a host that listens lists them again.
        await get_context().send_notification(mt.ToolListChangedNotification())
        shares = [
            {"app": f"{share.login}/{share.name}", "owner": share.login, "role": share.role}
            for share in waiting
        ]
        text = f"The person's login is now {person.login}."
        if waiting:
            named = listed([f"{one['app']} as {role_label(one['role'])}" for one in shares])
            text += (
                f" Shares wait for them to accept: {named}. Say so, and accept or decline one "
                "only if they ask you to."
            )
        text += (
            " Their tools work now: list your tools again, or, if your host doesn't, start a "
            "new conversation."
        )
        # Such as how many days they have to verify their email, as each tool's answer says.
        told = apps.notices.take(settings, person, now)
        data = {"login": person.login, "waiting": shares}
        return answer(" ".join([text, *map(for_the_person, told)]), data)

    mcp.add_middleware(Newcomers(database, settings))
    mcp.add_middleware(Reminders(apps))


def for_the_person(notice: str) -> str:
    """A notice, as a tool's answer passes it on to the person."""
    return f"Tell the person, from Cliquet: {notice}"


class Reminders(Middleware):
    """Add to a tool's answer what Cliquet tells the person once, as `Notices.take` says, with
    the reminder to verify their email once a day: never to an error, nor to the answer of a
    tool only a panel calls, which no model reads."""

    def __init__(self, apps: Apps) -> None:
        self.apps = apps

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Run the call, then add what to tell its person, if anything."""
        result = await call_next(context)
        if result.is_error:
            return result
        try:
            person = caller(self.apps)
        except ToolError:
            return result
        apps = self.apps
        if person.email_verified and not apps.notices.waiting(person.id):
            return result
        if await _panels_only(context):
            return result
        told = apps.notices.take(apps.settings, person, apps.now())
        if not told:
            return result
        added = [mt.TextContent(type="text", text=for_the_person(one)) for one in told]
        return ToolResult.from_mcp_result(
            mt.CallToolResult(
                content=[*result.content, *added],
                structured_content=result.structured_content,
                is_error=False,
                _meta=result.meta,
            )
        )


async def _panels_only(context: MiddlewareContext[mt.CallToolRequestParams]) -> bool:
    """Whether the tool called is one only a panel calls, as its meta says."""
    server = context.fastmcp_context.fastmcp if context.fastmcp_context else None
    tool = await server.get_tool(context.message.name) if server is not None else None
    meta = (tool.meta or {}) if tool is not None else {}
    return meta.get("ui", {}).get("visibility") == ["app"]
