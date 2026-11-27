"""The engine that runs an app's code in a sandbox."""

import base64
import contextlib
from collections.abc import Callable, Collection, Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any

from cliquet.context import Caller
from cliquet.errors import (
    AppError,
    AppFailed,
    Denied,
    Problem,
    Refused,
    SafeError,
    shortened,
    type_name,
)
from cliquet.files import data_name_problem, declares
from cliquet.inputs import Input
from cliquet.limits import MAX_DATA, MAX_RESULT, amount_text, size_text
from cliquet.worker import (
    MAX_TRACEBACK,
    NAMED_OPS,
    Changes,
    Checked,
    Refusal,
    WrongArguments,
    json_length,
)
from cliquet_server.executor import MAX_ERROR, SandboxClient, UnknownVersion, decoded, encoded


class UnknownName(SafeError):
    """The sandbox has no action, query or view of the name asked for. Its text is Cliquet's."""


class SandboxEngine:
    """Runs an app's code in a sandbox. `Runtime` takes it as its engine.

    `used`, when given, makes what each call to the sandbox runs in, such as the count of
    the owner's sandbox time.
    """

    manifest: dict[str, Any]

    def __init__(
        self,
        client: SandboxClient,
        login: str,
        app: str,
        version: str,
        manifest: dict[str, Any],
        code: Callable[[], dict[str, bytes]],
        used: Callable[[], AbstractContextManager[object]] | None = None,
    ) -> None:
        self._client = client
        self._login = login
        self._app = app
        self._version = version
        self.manifest = manifest
        self._code = code
        self._used = used

    def run_action(
        self,
        caller: Caller,
        now: datetime,
        action: str,
        arguments: dict[str, Any],
        files: dict[str, bytes],
        inputs: Mapping[str, Input] | None = None,
    ) -> Changes | Refusal:
        """Run an action in the sandbox: its file, in `inputs`, travels with the call, as the
        data does."""
        answer = self._ask(
            {
                "op": "call",
                "caller": _caller(caller),
                "now": now.isoformat(),
                "name": action,
                "arguments": arguments,
                "files": encoded(files),
                "inputs": {
                    argument: {
                        "name": one.name,
                        "content": base64.b64encode(one.content).decode(),
                    }
                    for argument, one in (inputs or {}).items()
                },
            }
        )
        return _change(answer, self.manifest["files"], allow_result=True)

    def migrate(self, caller: Caller, files: dict[str, bytes]) -> Changes | Refusal:
        """Run the version's `migrate.py` on a working copy of `files`, then every invariant."""
        answer = self._ask(
            {
                "op": "migrate",
                "caller": _caller(caller),
                "files": encoded(files),
            }
        )
        removable = [name for name in files if not declares(self.manifest["files"], name)]
        return _change(answer, self.manifest["files"], removable, what="a migration")

    def apply(
        self,
        caller: Caller,
        label: str,
        files: dict[str, bytes],
        result: dict[str, bytes],
    ) -> Changes | Refusal:
        answer = self._ask(
            {
                "op": "apply",
                "caller": _caller(caller),
                "label": label,
                "files": encoded(files),
                "result": encoded(result),
            }
        )
        return _change(answer, self.manifest["files"], what=f"the change {label!r}")

    def query_json(
        self,
        caller: Caller,
        now: datetime,
        query: str,
        arguments: dict[str, Any],
        files: dict[str, bytes],
        state: str | None,
    ) -> Any:
        answer = self._ask(
            {
                "op": "query",
                "caller": _caller(caller),
                "now": now.isoformat(),
                "name": query,
                "arguments": arguments,
                "files": encoded(files),
                "state": state,
            }
        )
        return answer["result"]

    def view(
        self,
        caller: Caller,
        now: datetime,
        view: str,
        arguments: dict[str, Any],
        files: dict[str, bytes],
        state: str | None,
    ) -> dict[str, Any]:
        """The sandbox's screen, as it answered: the runtime checks it, and reads nothing else
        of it than its arguments and its tree."""
        answer = self._ask(
            {
                "op": "view",
                "caller": _caller(caller),
                "now": now.isoformat(),
                "name": view,
                "arguments": arguments,
                "files": encoded(files),
                "state": state,
            }
        )
        return answer["result"]

    def check(self, now: datetime, files: dict[str, bytes]) -> Checked:
        answer = self._ask({"op": "check", "now": now.isoformat(), "files": encoded(files)})
        result = answer["result"]
        refusal = result.get("refusal")
        views = [Problem(one["where"], one["message"]) for one in result.get("views", [])]
        return Checked(None if refusal is None else _refusal(refusal), views)

    def _ask(self, request: dict[str, Any]) -> dict[str, Any]:
        with contextlib.nullcontext() if self._used is None else self._used():
            try:
                answer = self._client.run(self._login, self._app, self._version, request)
            except UnknownVersion:
                self._client.put_version(self._login, self._app, self._version, self._code())
                answer = self._client.run(self._login, self._app, self._version, request)
        error = answer.get("error")
        if error is not None:
            raise _exception(error, request)
        return answer


def _exception(error: dict[str, Any], request: dict[str, Any]) -> Exception:
    kind = error.get("kind")
    # Its head: an error of the app's code starts with the exception's type.
    message = shortened(str(error.get("message", "")), MAX_ERROR)
    if kind == "invalid_arguments":
        # The worker raises it only for arguments that don't fit the schema.
        return WrongArguments(message)
    if kind == "refused":
        return Refused(message)
    if kind == "denied":
        return Denied(message)
    if kind == "unknown":
        # Built from what the server asked for, never from the sandbox's text: every caller
        # reads it.
        named = NAMED_OPS.get(request.get("op", ""))
        if named is None:
            return UnknownName("the sandbox doesn't know this request")
        return UnknownName(f"there is no {named[0]} named {request.get('name')}")
    if kind == "app_error":
        return AppError(_problems(error, message))
    # The exception's type is its own field, which Cliquet set: a caller who isn't the owner
    # is told it, so it is never read from the text, which holds what the app's code wrote.
    failed = AppFailed(message, type_name(error.get("type")))
    trace = error.get("traceback")
    if isinstance(trace, str) and trace.strip():
        # Where the app's code raised, as the sandbox says it: the app's text, like the
        # message. A note is part of the error's traceback, never of its message.
        failed.add_note(f"In the sandbox:\n{trace[-MAX_TRACEBACK:].rstrip()}")
    return failed


def _problems(error: dict[str, Any], message: str) -> list[Problem]:
    """An app's problems, each where the worker says it is: in `app.py` when it doesn't."""
    sent = error.get("problems")
    if isinstance(sent, list) and sent:
        return [
            Problem(str(one.get("where", "app.py")), str(one.get("message", "")))
            for one in sent
            if isinstance(one, dict)
        ]
    return [Problem("app.py", line.removeprefix("- ")) for line in message.splitlines()]


def _change(
    answer: dict[str, Any],
    declared: Collection[str],
    removable: Collection[str] = (),
    *,
    allow_result: bool = False,
    what: str = "this change",
) -> Changes | Refusal:
    """The changes the sandbox answered, each to a declared file, or a removed one.

    Only an action's answer (`allow_result`) may carry a result; for any other, `what` says
    what the sandbox answered a result for.
    """
    if "refusal" in answer:
        return _refusal(answer["refusal"])
    result = answer.get("result")
    if result is not None:
        if not allow_result:
            raise AppFailed(f"the sandbox answered a result for {what}, which has none")
        if json_length(result) > MAX_RESULT:
            raise AppFailed(f"the sandbox answered a result of more than {size_text(MAX_RESULT)}")
    changes: dict[str, bytes | None] = {}
    for name, content in answer["changes"].items():
        allowed = declares(declared, name) or (content is None and name in removable)
        if data_name_problem(name) is not None or not allowed:
            raise AppFailed(
                f"the sandbox answered a change to {name}, which the app doesn't declare"
            )
        if content is None:
            changes[name] = None
        elif isinstance(content, str) and len(content) * 3 // 4 > MAX_DATA:
            # Cliquet's own sentence about a size it measured: every caller reads it.
            return Refusal(message=too_large(name, len(content)))
        else:
            changes[name] = decoded(content, MAX_DATA, name, AppFailed)
    return Changes(changes, result)


def too_large(name: str, encoded: int) -> str:
    """The sentence that refuses a change to a file no single change can write back."""
    return (
        f"data/{name} is {amount_text(encoded * 3 // 4)}, and a file may be at most "
        f"{size_text(MAX_DATA)}: bring it under that in one change, or remove it."
    )


def _refusal(data: dict[str, Any]) -> Refusal:
    return Refusal(
        message=data.get("message"),
        invariants=data.get("invariants") or {},
        descriptions=data.get("descriptions") or {},
    )


def _caller(caller: Caller) -> dict[str, Any]:
    return {"id": caller.id, "role": caller.role, "token_id": caller.token_id}
