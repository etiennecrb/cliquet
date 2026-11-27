"""An app's history in the console, for its owner only: which changes it lists, how each reads,
and what the page of one change shows, its diff included.

The History tab, the overview's last change and the change page all read it here, so they
list the same changes: every commit of the live history that changed the data. That holds a
publish whose migration changed the data, and the first publish, which brought it. A publish
that changed no data isn't one: the history of the data never holds it.
"""

from typing import Any

from cliquet.context import Caller
from cliquet.store import Entry, GitStore
from cliquet_server.apps import Live
from cliquet_server.db import PersonInfo
from cliquet_server.diffs import COMMIT, MAX_LINES, change_entry, csv_table, read_diff
from cliquet_server.repos import AppRepo, is_publish, remark_of

from .common import readable_size, readable_time

# How a publish reads, as a title and a detail. The first is the repository's first commit.
# Once an app is live, a publish changes the data only through its migration, so any later one
# changed it that way.
_FIRST = ("Published the first version", "It brought the data.")
_NEW = ("Published a new version", "Its migration changed the data.")
# A restore changes the data only when it put back the data from before a migration.
_RESTORED = (
    "Restored an earlier version",
    "It put back the data from before the publishes it undid.",
)


def latest_changes(live: Live, owner: PersonInfo, limit: int) -> list[dict[str, Any]]:
    """The app's latest changes, newest first, as the console lists them to its owner, who sees
    every change."""
    roles = list(live.runtime.manifest["roles"])
    caller = Caller(owner.login, roles[0] if roles else None)
    store = live.runtime.store
    return [_row(store, entry) for entry in live.runtime.history(caller, limit=limit)]


def change_page(live: Live, commit: str) -> dict[str, Any] | None:
    """What the page of one change shows, or None when the app's history lists no such change."""
    store = live.runtime.store
    entry = change_entry(store, commit)
    if entry is None:
        return None
    files = read_diff(store, commit)
    for file in files:
        file.table = csv_table(store, file)
    undone = entry.reverts if entry.reverts and COMMIT.fullmatch(entry.reverts) else ""
    return {
        "entry": {**_row(store, entry), "reverts": undone},
        "files": files,
        "left_out": sum(file.left_out for file in files),
        "max_lines": MAX_LINES,
        "size": readable_size,
    }


def _row(store: GitStore, entry: Entry) -> dict[str, Any]:
    """One change, as the console shows it. A publish is told by its trailer, never by its
    label, so an app's own action named `publish` reads as any other action. Its note is its
    remark."""
    publish = None
    if is_publish(entry):
        if entry.restores is not None:
            title, detail = _RESTORED
        else:
            title, detail = _FIRST if store.parent(entry.commit) is None else _NEW
        publish = {"title": title, "detail": detail}
    return {
        "commit": entry.commit,
        "when": readable_time(entry.time),
        "caller": entry.caller or "",
        "role": entry.role,
        "agent": entry.agent or "",
        "action": entry.action or "",
        "publish": publish,
        "remark": remark_of(entry) or "",
        "files": ", ".join(entry.files),
    }


def versions(repo: AppRepo, limit: int) -> list[dict[str, Any]]:
    """The app's latest publishes, newest first, as the History tab lists them: the first is
    the live version, and each other one may be restored."""
    rows = []
    for number, one in enumerate(repo.publishes(limit=limit)):
        rows.append(
            {
                "commit": one.commit,
                "short": one.commit[:7],
                "when": readable_time(one.time),
                "caller": one.login,
                "note": one.note,
                "version": one.version,
                "restores": one.restores[:7] if one.restores else "",
                "live": number == 0,
            }
        )
    return rows
