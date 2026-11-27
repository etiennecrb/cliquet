"""The runtime: the write path, queries and the history tools for one app.

Every write is a transaction. The change runs on a working copy of the data, every
invariant runs on the result, then the runtime commits it or refuses it. The app's code
runs in an engine: the SDK's `Worker` in this process, or the server's engine
in a sandbox. The runtime relies on the manifest, and otherwise only calls the engine.
"""

import hashlib
import json
import re
import threading
from collections import Counter
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from cliquet import transactions as tx
from cliquet.context import Caller
from cliquet.errors import AppError, AppFailed, Denied, Problem, Refused, SafeError
from cliquet.inputs import Claim, Input
from cliquet.limits import (
    MAX_FILE_ARGUMENT,
    MAX_QUERY_ANSWER,
    data_problem,
    file_too_big,
    size_text,
)
from cliquet.screens import finish
from cliquet.store import (
    ACTION,
    AGENT,
    CALLER,
    CODE,
    FULL_ID,
    NULL_ID,
    REMARK,
    REQUEST,
    REVERTS,
    ROLE,
    TRANSACTION,
    UNDO,
    Entry,
    GitStore,
    Moved,
    StoreError,
    trailer_value,
)
from cliquet.tools import CHANGE, DATA_VERSION, MAX_REMARK, REQUEST_ID, REVERT, UNCHANGED, access
from cliquet.tools import REMARK as REMARK_ARGUMENT
from cliquet.views import in_transaction
from cliquet.worker import (
    Changes,
    Committed,
    Engine,
    InvalidArguments,
    Outcome,
    Refusal,
    Worker,
    json_length,
    undo_files,
)

# How many times a write is tried when someone else commits at the same moment.
_ATTEMPTS = 3
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:/+=-]{1,200}")
# What an action called with the version of data that changed since is refused with: Cliquet's
# fixed words, for every caller. It says nothing of what changed.
CHANGED = (
    "The data changed since you read it, so the call wasn't made. Read it again, decide "
    f"again, and call with the new {DATA_VERSION}."
)


class OldData(SafeError):
    """A query can't run on the data of an earlier time, such as before a migration."""


class Everyone(Enum):
    """The owner of a runtime where every caller is the owner: pass `EVERYONE` as `owner`."""

    EVERYONE = "everyone"


# For a runtime with one person behind it, who owns the app: `cliquet serve`, a preview and
# the test harness. Never for a live app, where the owner's rights would be every member's.
EVERYONE = Everyone.EVERYONE


class TooLarge(Exception):
    """An answer for a caller is past `MAX_QUERY_ANSWER`: that of `query`, or, with `changes`,
    what changed in it. `size` is its length as JSON, which only the app's owner is told."""

    def __init__(self, query: str, size: int, *, changes: bool = False) -> None:
        self.query, self.size, self.changes = query, size, changes
        super().__init__(self.caller_text())

    def caller_text(self) -> str:
        """Cliquet's fixed words, for every caller."""
        what = "What changed in the answer" if self.changes else "The answer"
        later = " or a later time or version" if self.changes else ""
        return (
            f"{what} of {self.query} is more than the {size_text(MAX_QUERY_ANSWER)} of JSON a "
            f"query may answer, so it wasn't sent. Ask again with narrower arguments{later}."
        )

    def owner_text(self) -> str:
        """The fixed words, the answer's size, and how the app's owner can let callers ask
        for less."""
        return (
            f"{self.caller_text()} It was {self.size:,} bytes. If {self.query} reads data "
            "that grows, give it an argument that narrows it, such as a period or a limit."
        )


class Runtime:
    """Serve one app: its folder holds `app.py` and a `data/` folder inside a git repository."""

    def __init__(
        self,
        app: str | Path,
        *,
        data_dir: str | Path | None = None,
        module_name: str = "app",
        clock: Callable[[], datetime] | None = None,
        engine: Engine | None = None,
        owner: str | Everyone | None = None,
    ) -> None:
        """Serve the app in `app`, a folder holding `app.py` or the module file itself.

        Its data is in `data_dir`, by default the `data` folder next to `app.py`. The app's
        code runs in `engine`, by default a `Worker` in this process. `owner` is the caller
        id of the app's owner, who sees every change and every error in full. With none,
        nobody is the owner. `EVERYONE` makes every caller the owner, as `cliquet serve`, a
        preview and the test harness ask for.
        """
        path = Path(app).resolve()
        app_py = path / "app.py" if path.is_dir() else path
        self.app_file = app_py
        self.store = GitStore(Path(data_dir) if data_dir is not None else app_py.parent / "data")
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._write_lock = threading.Lock()
        self.owner = owner
        self._serve(engine if engine is not None else Worker(app_py, module_name))
        # A transaction begun with another version of app.py ends, as at a publish: so
        # does one of `cliquet serve` started again with new code.
        if self.transactions.stale():
            with self._locked() as now:
                self.transactions.end_new_version(now)

    def _serve(self, engine: Engine) -> None:
        """Take the version of the app that `engine` runs: its manifest, and `app.py` as it
        is now."""
        self.engine: Engine = engine
        self.manifest = engine.manifest
        # The git blob id of app.py: which rules checked each commit.
        self._app_blob = _blob_id(self.app_file.read_bytes())
        self._files = tuple(self.manifest["files"])
        self._actions = {m["name"]: m for m in self.manifest["actions"]}
        self._queries = {m["name"]: m for m in self.manifest["queries"]}
        self._views = {m["name"]: m for m in self.manifest["views"]}
        self.transactions = tx.Transactions(self.store, self._app_blob)

    def new_version(self, engine: Engine) -> None:
        """Serve the version of the app that `engine` runs, on the same data, as the builder's
        preview does when the draft's code changes. Every open transaction ends, as at a
        publish.

        It takes the write lock, so a write that holds it finishes on the old version. One
        waiting for it was checked against the old manifest, and runs on the new engine. A
        query takes no lock: one running meanwhile may mix the two versions, and fail, which
        a preview, where every caller is the owner, can accept. So the old engine isn't
        closed.
        """
        with self._locked() as now:
            self._serve(engine)
            self.transactions.end_new_version(now, every=True)

    def is_owner(self, caller: Caller) -> bool:
        """Whether the caller is the app's owner: the person whose app it is, never a role."""
        return self.owner is EVERYONE or caller.id == self.owner

    def close(self) -> None:
        """Close the engine, which removes what it keeps."""
        close = getattr(self.engine, "close", None)
        if close is not None:
            close()

    # Calls

    def call(
        self,
        caller: Caller,
        action: str,
        arguments: dict[str, Any],
        request_id: str | None = None,
        transaction: str | None = None,
        remark: str | None = None,
        if_unchanged_since: str | None = None,
        agent: str | None = None,
        inputs: Mapping[str, Claim] | None = None,
    ) -> Outcome:
        """Run an action as `caller`, then commit the change or refuse it.

        With a `request_id`, a call already committed under that id returns its commit
        again without running, so a client can retry safely after a timeout. That answer
        is `replayed` and has no result: results aren't stored. With a `transaction`, the
        action runs on that transaction's data, and commits on its branch. A `remark` is
        kept in the commit, for the history, and so is `agent`, the name of the API token
        that made the call: the app's code never sees either. With
        `if_unchanged_since`, the version a query's answer gave (see `query_version`), the
        action runs only on that version of the data: on any other, the call is refused
        with `CHANGED`, without running. A call replayed by its `request_id` isn't checked.

        `inputs` holds what the call's file argument claims, by the argument's name: it is
        claimed once the call is known to run, after those checks, and kept if the write runs
        again. A commit uses it up; a call that keeps nothing, or fails, gives it back.
        """
        spec = self._actions.get(action)
        if spec is None:
            raise InvalidArguments(f"there is no action {action!r}")
        self._authorize(caller, spec["roles"], f"the action {action!r}")
        claims = _Claims(inputs or {})

        def run(files: dict[str, bytes], now: datetime) -> Changes | Refusal:
            given = claims.take()
            return self.engine.run_action(
                caller, now, action, arguments, files, inputs=given or None
            )

        outcome: Outcome | None = None
        try:
            outcome = self._write(
                caller,
                action,
                run,
                request_id=request_id,
                transaction=transaction,
                arguments=arguments,
                remark=remark,
                unchanged_since=if_unchanged_since,
                agent=agent,
            )
        finally:
            claims.settle(outcome)
        return outcome

    def query_json(
        self,
        caller: Caller,
        query: str,
        arguments: dict[str, Any],
        transaction: str | None = None,
    ) -> Any:
        """Run a query as `caller` on the current data, or on a transaction's, read-only, and
        return its result as JSON data, by alias, as its output schema describes it. Raises
        `TooLarge` past `MAX_QUERY_ANSWER`."""
        return self.query_version(caller, query, arguments, transaction)[0]

    def query_version(
        self,
        caller: Caller,
        query: str,
        arguments: dict[str, Any],
        transaction: str | None = None,
    ) -> tuple[Any, str]:
        """Run a query as `query_json` does, and return its result with the version of the
        data it read, which `call` takes as `if_unchanged_since`: the id of the commit it read,
        the live one or the transaction's (see `data_version`)."""
        self._check_query(caller, query)
        state, files = self._data(caller, transaction)
        data = self.engine.query_json(caller, self._now(), query, arguments, files, state)
        return _bounded(query, data), data_version(state)

    def version(self, caller: Caller, transaction: str | None = None) -> str:
        """The version of the data a read as `caller` sees now, live or in a `transaction`:
        what a query's answer gives as `app_data_version`."""
        return data_version(self._state(caller, transaction))

    def _check_query(self, caller: Caller, query: str) -> None:
        """Raise `InvalidArguments` if the app has no such query, and `Denied` if the caller's
        role may not use it."""
        spec = self._queries.get(query)
        if spec is None:
            raise InvalidArguments(f"there is no query {query!r}")
        self._authorize(caller, spec["roles"], f"the query {query!r}")

    def view(
        self,
        caller: Caller,
        view: str,
        arguments: dict[str, Any] | None = None,
        transaction: str | None = None,
    ) -> dict[str, Any]:
        """Lay out a view as `caller` on the current data, with its `arguments`, and return
        the screen.

        The engine's answer is checked here, wherever the app's code ran: raises `AppError` if
        the screen breaks a rule, such as a form that calls a tool the caller may not use.
        With a `transaction`, the view reads its data, and every call the screen makes carries
        its id, so it stays in the transaction.
        """
        spec = self._views.get(view)
        if spec is None:
            raise InvalidArguments(f"there is no view {view!r}")
        self._authorize(caller, spec["roles"], f"the view {view!r}")
        state, files = self._data(caller, transaction)
        raw = self.engine.view(caller, self._now(), view, dict(arguments or {}), files, state)
        screen = finish(raw, self.manifest, caller.role, view)
        if transaction is None:
            return screen
        return in_transaction(screen, transaction)

    def _data(self, caller: Caller, transaction: str | None) -> tuple[str | None, dict[str, bytes]]:
        """The commit a read sees, and the data files there: HEAD's, or the transaction's."""
        state = self._state(caller, transaction)
        return state, self.store.read_declared(state, self._files)

    def _state(self, caller: Caller, transaction: str | None) -> str | None:
        """The commit a read sees: HEAD's, or the tip of the caller's open transaction. None
        when there is no data yet."""
        if transaction is None:
            return self.store.head()
        return self.transactions.read_tip(caller.id, transaction, self._now())

    # Transactions

    def begin(self, caller: Caller) -> str:
        """Begin a transaction from the current data, and return its id.

        Raises `InvalidArguments` if the caller already has one open in this app.
        """
        return self.begin_record(caller).id

    def begin_record(self, caller: Caller) -> tx.Record:
        """Begin a transaction as `begin` does, and return its record: its id, and `base`,
        the version of the data to ask `changes_since` what it changes since."""
        self._authorize(caller, self._writer_roles(), "transactions")
        with self._locked() as now:
            return self.transactions.begin(caller.id, now)

    def commit_transaction(self, caller: Caller, transaction: str) -> Outcome:
        """Keep a transaction's commits, if nobody changed the data since it began.

        Otherwise it ends, nothing is kept, and the refusal lists its calls. A committed
        one lists every commit it kept. A transaction already ended answers as when it
        ended, `replayed`.
        """
        self._authorize(caller, self._writer_roles(), "transactions")
        with self._locked() as now:
            return self.transactions.commit(caller.id, transaction, now)

    def rollback(self, caller: Caller, transaction: str) -> None:
        """Drop a transaction: nothing it did is kept."""
        self._authorize(caller, self._writer_roles(), "transactions")
        with self._locked() as now:
            self.transactions.rollback(caller.id, transaction, now)

    def transaction_calls(self, caller: Caller, transaction: str) -> dict[str, Any]:
        """The calls of the caller's open transaction, oldest first: each one's action, its
        commit, its arguments (None when they were too long to keep), and the files it changed
        with how many lines it added and removed. Never a line of the data.

        It takes no lock and writes nothing. Anyone else's transaction is `NoSuchTransaction`,
        and one that ended `TransactionEnded`.
        """
        self._authorize(caller, self._writer_roles(), "transactions")
        record = self.transactions.calls(caller.id, transaction, self._now())
        counts = self.store.counts([call["commit"] for call in record.calls])
        calls = []
        for call in record.calls:
            kept = call.get("arguments")
            changed = counts[call["commit"]]
            calls.append(
                {
                    "action": call["action"],
                    "commit": call["commit"],
                    "arguments": None if kept is None else json.loads(kept),
                    "files": [asdict(one) for one in changed],
                }
            )
        return {"transaction": record.id, "began": record.began, "calls": calls}

    def check_transaction(self, caller: Caller, transaction: str) -> None:
        """Check that `transaction` is the caller's and open, as a read in it does.

        For a call that reads nothing, such as a view shown to a client that can't draw it.
        """
        self._state(caller, transaction)

    def end_transactions(self, published: str | None = None) -> None:
        """End every open transaction, as a new version of the app does. The server calls it
        when it serves a version it has just published.

        With `published`, the commit that made the version live, it ends only those begun
        before it: the server calls it so each time it starts the app, in case the end at the
        publish failed.
        """
        with self._locked() as now:
            self.transactions.end_new_version(now, every=published is None, published=published)

    def check_views(self) -> list[Problem]:
        """Lay out every view on the current data, once for each role that may open it."""
        files = self.store.read_declared(self.store.head(), self._files)
        return self.engine.check(self._now(), files).views

    def change(self, caller: Caller, edit: Callable[[], None], label: str = CHANGE) -> Outcome:
        """Apply an edit made outside any action, through the invariants. For tests."""
        return self._write(
            caller,
            label,
            lambda files, now: self._worker().edit(caller, label, files, edit),
        )

    def check(self) -> Refusal | None:
        """Run every invariant on the current data. Returns the refusal, if any."""
        files = self.store.read_declared(self.store.head(), self._files)
        return self.engine.check(self._now(), files).refusal

    # History

    def history(
        self,
        caller: Caller,
        limit: int = 50,
        since: str | None = None,
        transaction: str | None = None,
    ) -> list[Entry]:
        """The caller's latest changes to the data, newest first: every change for the app's
        owner. With `since`, a time or a version, only those made after the data it names
        (see `_then`), as for `changes_since`. With a `transaction`, the history of its
        branch."""
        self._authorize(caller, None, "the history")
        head = self._state(caller, transaction)
        then = None if since is None else self._then(since, head)[0]
        if head is None:
            return []
        return self.store.log(since=then, limit=limit, caller=self._whose(caller), ref=head)

    def changes_since(
        self,
        caller: Caller,
        query: str,
        arguments: dict[str, Any],
        since: str,
        transaction: str | None = None,
    ) -> dict[str, Any]:
        """What changed in a query's answer since a time or a version of the data: in the live
        data, or in a `transaction`'s, so that from the version it began from it is what the
        transaction changes.

        `since` names the data then (see `_then`): no data before the first commit. The
        query runs on the data then and now, with the app's current code and the same
        `ctx.now`. For a list,
        `added` holds the items now that weren't there then, and `removed` the items then
        that aren't there now, compared as JSON values; for any other answer, the new value
        and the old one, when they differ; a list and None compare as a list and an empty
        one. Raises `OldData` if the app's code fails (`AppFailed`) or refuses on the data
        then, with its error as the cause, and `TooLarge` if what changed is past
        `MAX_QUERY_ANSWER`: only that is sent, so either answer may be larger. The answer
        names the commits it compared, `then` and `now`, for every caller, as a query's
        version does.
        """
        self._authorize(caller, None, "the history")
        if not isinstance(query, str):
            raise InvalidArguments("query must be the name of one of your queries")
        if not isinstance(arguments, dict):
            raise InvalidArguments(f"arguments must be an object: the arguments of {query}")
        yours = list(access(self.manifest, caller.role).queries)
        if self.is_owner(caller) or not self.manifest["roles"]:
            self._check_query(caller, query)
        elif query not in yours:
            # The same answer for a query the app lacks and one the role may not use, so a
            # member isn't told which queries the app has.
            raise Denied(f"{query} is not one of your queries: {', '.join(yours) or 'none'}")
        now = self._now()
        head = self._state(caller, transaction)
        then, shown = self._then(since, head)
        current = self.store.read_declared(head, self._files)
        old = current if then == head else self.store.read_declared(then, self._files)
        added: list[Any] = []
        removed: list[Any] = []
        if old != current:
            # The old data first, so a failure there throws no work away. No state: the
            # worker keeps one snapshot of the data, which stays the current one.
            try:
                before = self.engine.query_json(caller, now, query, arguments, old, None)
            except (AppFailed, AppError, Refused) as error:
                told = f"{query} can't run on the data of {shown}."
                raise OldData(f"{told} Ask about a later time or version.") from error
            answer = self.engine.query_json(caller, now, query, arguments, current, head)
            added, removed = _difference(before, answer)
        else:
            # The same data: the same answer. Run once, so bad arguments are still told.
            self.engine.query_json(caller, now, query, arguments, current, head)
        changes = {
            "query": query,
            "since": shown,
            "then": then,
            "now": head,
            "added": added,
            "removed": removed,
        }
        return _bounded(query, changes, changes=True)

    def _then(self, since: object, head: str | None) -> tuple[str | None, str]:
        """The commit that holds the data `since` names, in `head`'s history, None for no
        data yet, and `since` as an answer says it: the one rule for what `since` means.

        A version of the data, as a query's `app_data_version` or the `now` of an earlier
        `changes_since`, names its own commit, which must be in `head`'s history. A time names
        the last commit before the first one dated in its second or later, in the history's
        own order (`GitStore.at`). So nothing that reached the data at or after it is missed,
        and a change made earlier in that second, or after a commit dated later, as when the
        clock went back, is told again. A kept transaction reached the live data when it was
        kept, and in a transaction, its own commits count from their dates.
        """
        if isinstance(since, str) and FULL_ID.fullmatch(since):
            if since == NULL_ID:
                return None, since
            if head is not None and (since == head or self.store.is_ancestor(since, head)):
                return since, since
            raise InvalidArguments(
                f"since {since} is no version of this data. Pass a time, or the "
                f"{DATA_VERSION} of an answer from this app."
            )
        when = _parse_time(since)
        return (None if head is None else self.store.at(when, head)), when.isoformat()

    def revert(
        self,
        caller: Caller,
        commit: str,
        request_id: str | None = None,
        transaction: str | None = None,
        remark: str | None = None,
        agent: str | None = None,
    ) -> Outcome:
        """Undo one change, applied like any other change, through the invariants.

        The caller undoes their own changes, and the app's owner anyone's; either way only
        with a role that may call the change's action. With a `transaction`, the change is
        one of its branch's history, and the undo a call of the transaction.
        """
        self._authorize(caller, None, "the history")
        visible = self._visible(caller, commit, self._state(caller, transaction))
        entry = None if visible is None else visible[1]
        if entry is None:
            if self._whose(caller):
                raise Denied("You can only undo your own changes.")
            listed = "Call app_history for the commits you may undo."
            if visible is None:
                raise StoreError(f"{commit} is not in this app's history. {listed}")
            raise StoreError(f"{commit} didn't change this app's data. {listed}")
        spec = self._actions.get(entry.action or "")
        roles = spec["roles"] if spec else self._writer_roles()
        self._authorize(caller, roles, f"undoing {entry.action or 'this change'}")
        parent = self.store.parent(entry.commit)
        before = self.store.read_declared(parent, self._files)
        after = self.store.read_declared(entry.commit, self._files)
        trailers = {REVERTS: entry.commit}
        return self._write(
            caller,
            REVERT,
            lambda files, _now: self.engine.apply(
                caller, REVERT, files, undo_files(files, before, after, entry.commit)
            ),
            trailers,
            request_id,
            transaction,
            {"commit": entry.commit},
            remark,
            agent=agent,
        )

    def _whose(self, caller: Caller) -> str | None:
        """Whose changes the caller sees: their own, by their id as a commit's trailer
        holds it, or None, every one, for the owner."""
        return None if self.is_owner(caller) else trailer_value(caller.id)

    def _visible(
        self, caller: Caller, commit: str, head: str | None
    ) -> tuple[str, Entry | None] | None:
        """The full id of `commit` and its change to the data (None when it made none), if
        `head`'s history holds it and the caller may see it: any commit for the app's owner,
        one of their own changes for anyone else."""
        try:
            full = self.store.resolve(commit)
        except StoreError:
            return None
        if head is None or not self.store.is_ancestor(full, head):
            return None
        # Resolved once: `entry` reads the commit by its full id.
        entry = self.store.entry(full)
        whose = self._whose(caller)
        if whose is not None and (entry is None or entry.caller != whose):
            return None
        return full, entry

    # The write path

    def _write(
        self,
        caller: Caller,
        label: str,
        run: Callable[[dict[str, bytes], datetime], Changes | Refusal],
        trailers: dict[str, str] | None = None,
        request_id: str | None = None,
        transaction: str | None = None,
        arguments: dict[str, Any] | None = None,
        remark: str | None = None,
        unchanged_since: str | None = None,
        agent: str | None = None,
    ) -> Outcome:
        trailers = dict(trailers or {})
        if request_id is not None:
            if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
                raise InvalidArguments(
                    f"{REQUEST_ID} must be 1 to 200 letters, digits or . _ : / + = -"
                )
            trailers[REQUEST] = request_id
        kept = _remark(remark)
        if kept is not None:
            trailers[REMARK] = kept
        if agent:
            trailers[AGENT] = agent
        version = _version(unchanged_since)
        with self._locked() as now:
            if transaction is None:
                return self._attempts(
                    caller, label, now, run, trailers, request_id, "HEAD", version
                )
            record = self.transactions.open(caller.id, transaction, now)
            trailers[TRANSACTION] = record.id
            ref = tx.ref(record.id)
            outcome = self._attempts(caller, label, now, run, trailers, request_id, ref, version)
            self.transactions.called(record, outcome, label, arguments or {})
            return outcome

    def _attempts(
        self,
        caller: Caller,
        label: str,
        now: datetime,
        run: Callable[[dict[str, bytes], datetime], Changes | Refusal],
        trailers: dict[str, str],
        request_id: str | None,
        ref: str,
        version: str | None,
    ) -> Outcome:
        """Run a write on `ref`, again if someone committed meanwhile. Expects the locks.

        Each attempt reads `ref` once: the request id is looked for in its history, the
        change runs on its data, and with a `version`, only if that is its version. So a
        write that runs again is checked again, against the commit it would then build on.
        """
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                base = self.store.head(ref)
                if request_id is not None:
                    done = self._already_done(caller, label, request_id, base)
                    if done is not None:
                        return done
                if version is not None and version != data_version(base):
                    return Refusal(message=CHANGED)
                return self._attempt(caller, label, now, run, trailers, ref, base)
            except Moved:
                # Someone committed outside Cliquet meanwhile. Actions have no other
                # effect than their commit, so running it again on the new state is safe.
                if attempt == _ATTEMPTS:
                    raise
        raise AssertionError("unreachable")

    def _already_done(
        self, caller: Caller, label: str, request_id: str, base: str | None
    ) -> Committed | None:
        """The outcome of a call already committed under this request id, if any, in the
        history of `base`."""
        entry = self.store.find_request(request_id, base)
        if entry is None:
            return None
        own = entry.caller == trailer_value(caller.id)
        if entry.action != label or not own:
            # Who used it, and for what, is someone else's history.
            mine = own or self.is_owner(caller)
            how = f" by {entry.caller} for {entry.action} ({entry.commit[:7]})" if mine else ""
            raise InvalidArguments(
                f"{REQUEST_ID} {request_id!r} was already used{how}. Use a new id for a new call."
            )
        return Committed(entry.commit, entry.files, replayed=True)

    def _attempt(
        self,
        caller: Caller,
        label: str,
        now: datetime,
        run: Callable[[dict[str, bytes], datetime], Changes | Refusal],
        trailers: dict[str, str],
        ref: str,
        base: str | None,
    ) -> Outcome:
        """Run a write on the data of `base`, the commit `ref` points at, and commit it on
        `ref`. Raises `Moved` if `ref` moved meanwhile."""
        if ref == "HEAD":
            # It finishes a write cut off after its commit, which leaves HEAD where it is.
            self.store.check_clean()
        # A transaction's branch is in git only: the files on disk are HEAD's.
        files = self.store.read_declared(base, self._files)
        try:
            result = run(files, now)
        except Refused as e:
            return Refusal(message=e.message)
        if isinstance(result, Refusal):
            return result
        if not result.files:
            return Committed(None, [], result.result)
        refused = _too_much_data(files, result.files)
        if refused is not None:
            return refused
        # An undo is stored as `revert`, and its trailer makes it read as `REVERT` again.
        stored = UNDO if REVERTS in trailers else label
        message = _message(stored, caller, {CODE: self._app_blob, **trailers})
        commit = self.store.commit(base, result.files, message, caller.id, now, ref=ref)
        return Committed(commit, list(result.files), result.result)

    # Helpers

    @contextmanager
    def _locked(self) -> Generator[datetime]:
        """The app's write lock, in this process and across processes, and the time once it is
        held: a write is dated when it lands, since dated while it waited, it could land after
        a check made since that date."""
        with self._write_lock, self.store.write_lock():
            yield self._now()

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("the runtime's clock must return a timezone-aware datetime")
        return now

    def _authorize(self, caller: Caller, roles: list[str] | None, what: str) -> None:
        app_roles = self.manifest["roles"]
        if not app_roles:
            return
        if caller.role not in app_roles:
            raise Denied(f"{caller.role!r} is not a role of this app ({', '.join(app_roles)})")
        if roles is not None and caller.role not in roles:
            raise Denied(f"the role {caller.role!r} may not use {what}")

    def _writer_roles(self) -> list[str]:
        actions = self._actions.values()
        return [r for r in self.manifest["roles"] if any(r in a["roles"] for a in actions)]

    def _worker(self) -> Worker:
        if not isinstance(self.engine, Worker):
            raise TypeError("this needs the app's code in this process")
        return self.engine


class _Claims:
    """The files one call claims, each once, the first time the call runs: a write that runs
    again gets the same."""

    def __init__(self, inputs: Mapping[str, Claim]) -> None:
        self._inputs = dict(inputs)
        self._claimed: dict[str, Claim] = {}
        self._given: dict[str, Input] | None = None

    def take(self) -> dict[str, Input]:
        """The files, claimed now if they weren't yet. Raises `Refused` for one that can't be
        had, or that is past `MAX_FILE_ARGUMENT`."""
        if self._given is None:
            given = {}
            for argument, claim in self._inputs.items():
                given[argument] = claim.claim()
                self._claimed[argument] = claim
                if len(given[argument].content) > MAX_FILE_ARGUMENT:
                    raise Refused(file_too_big("changed"))
            self._given = given
        return self._given

    def settle(self, outcome: Outcome | None) -> None:
        """Use the files up after a commit, and give them back after anything else."""
        used = isinstance(outcome, Committed) and outcome.commit is not None
        for claim in self._claimed.values():
            if used:
                claim.use()
            else:
                claim.release()


def _too_much_data(files: dict[str, bytes], changes: dict[str, bytes | None]) -> Refusal | None:
    """The refusal of `changes` to the app's data `files`, when they would make it larger than
    an app keeps, and larger than it is.

    Only the files the app's declaration matches count. A migration also gets any other file
    left in the repository, but publish refuses a version that leaves one behind.
    """
    before = sum(len(content) for content in files.values())
    after = before + sum(
        (0 if content is None else len(content)) - len(files.get(name, b""))
        for name, content in changes.items()
    )
    sentence = data_problem("This change", before, after)
    if sentence is None:
        return None
    return Refusal(message=f"{sentence} Remove data before adding more.")


def _parse_time(since: object) -> datetime:
    """A caller's time: ISO 8601 with its zone. Raises `InvalidArguments` otherwise, saying
    that a version of the data does too.

    It is parsed here, so a caller's text never reaches git.
    """
    when = None
    if isinstance(since, str):
        with suppress(ValueError):
            when = datetime.fromisoformat(since)
    if when is None:
        raise InvalidArguments(
            "since must be a time in ISO 8601 with its zone, such as 2026-11-01T00:00:00+01:00, "
            f"or the {DATA_VERSION} of an answer from this app"
        )
    if when.tzinfo is None:
        raise InvalidArguments(
            "since must be a time with its zone, such as 2026-11-01T00:00:00+01:00 or "
            "2026-11-01T00:00:00Z"
        )
    return when


def data_version(commit: str | None) -> str:
    """The version of the data at `commit`, as a caller is given it: the commit's full id,
    or git's `NULL_ID` before the first. A caller's version is compared with it here, and never
    given to git."""
    return commit or NULL_ID


def _version(given: object) -> str | None:
    """A caller's `app_if_unchanged_since`, or None when there is none: left out, or empty,
    as a client may send an argument it has no value for. Raises `InvalidArguments` unless it
    has the form of a version."""
    if given is None or given == "":
        return None
    if not isinstance(given, str) or not FULL_ID.fullmatch(given):
        raise InvalidArguments(
            f"{UNCHANGED} must be the {DATA_VERSION} of a query's answer, a commit's id of 40 "
            "lower-case hexadecimal characters: call the query again and pass the one it gives"
        )
    return given


def _remark(remark: object) -> str | None:
    """A caller's remark as its commit keeps it, or None when there is none. Raises
    `InvalidArguments` unless it is one line of at most `MAX_REMARK` characters."""
    if remark is None:
        return None
    if not isinstance(remark, str):
        raise InvalidArguments(f"{REMARK_ARGUMENT} must be a string: one line on why")
    text = remark.strip()
    if len(text) > MAX_REMARK or not text.isprintable():
        raise InvalidArguments(
            f"{REMARK_ARGUMENT} must be one line of at most {MAX_REMARK} characters: say why "
            "the change is made, and leave the data out"
        )
    return text or None


def _difference(before: Any, after: Any) -> tuple[list[Any], list[Any]]:
    """What was added and removed between two answers of a query, compared as JSON values.

    For two lists, each item counts once per time it appears, and None beside a list is
    an empty list; otherwise the new value and the old one, when they differ.
    """
    if isinstance(before, list) or isinstance(after, list):
        before, after = ([] if side is None else side for side in (before, after))
    if not (isinstance(before, list) and isinstance(after, list)):
        same = _json(before) == _json(after)
        return ([], []) if same else ([after], [before])
    old = [(_json(item), item) for item in before]
    new = [(_json(item), item) for item in after]
    return _missing(new, old), _missing(old, new)


def _missing(items: list[tuple[str, Any]], other: list[tuple[str, Any]]) -> list[Any]:
    """The items of `items` not in `other`, in their order, as many times as they're more.
    Each comes with its JSON."""
    left = Counter(key for key, _ in other)
    missing = []
    for key, item in items:
        if left[key]:
            left[key] -= 1
        else:
            missing.append(item)
    return missing


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _bounded[T](query: str, data: T, *, changes: bool = False) -> T:
    """`data`, an answer of `query` or what changed in it, unless its JSON is past
    `MAX_QUERY_ANSWER`: then raise `TooLarge`."""
    size = json_length(data)
    if size > MAX_QUERY_ANSWER:
        raise TooLarge(query, size, changes=changes)
    return data


def _blob_id(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(content) + content, usedforsecurity=False).hexdigest()


def _message(label: str, caller: Caller, trailers: dict[str, str]) -> str:
    lines = {ACTION: label, CALLER: caller.id}
    if caller.role is not None:
        lines[ROLE] = caller.role
    lines.update(trailers)
    body = "\n".join(f"{k}: {trailer_value(v)}" for k, v in lines.items())
    return f"{label}\n\n{body}\n"
