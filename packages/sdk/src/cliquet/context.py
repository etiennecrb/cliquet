"""What app functions receive as `ctx`: who is calling, and the time."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Caller:
    """Who is calling: a person or an agent (`id`), and the role it acts with.

    `role` is None when the app declares no roles. `token_id` names the API token behind the
    call, the same in every app it reaches, and new when a token is made again. It is not the
    token's secret. It is None on the web, with a sign-in, and for what Cliquet does itself.
    """

    id: str
    role: str | None
    token_id: str | None = None


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("ctx.now must be a timezone-aware datetime")


@dataclass(frozen=True)
class ActionContext:
    """Given to an action. It runs in a working copy of the app's files."""

    caller: Caller
    now: datetime

    def __post_init__(self) -> None:
        _require_aware(self.now)


@dataclass(frozen=True)
class QueryContext:
    """Given to a query. It runs on the current state, read-only."""

    caller: Caller
    now: datetime

    def __post_init__(self) -> None:
        _require_aware(self.now)


@dataclass(frozen=True)
class InvariantContext:
    """Given to an invariant. It runs on the state after a change.

    `before` is a folder holding the app's files as they were before the change.
    `action` names the action that made the change. What Cliquet does itself has a name no
    action can have: `app_revert` for an undo, `app_publish` for a new version's migration,
    `app_check` for a check of the data that changes nothing, where `before` is the data
    itself, and `app_change` for `world.change` in a test.
    """

    caller: Caller
    before: Path
    action: str
