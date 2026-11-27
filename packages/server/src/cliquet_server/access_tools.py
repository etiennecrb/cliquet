"""The tools on the person's endpoint that hand out and take back access to the person's apps,
served to the people who may build, with the builder's: share an app and revoke a share, as
the console's Sharing tab does, and create, list and revoke API tokens.

Only the person decides who gets in: `tooling.ONLY_THE_PERSON` says so in the endpoint's
instructions, and `tooling.WHEN_ASKED` in each tool that hands out or takes back access. A
token made here is always limited to apps, and its secret reaches the model only when the
caller is itself a token for everywhere, which makes it: a person who signed in makes it
themselves, in the token panel or on the API tokens page, so the secret passes through their
browser alone.
"""

from functools import partial
from typing import Annotated, Any

import anyio
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp_types import CallToolResult
from pydantic import BaseModel, ConfigDict, Field

from cliquet import views
from cliquet.mcp_server import compact
from cliquet.tools import listed
from cliquet_server import person_tokens, sharing
from cliquet_server import tokens as api_tokens
from cliquet_server.app_auth import filled_tokens_link
from cliquet_server.apps import Apps
from cliquet_server.arguments import App, Refusal, Role
from cliquet_server.builder.errors import no_such_app
from cliquet_server.builder.panel import token_panel_html, token_panel_uri
from cliquet_server.builder.server import builder_tool, owned_app
from cliquet_server.callers import api_token, caller, draws_screens
from cliquet_server.db import AppInfo, PersonInfo, TokenInfo, TokenLimit
from cliquet_server.endpoints import role_label, why_not_started
from cliquet_server.executor import SandboxError
from cliquet_server.tooling import DESTROYS, READS, WHEN_ASKED, WRITES, answer, idempotent

SHARE_APP, REVOKE_SHARE = "share_app", "revoke_share"
CREATE_TOKEN, LIST_TOKENS, REVOKE_TOKEN = "create_token", "list_tokens", "revoke_token"
# The tool only the token panel calls, for the person's click on Create.
ISSUE_TOKEN = "issue_token"

# Why `create_token` makes no token for everywhere: decision 3 of the issue, as the rules say.
NOT_EVERYWHERE = (
    "A token made here is limited to apps, one role in each: name at least one in apps. A "
    "token for everywhere is made by the person on the console's API tokens page."
)
# Why it makes none that never expires, which an agent would forget.
NOT_FOR_GOOD = (
    f"A token made here works for {api_tokens.lifetimes()}, {api_tokens.DEFAULT_DAYS} by "
    "default: choose one in days. A token that never expires is made by the person on the "
    "console's API tokens page."
)


# One app a token works for, as one of the person's roles in it. No docstring: it would be
# the schema's description, which every conversation carries.
class TokenApp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: App
    role: Role = None


_NAME = Annotated[
    str,
    Field(
        description=f'Such as "Till import", at most {api_tokens.MAX_NAME} characters: the '
        "history keeps it with each change."
    ),
]
_DAYS = Annotated[
    int | None,
    Field(
        description=f"{api_tokens.lifetimes()}. Leave it out for {api_tokens.DEFAULT_DAYS}.",
    ),
]


def _lifetime(days: int | None) -> None:
    """Refuse a lifetime a token made here can't have: 0, for one that never expires, with
    who makes that, and any other with the lifetimes it may have. None is the default."""
    if days == 0:
        raise ToolError(NOT_FOR_GOOD)
    if days is not None and days not in api_tokens.LIFETIMES:
        raise ToolError(
            f"A token made here works for {api_tokens.lifetimes()}: choose one in days."
        )


def add_access_tools(mcp: FastMCP, served: Apps) -> frozenset[str]:
    """Add the tools that hand out and take back access to `mcp`, and give their names, for
    `BuildersOnly` to keep them to the people who may build. The shared checks' refusals, a
    `ValueError`'s or a `Refusal`'s sentence, are answered as they are."""
    base_url = served.settings.base_url
    names: set[str] = set()
    tool = partial(builder_tool, mcp, names, (ValueError, Refusal))

    async def _owned(app: str) -> AppInfo:
        """The caller's own app, named alone or as their owner/name."""
        login, name = owned_app(served, app)
        info = await anyio.to_thread.run_sync(served.database.app, login, name)
        if info is None:
            raise ToolError(no_such_app(name))
        return info

    @tool(
        name=SHARE_APP,
        title="Share an app",
        annotations=idempotent(WRITES),
        description="Share a published app with an email, as one of its roles: it waits until "
        f"they sign in with that email and accept it. {WHEN_ASKED}",
    )
    async def share_app(
        app: App,
        email: Annotated[str, Field(description="Such as ana@example.com.")],
        role: Annotated[
            str | None,
            Field(description="Leave it out when the app has one role or none."),
        ] = None,
    ) -> CallToolResult:
        # Shared as the console's Sharing form does, and said in its words.
        info = await _owned(app)
        named = f"{info.login}/{info.name}"
        try:
            done, text = await sharing.share(served, info, email, role)
        except SandboxError as error:
            # Its roles aren't kept yet, and it can't start to read them.
            why = why_not_started(error, info.login, info.login, info.name)
            raise ToolError(f"{named} can't be started: {why}") from None
        return answer(text, {"app": named, "email": email, "done": done})

    @tool(
        name=REVOKE_SHARE,
        title="Revoke a share",
        # It takes away access the person gave: destructive, in MCP's meaning.
        annotations=idempotent(DESTROYS),
        description="Take an app back from a member, or cancel a share not yet accepted, as "
        f"read_app_overview lists them. {WHEN_ASKED}",
    )
    async def revoke_share(
        app: App,
        who: Annotated[
            str, Field(description="A member's login, or the email of a share not yet accepted.")
        ],
        role: Annotated[
            str | None, Field(description="Only this role. Leave it out for every role.")
        ] = None,
    ) -> CallToolResult:
        # In words that never say whether an email has an account.
        info = await _owned(app)
        named = f"{info.login}/{info.name}"
        gone = await anyio.to_thread.run_sync(sharing.revoke, served.database, info, who, role)
        given = who.strip()
        if not gone:
            held = f" as {role}" if role is not None else ""
            raise ToolError(
                f"Nothing of {named} is held by {given}{held}, or waits for them: call "
                "read_app_overview for its members and the shares not yet accepted."
            )
        roles = [role_label(one) for one in gone]
        if "@" in given:
            text = f"The share of {named} with {given} as {listed(roles)} is cancelled."
        else:
            # The login as it is kept.
            given = given.lower()
            text = f"{given} no longer holds {named} as {listed(roles)}, from now on."
        return answer(text, {"app": named, "who": given, "roles": roles})

    @mcp.resource(
        token_panel_uri(),
        name="Cliquet token",
        description="Where the person creates an API token, and sees its secret.",
        mime_type=views.MIME_TYPE,
    )
    def token_panel() -> str:
        return token_panel_html()

    async def _wanted(person: PersonInfo, wanted: list[TokenApp]) -> list[TokenLimit]:
        """The limits `wanted` names, one at least, each a role the person holds."""
        if not wanted:
            raise ToolError(NOT_EVERYWHERE)
        # Only the apps the token names: an owner's whose roles aren't kept yet starts.
        found = await person_tokens.named_places(served, person, [one.app for one in wanted])
        return [
            person_tokens.limit_for(place, one.role)
            for place, one in zip(found, wanted, strict=True)
        ]

    @tool(
        name=CREATE_TOKEN,
        title="Create a token",
        annotations=WRITES,
        meta={"ui": {"resourceUri": token_panel_uri()}},
        description="Create an API token for an agent that runs on its own, limited to apps, one "
        "role in each: the answer gives the address where it works for each app. "
        f"{WHEN_ASKED}",
    )
    async def create_token(name: _NAME, apps: list[TokenApp], days: _DAYS = None) -> CallToolResult:
        # Made when a token for everywhere asks; with a sign-in, the person makes it.
        _lifetime(days)
        person = caller(served)
        limits = await _wanted(person, apps)
        maker = api_token()
        if maker is not None:
            # Without days, it ends with its maker if that one ends sooner than the default.
            info, secret = await person_tokens.create_token(
                served, person, name, limits, made_by=maker.id, days=days
            )
            return _made(info, secret, person_tokens.where(base_url, info.limits))
        # A sign-in: the person makes it themselves, and the secret never reaches the model.
        days = days or api_tokens.DEFAULT_DAYS
        name = await person_tokens.check_token(served, person, name, limits)
        where = person_tokens.where(base_url, tuple(limits))
        link = f"{base_url}{filled_tokens_link(name, limits, days)}"
        works = f"{where['label']}, for {days} days"
        if draws_screens():
            text = (
                f"The person now sees a panel in the conversation to create the token {name}. "
                f"{works}. They click Create there and see its secret there: you never "
                "do. Once it is made, the panel gives you its token_id, or the person tells you. "
                f"If the panel doesn't show, give them this link instead: {link}"
            )
            asked = [_asked(one) for one in limits]
            data = {"name": name, "apps": asked, "days": days, "where": where, "link": link}
            return answer(text, data)
        text = (
            f"The person makes the token {name} themselves, so its secret never reaches you: "
            f"give them this link to the API tokens page, with its form filled in: {link} "
            f"{works}. They click Make a token there, and copy its secret into the "
            f"agent's settings. Then {LIST_TOKENS} shows it."
        )
        return answer(text, {"name": name, "days": days, "where": where, "link": link})

    @tool(
        name=ISSUE_TOKEN,
        title="Create a token from the panel",
        annotations=WRITES,
        meta={"ui": {"visibility": ["app"]}},
    )
    async def issue_token(
        name: str, apps: list[TokenApp], days: int = api_tokens.DEFAULT_DAYS
    ) -> CallToolResult:
        """Used by the panel create_token shows. Don't call it yourself."""
        # The secret passes through the host to the panel, never through the model: a client
        # that draws no panel may still list the tool to its model.
        if not draws_screens():
            raise ToolError(
                f"{ISSUE_TOKEN} is for the panel {CREATE_TOKEN} shows. Give the person the link "
                f"from {CREATE_TOKEN} instead."
            )
        if api_token() is not None:
            raise ToolError(
                f"{ISSUE_TOKEN} is for a person who signed in: with a token, {CREATE_TOKEN} "
                "makes the token itself."
            )
        # Checked again, as the person created it in the panel.
        _lifetime(days)
        person = caller(served)
        limits = await _wanted(person, apps)
        info, secret = await person_tokens.create_token(served, person, name, limits, days=days)
        # The panel shows the secret from the data: the text, which a host may show its model,
        # holds none.
        where = person_tokens.where(base_url, info.limits)
        text = f"Made the token {info.name}, {info.public_id}. The panel shows its secret, once."
        return answer(text, _made_data(info, secret, where))

    @tool(
        name=LIST_TOKENS,
        title="List your tokens",
        annotations=READS,
        description="List the person's API tokens: where each works, when it was made, last "
        "used and expires, and which token made it.",
    )
    async def list_tokens() -> CallToolResult:
        # The one the call is made with is marked `current`.
        person = caller(served)
        tokens = await anyio.to_thread.run_sync(served.database.tokens, person.id)
        current = api_token()
        now = served.now()
        rows = [
            {
                "token_id": one.public_id,
                "name": one.name,
                "where": person_tokens.where(base_url, one.limits),
                "created_at": one.created_at.isoformat(),
                "last_used_at": one.last_used_at.isoformat() if one.last_used_at else None,
                "expires_at": one.expires_at.isoformat() if one.expires_at else None,
                "expired": one.expired(now),
                "made_by": one.made_by,
                "current": current is not None and current.id == one.public_id,
            }
            for one in tokens
        ]
        data = {"tokens": rows}
        if not rows:
            return answer(f"The person has no API token. {CREATE_TOKEN} makes one.", data)
        text = (
            "The person's API tokens, newest first. `current` marks the one you call with, "
            f"`made_by` the token that made one, which takes it along when it is revoked, and "
            "`expires_at` null one that never expires. An expired one works nowhere, and keeps "
            "its name until it is revoked:\n"
            f"{compact(data)}"
        )
        return answer(text, data)

    @tool(
        name=REVOKE_TOKEN,
        title="Revoke a token",
        # It takes away access the person gave: destructive, in MCP's meaning.
        annotations=idempotent(DESTROYS),
        description="Revoke one of the person's API tokens, and the tokens it made, at once. "
        f"{WHEN_ASKED}",
    )
    async def revoke_token(
        token_id: Annotated[str, Field(description=f"Its tok_ id, from {LIST_TOKENS}.")],
    ) -> CallToolResult:
        person = caller(served)
        given = token_id.strip()
        if api_tokens.is_token(given):
            # A secret, pasted by mistake: never repeated, not even in a refusal.
            raise ToolError(
                f"That is a token's secret, not its id: pass its tok_ id, from {LIST_TOKENS}. "
                "A secret pasted in a conversation is no longer secret: revoke that token."
            )
        database = served.database
        found = await anyio.to_thread.run_sync(database.find_token, person.id, given)
        went = None
        if found is not None:
            went = await anyio.to_thread.run_sync(database.revoke_token, person.id, found[0])
        if found is None or went is None:
            raise ToolError(
                f"The person has no token with the id {given!r}: call {LIST_TOKENS} for their "
                "tokens' ids."
            )
        name = found[1]
        text = f"Revoked {name}: it works nowhere any more."
        if went:
            text += f" The tokens it made went with it: {', '.join(went)}."
        current = api_token()
        if current is not None and current.id == given:
            text += " It is the token you call with: your next call with it is refused."
        return answer(text, {"token_id": given, "name": name, "went_with_it": went})

    return frozenset(names)


def _asked(limit: TokenLimit) -> dict[str, str]:
    """One app of a token the panel creates, as `create_token` and `issue_token` take it."""
    app = {"app": f"{limit.login}/{limit.name}"}
    return app if limit.role == "-" else {**app, "role": limit.role}


def _made(info: TokenInfo, secret: str, where: dict[str, Any]) -> CallToolResult:
    """The answer to a token for everywhere that made a token: its secret, this once."""
    addresses = [one["address"] for one in where["addresses"]]
    # A token made here always expires.
    assert info.expires_at is not None
    text = (
        f"Made the token {info.name}, {info.public_id}. This is the only time its secret is "
        f"shown: {secret}\n\n"
        f"{where['label']}, at {', '.join(addresses)}, until {api_tokens.expiry(info.expires_at)}. "
        "The agent sends it as `Authorization: Bearer <secret>` with every request there. Give "
        "it only to that agent: it goes when the token you call with is revoked."
    )
    return answer(text, _made_data(info, secret, where))


def _made_data(info: TokenInfo, secret: str, where: dict[str, Any]) -> dict[str, Any]:
    """What a new token's answer holds: its name, its id, its secret, where it works and when
    it expires."""
    expires_at = info.expires_at.isoformat() if info.expires_at else None
    return {
        "name": info.name,
        "token_id": info.public_id,
        "token": secret,
        "where": where,
        "expires_at": expires_at,
    }
