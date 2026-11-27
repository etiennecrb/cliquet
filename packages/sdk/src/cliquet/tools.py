"""The tools a caller gets: the app's actions, queries and views, by its manifest, and the
built-in ones, the same in every app.

`BUILT_IN` is the one table of the built-in tools: for each, what it takes, who gets it,
what answers it, and what the server's instructions say of it. The MCP server lists them from
it, answers them, and checks a call's arguments against it. It imports nothing of MCP, so the
worker checks a screen's calls without loading it.

Every name Cliquet adds to an app's tools starts with `app_`, which no app may use: it names
what the built-in tools act on, the app itself.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, get_args

from pydantic_core.core_schema import ErrorType

PREFIX = "app_"
# The arguments the server adds: `app_request_id`, `app_remark` and `app_if_unchanged_since`
# to every action, and `app_transaction` to every action, query and view, and to the built-in
# tools on the history, for a role that writes.
REQUEST_ID = "app_request_id"
TRANSACTION = "app_transaction"
REMARK = "app_remark"
UNCHANGED = "app_if_unchanged_since"
# What the server adds to every query's answer: the version of the data the query read, the
# id of its commit, which an action takes back as `app_if_unchanged_since`.
DATA_VERSION = "app_data_version"
# How long a remark may be, in characters, on one line.
MAX_REMARK = 200
REQUEST_ID_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "Optional. A unique id for this call, such as a UUID. When you retry after a "
        "timeout or a lost connection, send the same id: a call already done isn't done twice."
    ),
}
REMARK_SCHEMA: dict[str, Any] = {
    "type": "string",
    "maxLength": MAX_REMARK,
    "description": (
        "Why this change: what the person asked for, or the source it came from, such as "
        f'"statement of March, line 14". One line, at most {MAX_REMARK} characters, kept for '
        "good in the app's history with the change, where whoever may read the change reads "
        "it. Send one with every change. Leave out people's names and the values: the change "
        "holds the values, and history can't forget either."
    ),
}
REVERT_REMARK_SCHEMA: dict[str, Any] = {
    **REMARK_SCHEMA,
    "description": (
        "Why you undo this change: what the person asked for, or what was wrong with it, such "
        f'as "entered twice". One line, at most {MAX_REMARK} characters, kept for good in the '
        "app's history with the undo. Send one with every undo. Leave out people's names and "
        "the values."
    ),
}
UNCHANGED_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        f"Optional. The `{DATA_VERSION}` of a query's answer. Send it when this change depends "
        "on what that query told you: if the data changed since, the call is refused without "
        "running, and you read again and decide again."
    ),
}
DATA_VERSION_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "Added by Cliquet, not part of the app's answer: the version of the data this answer "
        f"read, the id of its commit. Pass it as `{UNCHANGED}` to an action that depends on "
        "this answer."
    ),
}
COMMIT_SCHEMA: dict[str, Any] = {
    "type": ["string", "null"],
    "description": "The commit that kept the change: null when nothing had to change.",
}
FILES_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string"},
    "description": "The data files the change wrote.",
}


def change_schema(result: dict[str, Any]) -> dict[str, Any]:
    """What a change answers as structured content, with `result` the schema of its result."""
    return {
        "type": "object",
        "properties": {"commit": COMMIT_SCHEMA, "files": FILES_SCHEMA, "result": result},
        "required": ["commit", "files", "result"],
    }


TRANSACTION_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "Optional. The id from app_begin_transaction: the call reads or changes that "
        "transaction's data, which is kept only once you commit it."
    ),
}
# How to use a transaction: in the instructions, and app_begin_transaction's description and
# answer.
STEPS = (
    f"pass its id as `{TRANSACTION}` to each call, read the result with the app's queries or "
    "show it with a view, then app_commit_transaction or app_rollback_transaction"
)
# What Cliquet does itself has a name no app's action can have: an invariant's `ctx.action`,
# and what the history reads a change as.
# The tool that undoes a change, and the action its change reads as, by its trailer.
REVERT = "app_revert"
# The tool that keeps a transaction, and the action of the commit that keeps it, which the
# history never lists: it is Cliquet's, so a write cut off after it is finished (`repair`).
COMMIT_TRANSACTION = "app_commit_transaction"
# The tool that drops a transaction.
ROLLBACK_TRANSACTION = "app_rollback_transaction"
# The tool that makes a reference to a file the person uploads, for an action's file argument.
ASK_FOR_FILE = "app_ask_for_file"
# A publish: the action it reads as in the history, by its `App-Version` trailer, whatever
# its label, and the action its migration's change is checked as, since the publish keeps it.
PUBLISH = "app_publish"
# A check of the data as it is, which changes nothing: before a version goes live, and when
# the app is checked.
CHECK = "app_check"
# A test's `world.change`: an edit of the data that no action made.
CHANGE = "app_change"
# What every built-in tool's description ends with.
SAME = "This is a built-in tool, the same in every Cliquet app."


def listed(names: list[str]) -> str:
    """Names joined as a sentence lists them: a, b and c. No names make an empty string."""
    if not names:
        return ""
    return " and ".join(filter(None, [", ".join(names[:-1]), names[-1]]))


def reserved(name: str) -> bool:
    """Whether a name is Cliquet's: it starts with `app_`, in any case."""
    return name.casefold().startswith(PREFIX)


@dataclass(frozen=True)
class Access:
    """What a caller's role may use of the app's actions, queries and views, by name, with
    their manifest."""

    actions: dict[str, dict[str, Any]]
    queries: dict[str, dict[str, Any]]
    views: dict[str, dict[str, Any]]

    @property
    def writes(self) -> bool:
        """Whether the role may call an action, and so use transactions."""
        return bool(self.actions)


def access(manifest: dict[str, Any], role: str | None) -> Access:
    """What a caller with `role` may use of the app whose manifest this is."""

    def usable(kind: str) -> dict[str, dict[str, Any]]:
        return {
            spec["name"]: spec
            for spec in manifest[kind]
            if spec["roles"] is None or role in spec["roles"]
        }

    return Access(usable("actions"), usable("queries"), usable("views"))


class NoSuchRole(ValueError):
    """A role that isn't one of the app's. The message says which roles it has."""

    @property
    def sentence(self) -> str:
        """The message as a sentence of its own, with its capital and full stop. The role
        names in it keep their case."""
        text = str(self)
        return f"{text[:1].upper()}{text[1:]}."


def app_role(manifest: dict[str, Any], role: str | None) -> str | None:
    """`role`, if a caller of the app whose manifest this is may act with it: one of its
    roles, or None for an app without roles. Raises `NoSuchRole` otherwise."""
    roles = manifest["roles"]
    if not roles:
        if role is not None:
            raise NoSuchRole(f"the app has no roles, so not {role}: use - for the role")
        return None
    if role not in roles:
        raise NoSuchRole(f"the app has no role {role or '-'}. Its roles are: {', '.join(roles)}")
    return role


@dataclass(frozen=True)
class BuiltIn:
    """A tool of every app: what it takes, and who gets it. Its answer is the MCP server's."""

    name: str
    # The method of the MCP server's `AppServer` that answers it.
    handler: str
    # What it does: its description says that, then that every app has it.
    does: str
    # The tool's arguments, for a caller with this access.
    arguments: Callable[[Access], dict[str, Any]]
    required: tuple[str, ...] = ()
    gets: Callable[[Access], bool] = lambda access: True
    writes: bool = False
    # Whether it takes `app_transaction`, as an action, a query or a view does: the call
    # then reads or changes that transaction's data.
    in_transaction: bool = False
    # What the server's instructions say of it, to a caller who gets it.
    instructions: Callable[[Access], str] = lambda access: ""

    @property
    def description(self) -> str:
        """The tool's description: what it does, and that it is the same in every app."""
        return f"{self.does} {SAME}"

    def schema(self, access: Access) -> dict[str, Any]:
        """The tool's input schema, for a caller with `access`."""
        properties = self.arguments(access)
        if self.in_transaction and access.writes:
            properties = {**properties, TRANSACTION: TRANSACTION_SCHEMA}
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if self.required:
            schema["required"] = list(self.required)
        return schema

    def problem(self, access: Access, arguments: dict[str, Any]) -> str | None:
        """What is wrong with a call's `arguments`, by the tool's schema for a caller with
        `access`, or None: an argument it doesn't take, a required one missing, or a value of
        the wrong type or past a limit. A null, or an empty required string, counts as left out.
        """
        properties = self.schema(access)["properties"]
        found: list[str] = []
        unknown = [key for key in arguments if key not in properties]
        if unknown:
            found.append(unknown_text(self.name, unknown, list(properties)))
        for key in self.required:
            # An empty string names nothing either: "commit": "" is still a missing commit.
            if arguments.get(key) in (None, ""):
                found.append(_missing_text(key, properties[key]["description"]))
        for key, value in arguments.items():
            if key in properties and value is not None:
                wrong = _value_problem(key, value, properties[key])
                if wrong is not None:
                    found.append(wrong)
        return " ".join(found) or None


def _value_problem(key: str, value: object, schema: dict[str, Any]) -> str | None:
    """What is wrong with one argument's value, by its schema's type and limits, if anything."""
    kind = schema.get("type")
    if kind == "integer":
        low, high = schema.get("minimum"), schema.get("maximum")
        whole = isinstance(value, int) and not isinstance(value, bool)
        if whole and (low is None or low <= value) and (high is None or value <= high):
            return None
        within = f" from {low} to {high}" if low is not None and high is not None else ""
        return f"{key} must be a whole number{within}."
    if kind == "string" and not isinstance(value, str):
        return f"{key} must be a string."
    if kind == "object" and not isinstance(value, dict):
        return f"{key} must be an object."
    return None


def takes_text(takes: list[str]) -> str:
    """The sentence that says which arguments a tool takes."""
    return f"It takes {listed(takes) if takes else 'no arguments'}."


def unknown_text(name: str, unknown: list[str], takes: list[str]) -> str:
    """The sentences for arguments a tool doesn't take, and those it takes."""
    return f"{name} doesn't take {listed(unknown)}. {takes_text(takes)}"


def wrong_arguments(
    name: str, errors: Sequence[Any], takes: dict[str, str | None], given: dict[str, Any]
) -> str:
    """What is wrong with the arguments given to an app's tool, one line per problem."""
    # From the `errors` of a Pydantic validation error: arguments it doesn't take, first, then
    # one missing, or a wrong value. `takes` maps each argument by its public name to the
    # description the app gave it, which a problem with that argument repeats.
    unknown: list[str] = []
    lines: list[str] = []
    for error in errors:
        loc, kind = tuple(error["loc"]), error["type"]
        path = _path(loc, given, kind == "missing")
        if kind == "extra_forbidden" and len(loc) == 1:
            unknown.append(path)
            continue
        if kind == "extra_forbidden":
            line = f"{_path(loc[:-1], given, False)} doesn't take {loc[-1]}."
        elif kind == "missing":
            line = _missing_text(path, takes.get(path))
        else:
            line = _wrong_value(path, kind, error["msg"], takes.get(path))
        if line not in lines:
            lines.append(line)
    if unknown:
        lines.insert(0, unknown_text(name, unknown, list(takes)))
    return "\n".join(lines)


# The subject of a Pydantic message on a value, such as "Input" in "Input should be greater
# than 0", whose place the argument's name takes. Only in Pydantic's own kinds of error: the
# text of an app's own is the app's.
_SUBJECT = re.compile(r"[A-Z][A-Za-z]*(?: input)? should ")
_PYDANTIC_KINDS = frozenset(get_args(ErrorType))
# Pydantic's kinds of error on a value past a limit, whose message says the limit in full,
# such as "should be greater than 0": the argument's description would only repeat it.
_LIMITS = frozenset(
    {
        "greater_than",
        "greater_than_equal",
        "less_than",
        "less_than_equal",
        "multiple_of",
        "too_short",
        "too_long",
        "string_too_short",
        "string_too_long",
        "bytes_too_short",
        "bytes_too_long",
        "decimal_max_digits",
        "decimal_max_places",
        "decimal_whole_digits",
    }
)
# What Pydantic puts before the text of an error raised in a validator: the app's own words.
_RAISED = ("Value error, ", "Assertion failed, ")


def _path(loc: tuple[Any, ...], given: Any, missing: bool) -> str:
    """Where a problem is in the arguments `given`, by public names, such as lines[0].quantity.
    A step of `loc` that is no key or index of what was given, such as the member of a union
    that Pydantic names, is left out, except the last step of a `missing` key."""
    parts: list[str] = []
    value = given
    for i, step in enumerate(loc):
        last = i == len(loc) - 1
        if isinstance(step, int) and isinstance(value, list) and -len(value) <= step < len(value):
            parts.append(f"[{step}]")
            value = value[step]
        elif (
            isinstance(step, str)
            and isinstance(value, dict)
            and (step in value or (last and missing))
        ):
            parts.append(f".{step}" if parts else step)
            value = value.get(step)
    return "".join(parts)


def _wrong_value(path: str, kind: str, message: str, description: str | None) -> str:
    """A wrong value at `path`, from Pydantic's `message` of what it should be, with the
    argument's `description`, if the app gave one and the message doesn't say a limit."""
    raised = next((message.removeprefix(p) for p in _RAISED if message.startswith(p)), None)
    subject = _SUBJECT.match(message) if kind in _PYDANTIC_KINDS else None
    if not path:
        text = raised or message
    elif raised is not None:
        text = f"{path} is wrong: {raised}"
    elif subject is not None:
        text = f"{path} should {message[subject.end() :]}"
    else:
        text = f"{path} is wrong: {message}"
    text = text.rstrip().removesuffix(".")
    if description is None or not description.strip() or kind in _LIMITS:
        return f"{text}."
    return f"{text}: {_lowered(description)}"


def _missing_text(key: str, description: str | None) -> str:
    """The sentence for a required argument left out, with its description if it has one."""
    if description is None or not description.strip():
        return f"{key} is required."
    return f"{key} is required: {_lowered(description)}"


def _lowered(sentence: str) -> str:
    """A description, as the end of a sentence with its full stop: "The id." becomes "the
    id.". A first word in capitals, such as "SKU" or "I", keeps them."""
    sentence = sentence.strip()
    first = sentence.split(maxsplit=1)[0] if sentence else ""
    if not (first.isupper() and (len(first) > 1 or first == "I")):
        sentence = sentence[:1].lower() + sentence[1:]
    return sentence if sentence.endswith((".", "!", "?")) else f"{sentence}."


_SINCE = {
    "type": "string",
    "description": (
        "A time in ISO 8601, with its zone, such as 2026-11-01T00:00:00+01:00, or a version "
        f"of the data, such as an answer's `{DATA_VERSION}`."
    ),
}
# How many changes `app_history` lists when the caller doesn't say.
HISTORY_LIMIT = 20
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 200, "default": HISTORY_LIMIT}
_COMMIT = {
    "type": "string",
    "description": "A commit id from app_history, in full or its first 7+ characters.",
}
_ID = {"type": "string", "description": "The id app_begin_transaction gave."}


def _changes_since(access: Access) -> dict[str, Any]:
    return {
        "query": {
            "type": "string",
            "enum": list(access.queries),
            "description": "One of your queries.",
        },
        "arguments": {
            "type": "object",
            "description": "The query's arguments, as you would call it.",
            "default": {},
        },
        "since": _SINCE,
    }


def _writes(access: Access) -> bool:
    return access.writes


def _queries(access: Access) -> bool:
    """Whether the caller gets `app_changes_since`: a role without queries would learn
    nothing from it."""
    return bool(access.queries)


def file_actions(access: Access) -> dict[str, str]:
    """The caller's actions that take a file, each with its file argument's name. A manifest
    made by an SDK before file arguments names none."""
    found = {name: spec.get("file_argument") for name, spec in access.actions.items()}
    return {name: argument for name, argument in found.items() if isinstance(argument, str)}


def _takes_files(access: Access) -> bool:
    """Whether the caller gets `app_ask_for_file`: one of their actions takes a file."""
    return bool(file_actions(access))


def _ask_for_file(access: Access) -> dict[str, Any]:
    return {
        "action": {
            "type": "string",
            "enum": list(file_actions(access)),
            "description": "The action that takes the file.",
        },
        "argument": {
            "type": "string",
            "description": "Optional. The action's argument that is the file: its only one.",
        },
    }


def _begin_instructions(access: Access) -> str:
    text = f"To check changes before they are kept, call `app_begin_transaction`, then {STEPS}."
    if _queries(access):
        text += (
            f" In a transaction, `app_changes_since` from the `{DATA_VERSION}` "
            "`app_begin_transaction` answers shows what your calls change in a query."
        )
    text += " `app_transaction_calls` lists the calls a transaction holds."
    return text + " A transaction is dropped an hour after its last change."


BUILT_IN: dict[str, BuiltIn] = {
    tool.name: tool
    for tool in (
        BuiltIn(
            "app_history",
            "_history",
            "Your latest changes to the data, or every change if you are the app's owner, "
            "newest first: who made each, with which action and which files changed, and "
            "`agent`, the name of the API token it was made with, if any. A new "
            f"version that changed the data reads as {PUBLISH}. With `since`, only those made "
            "after that version, or at that time or after: a time may list one made a little "
            "before, and never misses one made after.",
            lambda access: {"limit": _LIMIT, "since": _SINCE},
            in_transaction=True,
            instructions=lambda access: "Every change is recorded: see yours with `app_history`.",
        ),
        BuiltIn(
            "app_changes_since",
            "_changes_since",
            "What changed in one of your queries' answers since a version of the data or a "
            "time: the query runs on the data as it was then and as it is now. For a list, "
            "`added` holds the new items and `removed` the ones gone, so a changed item is in "
            "both; for any other answer, the new value and the old one. Use it to catch up "
            "since you last looked: pass the `now` of its last answer as `since`, and nothing "
            "is missed or told twice. A time never misses a change made at that time or after, "
            "and may count one made a little before. A transaction's changes count from when it "
            "was committed.",
            _changes_since,
            ("query", "since"),
            gets=_queries,
            in_transaction=True,
            instructions=lambda access: (
                "To see what changed in a query's answer since a time, call `app_changes_since`."
            ),
        ),
        BuiltIn(
            REVERT,
            "_revert",
            "Undo one of your changes, or anyone's if you are the app's owner, keeping later "
            "ones. It goes through the app's rules like any other change.",
            lambda access: {
                "commit": _COMMIT,
                REQUEST_ID: REQUEST_ID_SCHEMA,
                REMARK: REVERT_REMARK_SCHEMA,
            },
            ("commit",),
            # A role that only reads has made no change to undo.
            gets=_writes,
            writes=True,
            in_transaction=True,
            instructions=lambda access: "Undo one of your changes with `app_revert`.",
        ),
        BuiltIn(
            ASK_FOR_FILE,
            "_ask_for_file",
            "Ask the person for a file an action takes, so you never copy its contents: it "
            "answers a reference at once, and a link to the page where the person uploads the "
            "file. Pass the reference as the action's file argument once they did: it works "
            "once, for you alone, and ends an hour after the file came, or an hour after you "
            "asked if no file came.",
            _ask_for_file,
            ("action",),
            gets=_takes_files,
            instructions=lambda access: (
                "When an action takes a file, call `app_ask_for_file`, and pass the reference it "
                "gives once the person uploaded the file: never the file's contents."
            ),
        ),
        BuiltIn(
            "app_begin_transaction",
            "_begin",
            "Begin a transaction: a group of calls to this app whose changes are kept only "
            "once you commit it, all together, or not at all. Use one to check changes before "
            f"they are kept: {STEPS}. It answers the transaction's id and the "
            f"`{DATA_VERSION}` it began from: the `since` that shows what the transaction "
            "changes.",
            lambda access: {},
            gets=_writes,
            writes=True,
            instructions=_begin_instructions,
        ),
        BuiltIn(
            "app_transaction_calls",
            "_transaction_calls",
            "List the calls of your open transaction, oldest first: each one's action, its "
            "arguments, and the files it changed, with how many lines it added and removed. "
            "Use it to say what the transaction holds before you commit it.",
            lambda access: {TRANSACTION: _ID},
            (TRANSACTION,),
            gets=_writes,
        ),
        BuiltIn(
            COMMIT_TRANSACTION,
            "_commit",
            "Commit a transaction: keep what its calls changed, as one commit per call, and "
            f"answer each commit, oldest first, and the `{DATA_VERSION}` of the data it leaves. "
            "If someone else changed the data since it began, nothing is kept.",
            lambda access: {TRANSACTION: _ID},
            (TRANSACTION,),
            gets=_writes,
            writes=True,
        ),
        BuiltIn(
            ROLLBACK_TRANSACTION,
            "_rollback",
            "Roll back a transaction: nothing its calls changed is kept.",
            lambda access: {TRANSACTION: _ID},
            (TRANSACTION,),
            gets=_writes,
            writes=True,
        ),
    )
}
# The built-in tools that take `app_transaction`, as an action, a query or a view does.
IN_TRANSACTION = frozenset(name for name, tool in BUILT_IN.items() if tool.in_transaction)


# Some MCP clients accept a tool's full name, which starts with its server's, only up to this
# many characters.
MAX_FULL_NAME = 64


def full_name(app: str, tool: str) -> str:
    """A tool's name in an MCP client that puts its server's name first, as some do."""
    return f"mcp__{app}__{tool}"


# The longest name an app may have: the longest built-in tool's full name still fits.
MAX_APP_NAME = MAX_FULL_NAME - len(full_name("", max(BUILT_IN, key=len)))

# Some MCP clients accept a key in a tool's schema, such as an argument's name or a field's,
# only up to this many characters.
MAX_KEY = 64


def built_ins(access: Access) -> list[BuiltIn]:
    """The built-in tools a caller with `access` gets, in the order they are listed."""
    return [tool for tool in BUILT_IN.values() if tool.gets(access)]
