"""The arguments of the tools on the person's endpoint, the builder's too: the app and the role
they share, wrong ones answered in plain words, and an object or an array taken as its JSON
text too.

`pick_app` finds the app a tool names, alone or as owner/name, and `chosen_role` the role it
acts as, with the same sentences on every tool that takes them.
"""

import json
from collections.abc import Iterable
from difflib import get_close_matches
from typing import Annotated, Any

from fastmcp.exceptions import ToolError, ValidationError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp_types import CallToolRequestParams
from pydantic import Field
from pydantic import ValidationError as PydanticValidationError

from cliquet.tools import listed, wrong_arguments

# The app a tool acts on, the same for the person's tools and the builder's.
App = Annotated[str, Field(description="The app, as list_apps names it, such as sam/budget.")]
# The role a tool acts as: the owner holds every role of their app.
Role = Annotated[
    str | None,
    Field(description="The role to act as, one the person holds. Leave it out when they hold one."),
]
# The arguments of one of an app's tools or views.
Arguments = Annotated[dict[str, Any] | None, Field(description="Its arguments, as an object.")]


class Refusal(Exception):
    """A call a tool answers itself, as an error the agent can act on."""


def pick_app(
    given: str, names: Iterable[tuple[str, str]] | None, missing: str | None = None
) -> tuple[str, str]:
    """The owner and name of the app `given` names, as owner/name or as its name alone, among
    `names`, the apps it may be: a name alone is the one app that has it. With no `names`, an
    owner/name is taken as it is, for the caller to check. Raises `Refusal`: `missing`, or
    `no_app`'s, when none is the app."""
    given = given.strip()
    login, slash, name = given.rpartition("/")
    if slash and names is None:
        return login, name
    found = [one for one in names or () if one == (login, name) or (not slash and one[1] == name)]
    if len(found) > 1:
        several = ", ".join(f"{owner}/{one}" for owner, one in found)
        raise Refusal(f"Several apps are named {given}: {several}. Name one as owner/name.")
    if not found:
        raise Refusal(missing or no_app(given))
    return found[0]


def no_app(app: str) -> str:
    """Why there is no such app: the person may not use it, or it doesn't exist."""
    return f"There is no app {app} for you. Call list_apps to see the apps you may use."


def no_roles(app: str) -> str:
    """Why a role is refused in an app without roles."""
    return f"{app} has no roles: leave role out."


def name_your_role(app: str, held: list[str]) -> str:
    """Why a role left out is refused where the person holds several."""
    return f"Name your role in {app}: you hold {_held(held)}."


def not_held(app: str, role: str, held: list[str]) -> str:
    """Why a role the person doesn't hold is refused: it never says the app's others."""
    return f"You don't hold the role {role} in {app}. You hold {_held(held)}."


def _held(held: list[str]) -> str:
    """The roles the person holds, as a sentence names them."""
    return listed(held) if held else "none"


def chosen_role(app: str, role: str | None, held: list[str]) -> str:
    """The role a call names in `app`, among `held`, the person's roles there: the one they
    hold when it is left out, and `-` in an app without roles. Raises `Refusal`."""
    if not held:
        if role not in (None, "", "-"):
            raise Refusal(no_roles(app))
        return "-"
    if not role:
        if len(held) > 1:
            raise Refusal(name_your_role(app, held))
        return held[0]
    if role not in held:
        raise Refusal(not_held(app, role, held))
    return role


# The argument that holds an app's tool's own, on the tools that call one.
_NESTED = "arguments"
# Pydantic's kinds of error on a function's arguments, as `wrong_arguments` names them.
_KINDS = {"missing_argument": "missing", "unexpected_keyword_argument": "extra_forbidden"}


class PlainArguments(Middleware):
    """Take an object or an array sent as its JSON text, as models often send one, and answer
    a call with wrong arguments in plain words, as an app's endpoint does: what is wrong with
    which argument, and what the tool takes, never Pydantic's text."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Run the call, or say what is wrong with its arguments."""
        name = context.message.name
        found = context.fastmcp_context
        tool = None if found is None else await found.fastmcp.get_tool(name)
        if tool is None:
            return await call_next(context)
        given = _from_text(context.message.arguments or {}, tool.parameters)
        context = context.copy(message=context.message.model_copy(update={"arguments": given}))
        try:
            return await call_next(context)
        except ValidationError as error:
            cause = error.__cause__
            if not isinstance(cause, PydanticValidationError):
                raise
            properties = tool.parameters.get("properties", {})
            takes = {key: spec.get("description") for key, spec in properties.items()}
            errors = [
                {**one, "type": _KINDS.get(one["type"], one["type"])}
                for one in cause.errors(include_url=False)
            ]
            text = wrong_arguments(name, errors, takes, given)
            # A name close to one the tool takes is a typo of it, not an app's argument.
            unknown = [
                one
                for one in errors
                if one["type"] == "extra_forbidden"
                and len(one["loc"]) == 1
                and not get_close_matches(str(one["loc"][0]), list(takes), n=1)
            ]
            if _NESTED in takes and unknown:
                # An app's tool's argument, or one Cliquet adds, given beside `arguments`: the
                # sentence that names it comes first.
                first, *rest = text.split("\n")
                which = "it" if len(unknown) == 1 else "them"
                text = "\n".join([f"{first} Put {which} inside `{_NESTED}`.", *rest])
            if not any(one["type"] == "extra_forbidden" for one in errors):
                # The sentence for an argument it doesn't take already says what it takes.
                text += f"\n{name} takes {listed(list(takes)) if takes else 'no arguments'}."
            raise ToolError(text) from None


def _from_text(given: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """The arguments `given`, each object or array sent as its JSON text, where the tool's
    `schema` takes one and no string, parsed: anything else as it was given, for its type to
    accept or refuse."""
    properties, defs = schema.get("properties", {}), schema.get("$defs", {})
    parsed = dict(given)
    for key, value in given.items():
        kinds = _kinds(properties.get(key, {}), defs)
        if not isinstance(value, str) or "string" in kinds:
            continue
        try:
            read = json.loads(value)
        except ValueError:
            continue
        if ("object" in kinds and isinstance(read, dict)) or (
            "array" in kinds and isinstance(read, list)
        ):
            parsed[key] = read
    return parsed


def _kinds(schema: Any, defs: dict[str, Any]) -> set[str]:
    """The JSON types a schema takes, through its references and choices."""
    if not isinstance(schema, dict):
        return set()
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return _kinds(defs.get(ref.rsplit("/", 1)[-1]), defs)
    kind = schema.get("type")
    found = {kind} if isinstance(kind, str) else set(kind) if isinstance(kind, list) else set()
    for one in (*schema.get("anyOf", ()), *schema.get("oneOf", ())):
        found |= _kinds(one, defs)
    return found
