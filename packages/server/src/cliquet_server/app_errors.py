"""The errors of a published app's code, kept for its owner: who keeps a call's, and how the
console and the builder show one. `Apps` keeps a failed start's.

Only the owner reads an error's text, which may hold the app's data. The database bounds
what is kept: see `Database.keep_error`.
"""

import logging
from typing import Any

from cliquet.context import Caller
from cliquet.errors import shortened
from cliquet.mcp_server import KeepError
from cliquet_server.apps import Apps
from cliquet_server.db import ERRORS_KEPT, ERRORS_KEPT_FOR, ErrorInfo

_log = logging.getLogger(__name__)


def keeper(apps: Apps, login: str, name: str) -> KeepError:
    """What keeps the errors of the code of `login`'s published app `name` for its owner, as
    the app's servers are given it. It runs in the call's thread, never on the event loop."""

    def keep(caller: Caller, tool: str, error_id: str, text: str) -> str | None:
        try:
            kept = apps.database.keep_error(
                login, name, error_id, apps.now(), caller.id, caller.role, tool, text
            )
        except Exception:
            # The call still answers: the log has the error under the same id.
            _log.exception("unexpected: %s/%s: the error %s wasn't kept", login, name, error_id)
            return None
        return where_kept(error_id) if kept else None

    return keep


def where_kept(error_id: str) -> str:
    """What tells a caller who isn't the app's owner where the owner reads the error kept
    under `error_id`."""
    return f"Error {error_id} has the details: the app's owner can read it in the app's console."


def _call(error: ErrorInfo) -> dict[str, Any]:
    """What failed: the error's id, when, and who called which tool with which role, or the
    app's start, which had no caller."""
    if error.start:
        return {"id": error.id, "when": error.at.isoformat(), "during": "start"}
    return {
        "id": error.id,
        "when": error.at.isoformat(),
        "during": "call",
        "caller": error.caller,
        "role": error.role,
        "tool": error.tool,
    }


def summary(error: ErrorInfo) -> dict[str, Any]:
    """An error as a list shows it: which call failed, and the start of its text's first
    line, which says what failed. Without its traceback."""
    line, _, rest = error.text.partition("\n")
    if line.endswith(":"):
        # A heading, as for a result that doesn't fit: the first problem follows it.
        line = f"{line} {rest.lstrip().partition('\n')[0].removeprefix('- ')}"
    return {**_call(error), "line": shortened(line, 300)}


def record(error: ErrorInfo) -> dict[str, Any]:
    """An error's whole record, as the builder gives it to the owner's agent."""
    return {**_call(error), "text": error.text}


def described(apps: Apps, login: str, name: str, latest: int = 5) -> dict[str, Any]:
    """What `read_app_overview` says of the app's errors: how many are kept, and the latest ones
    without their traceback, each with the id that `read_error` takes."""
    now = apps.now()
    return {
        "count": apps.database.error_count(login, name, now),
        "latest": [summary(error) for error in apps.database.errors(login, name, now, latest)],
        "note": f"The calls the app's code failed, and the app's starts that failed, in the last "
        f"{ERRORS_KEPT_FOR.days} days: the last {ERRORS_KEPT} at most. read_error(app, error) "
        "gives one with its traceback.",
    }
