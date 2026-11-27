"""An app's transactions: their records, their branches, and what the runtime says of them.

A transaction is a branch of the app's data, `refs/app/transactions/<id>`, which holds
its calls' commits. Its record, `.git/app/transactions/<id>.json`, keeps what the branch
can't: whose it is, what it began from and with which version of the app, its calls with
their arguments, and how it ended. Only the runtime writes them, under its write lock. A
read writes nothing.
"""

import json
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeIs

from cliquet.store import (
    ACTION,
    CALLER,
    TRANSACTION,
    GitStore,
    Moved,
    StoreError,
    Tip,
    trailer_value,
    write_atomically,
)
from cliquet.tools import COMMIT_TRANSACTION, DATA_VERSION
from cliquet.worker import Committed, InvalidArguments, Outcome, Refusal

# A transaction is dropped this long after its last change.
IDLE = timedelta(hours=1)
# An ended transaction still answers its caller for this long.
KEPT = timedelta(days=1)
# How much of the calls' arguments an answer shows, after which it names the actions only.
SHOWN = 20_000

_ID = re.compile(r"t-[0-9a-f]{16}")
_BRANCHES = "refs/app/transactions/"

COMMITTED = "committed"
NOTHING = "nothing"
ROLLED_BACK = "rolled_back"
MOVED = "moved"
EXPIRED = "expired"
NEW_VERSION = "new_version"


class NoSuchTransaction(InvalidArguments):
    """The caller has no transaction of this id: anyone else's isn't theirs to know of."""


class TransactionEnded(InvalidArguments):
    """The transaction ended: the message says how, and nothing ran."""


def new_id() -> str:
    """A new transaction id, which nobody can guess: `t-` and 16 hexadecimal characters."""
    return f"t-{secrets.token_hex(8)}"


def is_id(transaction: object) -> TypeIs[str]:
    """Whether a caller's value has the shape of a transaction id, and so may name a ref."""
    return isinstance(transaction, str) and _ID.fullmatch(transaction) is not None


def ref(transaction: str) -> str:
    """The branch of a transaction."""
    return f"{_BRANCHES}{transaction}"


def shown_id(transaction: object) -> str:
    """The id as an answer may repeat it: as given when it looks like one, else cut short."""
    text = str(transaction)
    return text if _ID.fullmatch(text) else repr(text[:40])


@dataclass
class Record:
    """One transaction: whose it is, what it began from, its calls, and how it ended."""

    id: str
    caller: str
    base: str
    # The git blob id of the app.py it began with.
    app: str
    began: str
    # Each call that committed on the branch: its action, its commit, and its arguments while
    # they fit in what an answer shows.
    calls: list[dict[str, Any]] = field(default_factory=list)
    ended: str | None = None
    ended_at: str | None = None
    # The last commit a committed transaction kept: HEAD's history holds the others.
    commit: str | None = None

    def add_call(self, action: str, commit: str, arguments: dict[str, Any]) -> None:
        """Remember a call that committed on the branch. Its arguments are kept while they
        fit in what an answer shows: once one call's don't, no later call keeps its own."""
        text = json.dumps(arguments, ensure_ascii=False, default=str, sort_keys=True)
        call: dict[str, Any] = {"action": action, "commit": commit}
        fits = all("arguments" in one for one in self.calls)
        if fits and sum(len(one["arguments"]) for one in self.calls) + len(text) <= SHOWN:
            call["arguments"] = text
        self.calls.append(call)

    def end(self, how: str, now: datetime) -> None:
        """Mark the transaction ended, `how` and when, in memory: the caller saves it."""
        self.ended = how
        self.ended_at = now.isoformat()

    def ended_until(self) -> datetime:
        """When the record of an ended transaction is removed."""
        return datetime.fromisoformat(self.ended_at or self.began) + KEPT


class Records:
    """The transaction records of one repository, in `.git/app/transactions/`."""

    def __init__(self, git_dir: Path) -> None:
        self.folder = git_dir / "app" / "transactions"

    def load(self, transaction: object) -> Record | None:
        """The record of a transaction, or None when there is none or the id isn't one."""
        if not is_id(transaction):
            return None
        path = self.folder / f"{transaction}.json"
        if path.is_symlink() or not path.is_file():
            return None
        return Record(**json.loads(path.read_text()))

    def save(self, record: Record) -> None:
        """Write a record in one step, so a crash leaves the old one or the new one."""
        data = json.dumps(asdict(record), ensure_ascii=False).encode()
        write_atomically(self.folder / f"{record.id}.json", data)

    def remove(self, record: Record) -> None:
        """Delete a record, if it is still there."""
        (self.folder / f"{record.id}.json").unlink(missing_ok=True)

    def every(self) -> list[Record]:
        """Every record, open or ended."""
        if not self.folder.is_dir():
            return []
        found = (self.load(path.stem) for path in sorted(self.folder.glob("t-*.json")))
        return [record for record in found if record is not None]


def calls_text(record: Record) -> str:
    """The calls a transaction made, with their arguments while they fit, then by name."""
    if not record.calls:
        return "It made no calls."
    # The calls that kept their arguments come first: once one's didn't fit, none did.
    lines = [f"- {c['action']} {c['arguments']}" for c in record.calls if "arguments" in c]
    rest = [call["action"] for call in record.calls if "arguments" not in call]
    text = "It made these calls:\n" + "\n".join(lines)
    if rest:
        text += ("\n" if lines else "") + f"Then, without their arguments: {', '.join(rest)}."
    return text


MOVED_TEXT = (
    "The data changed since this transaction began, so nothing was kept. To keep these "
    "changes, begin a new transaction and make the calls again. If the person should see the "
    "result first, check it before you commit, or ask them."
)


# How a transaction that wasn't committed ended, as any later call with its id answers. A
# moved one adds its calls.
ENDED_TEXTS = {
    ROLLED_BACK: "Transaction {id} was rolled back.",
    MOVED: "Transaction {id} was not committed: the data changed since it began.",
    EXPIRED: "Transaction {id} was dropped after an hour without a change.",
    NEW_VERSION: "Transaction {id} ended when a new version of the app was published.",
}


def committed_text(
    transaction: str, commits: list[str], files: list[str], version: str | None
) -> str:
    """What a committed transaction answers, to `app_commit_transaction`, first or again, and
    to any later call with its id: every commit it kept, oldest first, so each can be undone,
    and the `version` of the data it left."""
    if not commits:
        return f"Transaction {transaction} had nothing to commit."
    changing = ", ".join(files)
    if len(commits) == 1:
        text = f"Transaction {transaction} was committed as {commits[0][:12]}, changing {changing}."
    else:
        ids = ", ".join(commit[:12] for commit in commits)
        text = (
            f"Transaction {transaction} was committed as {len(commits)} commits, one per call, "
            f"oldest first: {ids}, changing {changing}."
        )
    return text if version is None else f"{text} It left the data at {DATA_VERSION} {version}."


def open_text(record: Record, now: datetime, changed: datetime) -> str:
    """What a caller who already has an open transaction, last changed at `changed`, is told
    by `begin`, with times in the zone of `now`."""
    zone = now.tzinfo
    began = datetime.fromisoformat(record.began).astimezone(zone)
    last, dropped = changed.astimezone(zone), (changed + IDLE).astimezone(zone)
    count = len(record.calls)
    if count == 0:
        calls = "with no calls"
    else:
        names = ", ".join(call["action"] for call in record.calls)
        calls = f"with {count} call{'s' if count > 1 else ''}: {names}"
    return (
        f"You have a transaction open in this app, {record.id}, begun at {_clock(began)} and "
        f"last changed at {_clock(last)}, {calls}. If you began it, commit or roll it back. "
        "If you didn't, another of your agents may be using it: ask the person before you "
        f"touch it. It is dropped at {_clock(dropped)} unless a call changes it first."
    )


def _clock(moment: datetime) -> str:
    zone = moment.tzname()
    return f"{moment:%H:%M} {zone}" if zone else f"{moment:%H:%M}"


def _own(record: Record, tip: Tip | None) -> Tip | None:
    """The last commit of a transaction's own on its branch: None until its first."""
    return tip if tip is not None and tip.commit != record.base else None


def _missing(transaction: object) -> NoSuchTransaction:
    return NoSuchTransaction(f"There is no transaction {shown_id(transaction)}.")


class Transactions:
    """The transactions of one app: a branch and a record each, in the app's repository.

    `app` is the git blob id of the `app.py` serving them. The runtime holds its write lock
    around every method that writes, and gives each the time it took the lock. `read_tip`,
    `calls`, `any_open` and `stale` write nothing, and take no lock.
    """

    def __init__(self, store: GitStore, app: str) -> None:
        self.store = store
        self.app = app
        self.records = Records(store.git_dir)

    def begin(self, caller: str, now: datetime) -> Record:
        """Begin a transaction from the current data, and return its record.

        Raises `InvalidArguments` if the caller already has one open in this app.
        """
        for record in self._sweep(now):
            if record.caller == caller and record.ended is None:
                changed = self._changed(record, self._tip(record.id))
                raise InvalidArguments(open_text(record, now, changed))
        base = self.store.head()
        if base is None:
            raise StoreError(
                "the app has no data yet, so a transaction has nothing to begin from. "
                "Make the first change without one."
            )
        record = Record(new_id(), caller, base, self.app, now.isoformat())
        self.records.save(record)
        # A stop before the branch is made is put right on the next use (`_settle`).
        self.store.branch(ref(record.id), base)
        return record

    def commit(self, caller: str, transaction: object, now: datetime) -> Outcome:
        """Keep a transaction's commits, if nobody changed the data since it began.

        Otherwise it ends, nothing is kept, and the refusal lists its calls. One that ended
        already answers as when it ended, `replayed`.
        """
        record = self._find(caller, transaction, now)
        replayed = record.ended is not None
        if not replayed:
            tip = _own(record, self._tip(record.id))
            if tip is None:
                self._end(record, NOTHING, now)
            else:
                self.store.check_clean()
                # Kept as one commit of the live history, dated now: a check made since its
                # calls ran, before now, would otherwise never see them.
                message = (
                    f"Commit transaction {record.id}\n\n{ACTION}: {COMMIT_TRANSACTION}\n"
                    f"{CALLER}: {trailer_value(caller)}\n{TRANSACTION}: {record.id}\n"
                )
                merged = self.store.merge(record.base, tip.commit, message, caller, now)
                try:
                    self.store.advance(merged, record.base)
                except Moved:
                    self._end(record, MOVED, now)
                else:
                    # The commit that kept it: the version of the data it leaves.
                    record.commit = merged
                    self._end(record, COMMITTED, now)
        return self._outcome(record, replayed)

    def rollback(self, caller: str, transaction: object, now: datetime) -> None:
        """Drop a transaction: nothing it did is kept."""
        self._end(self.open(caller, transaction, now), ROLLED_BACK, now)

    def read_tip(self, caller: str, transaction: object, now: datetime) -> str:
        """The commit a read in the caller's open transaction sees.

        It takes no lock and writes nothing. The branch is read before the record, which an
        end marks before it drops the branch: a read that races with the end sees the
        branch's last commit or how it ended, never the live data.
        """
        record, tip = self._read(caller, transaction, now)
        return record.base if tip is None else tip.commit

    def calls(self, caller: str, transaction: object, now: datetime) -> Record:
        """The record of the caller's open transaction, to list its calls. It takes no lock
        and writes nothing, as `read_tip`."""
        return self._read(caller, transaction, now)[0]

    def _read(self, caller: str, transaction: object, now: datetime) -> tuple[Record, Tip | None]:
        """The caller's open transaction and its branch's tip, settled in memory only.

        The branch is read before the record, which an end marks before it drops the branch.
        One that ended raises `TransactionEnded`, and anyone else's `NoSuchTransaction`.
        """
        tip = self._tip(transaction)
        record = self._mine(caller, transaction)
        if not self._settled(record, now, tip):
            raise _missing(transaction)
        if record.ended is not None:
            raise TransactionEnded(self._ended_text(record))
        return record, tip

    def open(self, caller: str, transaction: object, now: datetime) -> Record:
        """The caller's open transaction.

        One that ended raises `TransactionEnded`, saying how, and nothing runs.
        """
        record = self._find(caller, transaction, now)
        if record.ended is not None:
            raise TransactionEnded(self._ended_text(record))
        return record

    def called(
        self, record: Record, outcome: Outcome, action: str, arguments: dict[str, Any]
    ) -> None:
        """Remember a call that committed on the transaction's branch, with its arguments.

        A replay counts when its commit is the transaction's own and the record doesn't list
        it: the first try committed, then stopped before the record said so.
        """
        if not isinstance(outcome, Committed) or outcome.commit is None:
            return
        if any(call.get("commit") == outcome.commit for call in record.calls):
            return
        if outcome.replayed:
            entry = self.store.entry(outcome.commit)
            if entry is None or entry.transaction != record.id:
                return
        record.add_call(action, outcome.commit, arguments)
        self.records.save(record)

    def any_open(self) -> bool:
        """Whether a transaction is open, by its record. It takes no lock."""
        return any(r.ended is None for r in self.records.every())

    def stale(self) -> bool:
        """Whether a transaction is open that began with another version of the app than
        this one. It takes no lock."""
        return any(r.ended is None and r.app != self.app for r in self.records.every())

    def end_new_version(
        self, now: datetime, *, every: bool = False, published: str | None = None
    ) -> None:
        """End the open transactions begun with another version of the app than this one, or
        `every` open one, as a new version of the app does. With `published`, the commit that
        made this version live, so do those begun from data it doesn't hold."""
        for record in self._sweep(now):
            if record.ended is None and (
                every
                or record.app != self.app
                or (published is not None and not self.store.is_ancestor(published, record.base))
            ):
                self._end(record, NEW_VERSION, now)

    def _mine(self, caller: str, transaction: object) -> Record:
        """The caller's record of the transaction. Anyone else's, or none, raises
        `NoSuchTransaction`."""
        record = self.records.load(transaction)
        if record is None or record.caller != caller:
            raise _missing(transaction)
        return record

    def _find(self, caller: str, transaction: object, now: datetime) -> Record:
        """The caller's transaction, its state settled (`_settle`)."""
        record = self._mine(caller, transaction)
        if not self._settle(record, now, self._tip(record.id)):
            raise _missing(transaction)
        return record

    def _tip(self, transaction: object) -> Tip | None:
        """Where a transaction's branch is: None when it has none, or the id isn't one."""
        if not is_id(transaction):
            return None
        name = ref(transaction)
        return self.store.tips(name).get(name)

    def _changed(self, record: Record, tip: Tip | None) -> datetime:
        """When a transaction last changed: its branch's last commit, dated to the second,
        or when it began."""
        began = datetime.fromisoformat(record.began)
        last = _own(record, tip)
        return began if last is None else max(began, last.time)

    def _settled(self, record: Record, now: datetime, tip: Tip | None) -> bool:
        """Settle a transaction's record from its branch, in memory, and say whether it is
        still known.

        An open one whose commits HEAD holds (an advance whose record wasn't marked) is
        committed, and one an hour after its last change expired. An ended one is forgotten
        a day after it ended.
        """
        if record.ended is None:
            last = _own(record, tip)
            if last is not None and self.store.is_ancestor(last.commit, "HEAD"):
                # The merge that kept it, or its last call when it was kept before there was
                # one, by a fast-forward.
                record.commit = self.store.kept_by(record.base, last.commit) or last.commit
                record.end(COMMITTED, now)
            elif self._changed(record, tip) + IDLE <= now:
                record.end(EXPIRED, now)
        return record.ended is None or now < record.ended_until()

    def _settle(self, record: Record, now: datetime, tip: Tip | None) -> bool:
        """`_settled`, made durable: the record first, then the branch. A branch whose
        making was cut off is made, and one that an end left is dropped."""
        ended = record.ended
        if not self._settled(record, now, tip):
            self.records.remove(record)
            return False
        if record.ended != ended:
            self.records.save(record)
        if record.ended is not None and tip is not None:
            self.store.drop(ref(record.id))
        elif record.ended is None and tip is None:
            self.store.branch(ref(record.id), record.base)
        return True

    def _sweep(self, now: datetime) -> list[Record]:
        """Settle every transaction, and return the records still kept."""
        tips = self.store.tips(_BRANCHES)
        return [r for r in self.records.every() if self._settle(r, now, tips.get(ref(r.id)))]

    def _end(self, record: Record, how: str, now: datetime) -> None:
        """End a transaction: mark its record first, then drop its branch."""
        record.end(how, now)
        self.records.save(record)
        self.store.drop(ref(record.id))

    def _kept(self, record: Record) -> tuple[list[str], list[str]]:
        """The commits a committed transaction kept, oldest first, and every file they
        changed, from HEAD's history."""
        if record.commit is None:
            return [], []
        entries = self.store.log(since=record.base, ref=record.commit)
        files = sorted({file for entry in entries for file in entry.files})
        return [entry.commit for entry in reversed(entries)], files

    def _outcome(self, record: Record, replayed: bool) -> Outcome:
        """What `commit` answers of a transaction that ended: what it kept, or the refusal
        that lists its calls when the data moved. Any other end raises `TransactionEnded`."""
        if record.ended in (COMMITTED, NOTHING):
            commits, files = self._kept(record)
            return Committed(record.commit, files, replayed=replayed, commits=commits)
        if record.ended == MOVED:
            return Refusal(message=f"{MOVED_TEXT}\n\n{calls_text(record)}")
        raise TransactionEnded(self._ended_text(record))

    def _ended_text(self, record: Record) -> str:
        """How a transaction ended, as any later call with its id answers."""
        if record.ended is None:
            raise ValueError(f"transaction {record.id} is still open")
        if record.ended in (COMMITTED, NOTHING):
            return committed_text(record.id, *self._kept(record), record.commit)
        text = ENDED_TEXTS[record.ended].format(id=record.id)
        return f"{text}\n\n{calls_text(record)}" if record.ended == MOVED else text
