import logging
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cliquet.store import FULL_ID, GitStore, Moved, scratch_repository

NOW = datetime(2026, 11, 10, 18, 30, tzinfo=ZoneInfo("Europe/Paris"))
BRANCH = "refs/app/transactions/t-0123456789abcdef"


def message(label: str) -> str:
    """A commit message with the trailer that marks a commit as Cliquet's."""
    return f"{label}\n\nApp-Action: {label}\nApp-Caller: test\n"


@pytest.fixture
def store(tmp_path: Path) -> GitStore:
    data = scratch_repository(tmp_path, {"log.txt": b"start\n"})
    return GitStore(data)


def on_disk(store: GitStore) -> str:
    return (store.data_dir / "log.txt").read_text()


def objects(store: GitStore) -> dict[str, int]:
    """What `git count-objects -v` says: the loose objects, the packs, and the rest."""
    counted = store._git("count-objects", "-v")
    pairs = (line.partition(": ") for line in counted.splitlines())
    return {name: int(value) for name, _, value in pairs}


def wait_for_pack(store: GitStore) -> None:
    """Wait, with a deadline, for the pack a write started on a thread of its own to end."""
    assert store._packing.acquire(timeout=20), "the pack didn't finish in time"
    store._packing.release()


def built_not_referenced(store: GitStore, content: bytes) -> str:
    """A commit built apart from every ref, as a write builds one before it moves HEAD."""
    head = store.head()
    assert head is not None
    store.branch(BRANCH, head)
    commit = store.commit(head, {"log.txt": content}, message("building"), "test", NOW, ref=BRANCH)
    store.drop(BRANCH)
    return commit


@contextmanager
def cut_off(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """A write that stops once HEAD moved, before the files on disk and the index follow it."""

    def stop(*args: object) -> None:
        raise KeyboardInterrupt("stopped before the files are written")

    with monkeypatch.context() as patched:
        patched.setattr(GitStore, "_mirror", stop)
        yield


def branch_of(store: GitStore, lines: list[str]) -> tuple[str, str]:
    """A branch from HEAD with one commit per line. Returns the base and the branch's tip."""
    base = store.head()
    assert base is not None
    store._git("update-ref", BRANCH, base)
    text = "start\n"
    tip = base
    for line in lines:
        text += line + "\n"
        tip = store.commit(tip, {"log.txt": text.encode()}, message(line), "test", NOW, ref=BRANCH)
    return base, tip


def moved_past(store: GitStore, contents: list[bytes]) -> str:
    """Move HEAD to each commit in turn, as a publish did before it repaired first: Cliquet's
    own commits, which leave the files on disk where they are."""
    base = store.head()
    assert base is not None
    store._git("update-ref", BRANCH, base)
    for content in contents:
        base = store.commit(base, {"log.txt": content}, message("publish"), "test", NOW, ref=BRANCH)
        store._git("update-ref", "-m", "cliquet: advance", "HEAD", base)
    store.drop(BRANCH)
    return base


def test_commit_on_a_ref(store: GitStore) -> None:
    head = store.head()
    base, tip = branch_of(store, ["one"])
    assert store.head() == head
    assert store.head(BRANCH) == tip
    assert store.read(tip, ("log.txt",)) == {"log.txt": b"start\none\n"}
    assert on_disk(store) == "start\n"
    assert store.dirty() == []
    assert [e.subject for e in store.log(ref=BRANCH)][:1] == ["one"]
    assert [e.subject for e in store.log()][:1] == ["given"]
    with pytest.raises(Moved):
        store.commit(base, {"log.txt": b"other\n"}, message("late"), "test", NOW, ref=BRANCH)
    assert store.head(BRANCH) == tip
    store.drop(BRANCH)
    assert store.head(BRANCH) is None


def test_advance(store: GitStore) -> None:
    base, tip = branch_of(store, ["one", "two"])
    store.advance(BRANCH, base)
    assert store.head() == tip
    assert on_disk(store) == "start\none\ntwo\n"
    assert store.dirty() == []
    assert store.recorded() == tip

    branch_of(store, ["three"])
    elsewhere = store.commit(
        tip, {"log.txt": b"start\nelsewhere\n"}, message("elsewhere"), "test", NOW
    )
    with pytest.raises(Moved):
        store.advance(BRANCH, tip)
    assert on_disk(store) == "start\nelsewhere\n"
    assert store.recorded() == elsewhere


def test_repair_after_a_cut_off_advance(store: GitStore, monkeypatch: pytest.MonkeyPatch) -> None:
    base, tip = branch_of(store, ["one", "two", "three"])
    with cut_off(monkeypatch), pytest.raises(KeyboardInterrupt):
        store.advance(BRANCH, base)
    assert store.head() == tip
    assert on_disk(store) == "start\n"
    assert store.dirty() == ["log.txt"]
    # The files on disk are the record's, though HEAD moved: that is what makes them Cliquet's.
    assert store.recorded() == base
    assert store.repair() == []
    assert on_disk(store) == "start\none\ntwo\nthree\n"
    assert store.dirty() == []
    assert store.recorded() == tip


def test_repair_records_a_commit_once_the_files_match_it(store: GitStore) -> None:
    """A repository from before the record keeps one as soon as nothing is dirty."""
    store.drop(store.record_ref)
    assert store.recorded() is None
    assert store.repair() == []
    assert store.recorded() == store.head()


def test_each_folder_keeps_its_own_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A publish's store covers the whole repository, a data folder only its own files: the
    data folder's repair never claims a commit the code hasn't reached, so the code's record
    still finishes a publish cut off after its commit."""
    data = GitStore(scratch_repository(tmp_path, {"log.txt": b"start\n"}))
    root = GitStore(tmp_path)
    assert data.record_ref != root.record_ref
    base = root.commit(root.head(), {"app.py": b"one\n"}, message("publish"), "test", NOW)
    with cut_off(monkeypatch), pytest.raises(KeyboardInterrupt):
        root.commit(base, {"app.py": b"two\n"}, message("publish"), "test", NOW)
    assert (tmp_path / "app.py").read_text() == "one\n"
    # The data folder's files are where its own record left them, so it records HEAD, and the
    # code's record still names the commit the file on disk holds.
    assert data.repair() == []
    assert data.recorded() == root.head()
    assert root.recorded() == base
    assert root.repair() == []
    assert (tmp_path / "app.py").read_text() == "two\n"
    assert root.recorded() == root.head()


def test_an_edit_beside_a_cut_off_advance_is_someone_else_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GitStore(scratch_repository(tmp_path, {"log.txt": b"start\n", "other.txt": b"x\n"}))
    base, _ = branch_of(store, ["one", "two"])
    with cut_off(monkeypatch), pytest.raises(KeyboardInterrupt):
        store.advance(BRANCH, base)
    (store.data_dir / "other.txt").write_text("edited by hand\n")
    assert store.repair() == ["other.txt"]
    assert on_disk(store) == "start\none\ntwo\n"
    # The record stays where it was: what is left dirty isn't Cliquet's to settle.
    assert store.recorded() == base


def test_a_write_records_the_files_it_matched(store: GitStore) -> None:
    """A commit on HEAD names the commit the files on disk and their index entries match."""
    assert store.recorded() == store.head()
    commit = store.commit(store.head(), {"log.txt": b"one\n"}, message("one"), "test", NOW)
    assert store.recorded() == commit == store.head()


def test_a_write_moved_past_twice_is_still_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write cut off before the files follow HEAD, then two commits on top of it, as a
    publish moved HEAD on: the record still names what the files hold, so they are Cliquet's
    own and are put right, rather than returned as someone else's edit."""
    store = GitStore(scratch_repository(tmp_path, {"log.txt": b"start\n"}))
    base = store.head()
    with cut_off(monkeypatch), pytest.raises(KeyboardInterrupt):
        store.commit(base, {"log.txt": b"one\n"}, message("one"), "test", NOW)
    assert on_disk(store) == "start\n" and store.recorded() == base
    moved = moved_past(store, [b"two\n", b"three\n"])
    assert store.head() == moved
    # HEAD^ holds `two`, so only the record can say where the files on disk belong.
    assert store.repair() == []
    assert on_disk(store) == "three\n"
    assert store.dirty() == [] and store.recorded() == moved


def test_a_repair_cut_off_after_its_first_file_finishes_on_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GitStore(scratch_repository(tmp_path, {"log.txt": b"start\n", "other.txt": b"x\n"}))
    head = store.head()
    changes: dict[str, bytes | None] = {"log.txt": b"one\n", "other.txt": b"y\n"}
    with cut_off(monkeypatch), pytest.raises(KeyboardInterrupt):
        store.commit(head, changes, message("one"), "test", NOW)
    assert store.dirty() == ["log.txt", "other.txt"]
    real = GitStore._mirror
    repaired: list[dict[str, bytes | None]] = []

    def stop_after_one(self: GitStore, up: dict[str, bytes | None], entries: bytes) -> None:
        if repaired:
            raise KeyboardInterrupt("stopped between the files")
        repaired.append(up)
        real(self, up, entries)

    with monkeypatch.context() as patched:
        patched.setattr(GitStore, "_mirror", stop_after_one)
        with pytest.raises(KeyboardInterrupt):
            store.repair()
    # Its first file went, the record didn't: the one left is still Cliquet's to put right.
    assert store.dirty() == ["other.txt"] and store.recorded() == head
    assert store.repair() == []
    assert (store.data_dir / "other.txt").read_text() == "y\n"
    assert store.dirty() == [] and store.recorded() == store.head()


def test_a_repository_with_no_record_falls_back_on_the_commit_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An app from before Cliquet kept the record: one repair by the commit before HEAD, and
    the record is kept as soon as nothing is dirty."""
    store = GitStore(scratch_repository(tmp_path, {"log.txt": b"start\n"}))
    base = store.head()
    with cut_off(monkeypatch), pytest.raises(KeyboardInterrupt):
        store.commit(base, {"log.txt": b"one\n"}, message("one"), "test", NOW)
    # As a repository is after an upgrade: the write was cut off by the code before the record.
    store.drop(store.record_ref)
    assert store.recorded() is None and store.dirty() == ["log.txt"]
    assert store.repair() == []
    assert on_disk(store) == "one\n"
    assert store.recorded() == store.head()


def test_a_record_of_a_commit_that_is_gone_counts_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GitStore(scratch_repository(tmp_path, {"log.txt": b"start\n"}))
    base = store.head()
    with cut_off(monkeypatch), pytest.raises(KeyboardInterrupt):
        store.commit(base, {"log.txt": b"one\n"}, message("one"), "test", NOW)
    gone = store.git_dir / store.record_ref
    # A ref git itself refuses to point at nothing: written by hand, as a lost commit leaves it.
    gone.write_text("1" * 40 + "\n")
    assert store.recorded() is None
    # Nothing that names no commit stops a repair: the commit before HEAD stands in for it.
    assert store.repair() == []
    assert on_disk(store) == "one\n"
    assert store.recorded() == store.head()


def test_a_publish_reads_by_its_trailer_whatever_its_label(store: GitStore) -> None:
    # An app's own action named publish, then a publish of Cliquet's, as the server writes it.
    own = store.commit(store.head(), {"log.txt": b"one\n"}, message("publish"), "test", NOW)
    trailers = "App-Action: publish\nApp-Caller: test\nApp-Version: 0123456789abcdef\n"
    published = store.commit(own, {"log.txt": b"two\n"}, f"Publish\n\n{trailers}", "test", NOW)

    newest, older = store.log(limit=2)

    assert (newest.commit, newest.action, newest.version) == (
        published,
        "app_publish",
        "0123456789abcdef",
    )
    assert (older.commit, older.action, older.version) == (own, "publish", None)
    assert store.entry(published) == newest


def test_history_from_before_the_rename_reads_as_it_did(store: GitStore) -> None:
    # Commits that Sundry wrote, before it was renamed Cliquet.
    old = (
        "sell\n\nSundry-Action: sell\nSundry-Caller: sam\nSundry-Role: staff\n"
        "Sundry-Request: r-1\nSundry-Remark: sold two\nSundry-Agent: Stock routine\n"
    )
    sold = store.commit(store.head(), {"log.txt": b"one\n"}, old, "sam", NOW)
    undo = f"revert\n\nSundry-Action: revert\nSundry-Caller: sam\nSundry-Reverts: {sold}\n"
    undone = store.commit(sold, {"log.txt": b"start\n"}, undo, "sam", NOW)
    publish = "Publish\n\nSundry-Action: publish\nSundry-Caller: sam\nSundry-Version: 0123\n"
    published = store.commit(undone, {"log.txt": b"two\n"}, publish, "sam", NOW)
    new = store.commit(published, {"log.txt": b"three\n"}, message("sell"), "test", NOW)

    entries = store.log(limit=4)

    assert [(e.commit, e.action) for e in entries] == [
        (new, "sell"),
        (published, "app_publish"),
        (undone, "app_revert"),
        (sold, "sell"),
    ]
    old_sale = entries[3]
    assert (old_sale.caller, old_sale.role, old_sale.request) == ("sam", "staff", "r-1")
    assert (old_sale.remark, old_sale.agent) == ("sold two", "Stock routine")
    assert entries[2].reverts == sold
    assert entries[1].version == "0123"
    assert [e.commit for e in store.log(caller="sam")] == [published, undone, sold]
    assert store.find_request("r-1", new) == old_sale


def test_the_log_lists_the_first_commit_s_files_whatever_git_s_config(
    store: GitStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first commit of a published app is its first publish, which brought the data.
    for name, value in {"COUNT": "1", "KEY_0": "log.showRoot", "VALUE_0": "false"}.items():
        monkeypatch.setenv(f"GIT_CONFIG_{name}", value)

    (first,) = store.log()

    assert first.files == ["log.txt"]


def test_the_commit_just_before_a_time(tmp_path: Path) -> None:
    """A commit dated in the second asked about counts as made since: its date has no finer
    part, so it may have been made after."""
    store = GitStore(scratch_repository(tmp_path, {"log.txt": b"start\n"}, when=NOW))
    first = store.head()
    later = NOW + timedelta(seconds=5)
    # Dated to the second: 18:30:05, though made 0.9 s into it.
    second = store.commit(
        first, {"log.txt": b"two\n"}, message("two"), "test", later + timedelta(seconds=0.9)
    )
    assert first is not None
    assert store.at(NOW - timedelta(seconds=1), second) is None
    assert store.at(NOW, second) is None
    assert store.at(NOW + timedelta(seconds=0.5), second) is None
    assert store.at(NOW + timedelta(seconds=1), second) == first
    assert store.at(later, second) == first
    assert store.at(later + timedelta(seconds=0.5), second) == first
    assert store.at(later + timedelta(seconds=1), second) == second
    # The history asked about is the given commit's, not HEAD's.
    assert store.at(later + timedelta(seconds=1), first) == first


def test_the_commit_just_before_a_time_when_the_clock_went_back(tmp_path: Path) -> None:
    """The history is read in its own order: a commit that follows one dated later was made
    after it, whatever its own date says."""
    store = GitStore(scratch_repository(tmp_path, {"log.txt": b"start\n"}, when=NOW))
    first = store.head()
    hour = timedelta(hours=1)
    late = store.commit(first, {"log.txt": b"late\n"}, message("late"), "test", NOW + 2 * hour)
    back = store.commit(late, {"log.txt": b"back\n"}, message("back"), "test", NOW + hour)
    # By its date alone, `back` would be the data at half past: it holds `late`, made since.
    assert store.at(NOW + 1.5 * hour, back) == first
    assert store.at(NOW + 0.5 * hour, back) == first
    assert store.at(NOW + 3 * hour, back) == back
    assert [entry.commit for entry in store.log(since=first, ref=back)] == [back, late]


def test_a_scratch_repository_s_ids_are_sha1_whatever_git_s_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha256")
    store = GitStore(scratch_repository(tmp_path, {"log.txt": b"start\n"}, when=NOW))
    head = store.head()
    assert head is not None and FULL_ID.fullmatch(head)


def test_the_first_parents_read_are_kept_and_grow(tmp_path: Path) -> None:
    """What `at` reads is kept: a later head reads only what came after, and a branch off an
    older commit, as a transaction's, reads only its own commits, the same as from scratch."""
    store = GitStore(scratch_repository(tmp_path, {"log.txt": b"start\n"}, when=NOW))
    first = store.head()
    assert first is not None

    def fresh(ref: str) -> tuple[tuple[str, int], ...]:
        return GitStore(store.data_dir)._first_parents(ref)

    assert store._first_parents(first) == fresh(first)
    second = store.commit(first, {"log.txt": b"two\n"}, message("two"), "test", NOW)
    third = store.commit(second, {"log.txt": b"three\n"}, message("three"), "test", NOW)
    assert [c for c, _ in store._first_parents(third)] == [first, second, third]
    # A read in a transaction from the live head reads only its own commits.
    store.branch(BRANCH, third)
    call = store.commit(third, {"log.txt": b"call\n"}, message("call"), "test", NOW, ref=BRANCH)
    assert store._first_parents(call) == fresh(call)
    # Kept, it is one commit of the live history, its call the merge's second parent, and
    # the live history is what is kept again: later reads don't start over from `third`.
    merged = store.merge(third, call, "Commit transaction\n", "test", NOW)
    store.advance(merged, third)
    assert [c for c, _ in store._first_parents(merged)] == [first, second, third, merged]
    assert store._first_parents(merged) == fresh(merged)
    assert store._chain[0][-1][0] == merged
    later = store.commit(merged, {"log.txt": b"later\n"}, message("later"), "test", NOW)
    assert store._first_parents(later) == fresh(later)
    assert store._chain[0][-1][0] == later
    assert store._first_parents(second) == fresh(second)


def test_the_log_lists_a_kept_transaction_s_calls_not_its_merge(tmp_path: Path) -> None:
    """Each call stays its own change, children first, even when they cancel out or the
    clock went back; the merge that kept them changed nothing of its own."""
    hour = timedelta(hours=1)
    store = GitStore(scratch_repository(tmp_path, {"log.txt": b"start\n"}, when=NOW + 2 * hour))
    base = store.head()
    assert base is not None
    store.branch(BRANCH, base)
    one = store.commit(base, {"log.txt": b"one\n"}, message("one"), "test", NOW + hour, BRANCH)
    back = store.commit(one, {"log.txt": b"start\n"}, message("back"), "test", NOW, BRANCH)
    kept = NOW + 3 * hour
    merged = store.merge(base, back, "Commit transaction\n", "test", kept)
    store.advance(merged, base)
    assert [entry.commit for entry in store.log()] == [back, one, base]
    assert [entry.commit for entry in store.log(since=base)] == [back, one]
    assert store.entry(merged) is None
    assert store.at(kept, merged) == base
    assert store.at(kept + timedelta(seconds=1), merged) == merged


def test_a_write_packs_once_enough_loose_objects_pile_up(
    store: GitStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enough loose objects, and the write after them packs the repository on a thread of its
    own: history, a read and the log answer the same afterwards."""
    monkeypatch.setattr("cliquet.store.PACK_AT", 0)
    start = store.head()
    assert start is not None
    before_read = store.read(start, ("log.txt",))
    base = start
    for line in range(3):
        content = f"start\n{line}\n".encode()
        base = store.commit(base, {"log.txt": content}, message(f"line {line}"), "test", NOW)
        wait_for_pack(store)
        counted = objects(store)
        assert (counted["count"], counted["packs"]) == (0, 1)
    assert store.read(start, ("log.txt",)) == before_read
    assert [entry.subject for entry in store.log(limit=4)] == [
        "line 2",
        "line 1",
        "line 0",
        "given",
    ]
    assert on_disk(store) == "start\n2\n"
    assert store.dirty() == []


def test_a_pack_keeps_a_commit_no_ref_names_yet(store: GitStore) -> None:
    """A commit built with plumbing, as a write builds one before it moves HEAD, survives the
    pack: git's two-week prune grace holds it until the ref that names it moves."""
    building = built_not_referenced(store, b"built\n")
    assert store.head() != building
    store.pack()
    assert store.read(building, ("log.txt",)) == {"log.txt": b"built\n"}


def test_a_failed_pack_logs_and_leaves_the_write_that_landed(
    store: GitStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A pack that fails is no part of the write that already landed: it logs one warning
    with git's own text, and the write reads as it did."""
    monkeypatch.setattr("cliquet.store.PACK_AT", 0)
    caplog.set_level(logging.WARNING, logger="cliquet.store")
    real = GitStore._run

    def failing(
        self: GitStore,
        *args: str,
        cwd: Path | None = None,
        input: bytes | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        if "gc" in args:
            return subprocess.CompletedProcess(["git", *args], 1, b"", b"fatal: cannot pack")
        return real(self, *args, cwd=cwd, input=input, env=env)

    monkeypatch.setattr(GitStore, "_run", failing)
    commit = store.commit(store.head(), {"log.txt": b"one\n"}, message("one"), "test", NOW)
    wait_for_pack(store)
    assert store.head() == commit
    assert on_disk(store) == "one\n"
    assert store.dirty() == []
    assert store.read(commit, ("log.txt",)) == {"log.txt": b"one\n"}
    logged = [record for record in caplog.records if record.name == "cliquet.store"]
    assert [record.levelno for record in logged] == [logging.WARNING]
    assert "fatal: cannot pack" in logged[0].getMessage()


def test_what_an_app_declares_is_read_from_the_tree_of_each_commit(tmp_path: Path) -> None:
    files = {"ledger.beancount": b"open\n", "2026/01.beancount": b"jan\n", "notes.txt": b"x\n"}
    store = GitStore(scratch_repository(tmp_path, files))
    first = store.head()
    second = store.commit(first, {"2026/02.beancount": b"feb\n"}, message("add"), "test", NOW)
    declared = ["notes.txt", "**/*.beancount"]
    assert store.read_declared(first, declared) == files
    assert store.read_declared(second, declared) == {**files, "2026/02.beancount": b"feb\n"}
    # A name the pattern matches too is read once, and a missing name is left out.
    assert store.read_declared(first, ["2026/*.beancount", "2026/01.beancount", "gone.csv"]) == {
        "2026/01.beancount": b"jan\n"
    }
    # Plain names are read as `read` reads them, and nothing at no commit.
    assert store.read_declared(first, ["notes.txt"]) == store.read(first, ("notes.txt",))
    assert store.read_declared(None, declared) == {}
