"""An app's data in git: read files at a commit, commit changes to the data folder only.

The data folder may sit anywhere in a repository. A commit is built apart from the
person's index and working tree, then becomes current with one atomic step: moving
HEAD from the commit the change was checked against to the new one. Only then are the
files on disk and the index entries of the data folder brought up to date.
"""

import fcntl
import functools
import logging
import math
import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from cliquet.files import declares, is_pattern
from cliquet.tools import PUBLISH, REVERT

_log = logging.getLogger(__name__)

# Every trailer Cliquet writes starts with `App-`, a name no rename of the product changes.
ACTION = "App-Action"
CALLER = "App-Caller"
ROLE = "App-Role"
REVERTS = "App-Reverts"
# The label an undo is stored under, with its `App-Reverts` trailer, as Cliquet always wrote
# it. It reads as the tool that made it, `tools.REVERT`.
UNDO = "revert"
# The blob of `app.py` that made the change.
CODE = "App-Code"
REQUEST = "App-Request"
TRANSACTION = "App-Transaction"
# A line the caller wrote to be kept with the change: the one thing of a call's that is kept on
# purpose.
REMARK = "App-Remark"
# The trailer of a commit that publishes a new version of the app, as the server writes it.
# It reads as `tools.PUBLISH`, whatever its label.
VERSION = "App-Version"
# The trailer of a publish that restores an earlier version, as the server writes it: the
# commit that first published the version it restores.
RESTORES = "App-Restores"
# The name of the API token that made a change, as it was then: its person's own words, read
# only by that person and the app's owner.
AGENT = "App-Agent"
# Each trailer's name before Sundry was renamed Cliquet. History written then still reads:
# every trailer is read under both names.
_FORMERLY = {CODE: "Sundry-App"}

# What every git Cliquet runs is given: no signing prompt, and no hook.
GIT_FLAGS = ["-c", "commit.gpgsign=false", "-c", f"core.hooksPath={os.devnull}"]
_GIT = ["git", *GIT_FLAGS]
# Data commits are small: flush them to disk, so a power cut can't lose the newest one.
_DURABLE = ["-c", "core.fsync=committed"]
# How many loose objects a repository may hold, each a whole copy of a file a write changed,
# before a write packs it. The write counts them itself: git's own `gc --auto` needs thousands
# and declines when unreachable objects, as a dropped transaction's leave, are most of them.
PACK_AT = 1024
_FIELD, _RECORD = "\x1f", "\x1e"
# How a caller names a commit: its id, in full or its first 7+ characters. Anything else git
# reads, such as `:/text` or `HEAD~2`, would let a caller search the history.
_COMMIT_ID = re.compile(r"[0-9a-f]{7,40}")
# A commit's full id, and git's own name for no object, of the same form. Every repository
# Cliquet makes says its ids are SHA-1, whatever git's own default: a SHA-256 one would give
# ids of 64 characters.
FULL_ID = re.compile(r"[0-9a-f]{40}")
NULL_ID = "0" * 40
OBJECT_FORMAT = "--object-format=sha1"
_TRAILERS = (
    ACTION,
    CALLER,
    ROLE,
    REVERTS,
    CODE,
    REQUEST,
    TRANSACTION,
    REMARK,
    VERSION,
    RESTORES,
    AGENT,
)
# Where a folder's record lives: the commit its files and their index entries last matched,
# so a write cut off after its commit is finished even once HEAD moved on. One ref per folder
# a store writes, the repository's own at `RECORDS/root`. A ref, written with `git
# update-ref`, not a file of its own: packing keeps it.
RECORDS = "refs/app/files"


_SCRATCH = {
    "GIT_AUTHOR_NAME": "app",
    "GIT_AUTHOR_EMAIL": "app@localhost",
    "GIT_COMMITTER_NAME": "app",
    "GIT_COMMITTER_EMAIL": "app@localhost",
}


class StoreError(Exception):
    """A git operation failed, or the data folder is not in a state Cliquet can write to."""


class GitFailed(StoreError):
    """A git command failed: its text is git's own, so only the app's owner reads it."""


class Moved(StoreError):
    """HEAD moved since the change was checked: someone else committed meanwhile."""


@dataclass(frozen=True)
class Entry:
    """One commit that changed the data."""

    commit: str
    time: str
    subject: str
    action: str | None
    caller: str | None
    role: str | None
    reverts: str | None
    app: str | None
    request: str | None
    files: list[str]
    transaction: str | None = None
    remark: str | None = None
    # The version a publish made live: only Cliquet's own publish has one.
    version: str | None = None
    # The publish whose version a publish restores, if it restores one.
    restores: str | None = None
    agent: str | None = None


@dataclass(frozen=True)
class Changed:
    """A data file one commit changed, with how many lines it added and removed: None for
    a file git reads as binary."""

    file: str
    added: int | None
    removed: int | None


@dataclass(frozen=True)
class Tip:
    """The commit a ref points at, and its committer date, to the second."""

    commit: str
    time: datetime


class GitStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.root = Path(self._git("rev-parse", "--show-toplevel", cwd=self.data_dir).strip())
        self.git_dir = Path(self._git("rev-parse", "--absolute-git-dir").strip())
        self.prefix = self.data_dir.relative_to(self.root.resolve()).as_posix()
        # The first parents `at` last read, oldest first, and where each commit is in them.
        self._chain: tuple[tuple[tuple[str, int], ...], dict[str, int]] = ((), {})
        # Held while a pack runs, so one store runs one pack at a time.
        self._packing = threading.Lock()
        # Raised after a pack that failed, so the next write doesn't try again at once.
        self._pack_at: int | None = None

    def _run(
        self,
        *args: str,
        cwd: Path | None = None,
        input: bytes | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [*_GIT, *args],
            cwd=cwd or self.root,
            input=input,
            capture_output=True,
            env={**os.environ, **(env or {})},
        )

    def _git_bytes(
        self,
        *args: str,
        cwd: Path | None = None,
        input: bytes | None = None,
        env: dict[str, str] | None = None,
    ) -> bytes:
        result = self._run(*args, cwd=cwd, input=input, env=env)
        if result.returncode != 0:
            raise GitFailed(f"git {' '.join(args)} failed: {result.stderr.decode().strip()}")
        return result.stdout

    def _git(
        self,
        *args: str,
        cwd: Path | None = None,
        input: bytes | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        return self._git_bytes(*args, cwd=cwd, input=input, env=env).decode()

    def _path(self, file: str) -> str:
        return f"{self.prefix}/{file}" if self.prefix != "." else file

    def _file(self, path: str) -> str:
        return path[len(self.prefix) + 1 :] if self.prefix != "." else path

    def _object(self, revision: str) -> str | None:
        out = self._run("rev-parse", "--verify", "-q", revision).stdout.decode().strip()
        return out or None

    # Reading

    def head(self, ref: str = "HEAD") -> str | None:
        """The commit `ref` points at, or None when there is none, such as in a new repository."""
        return self._object(f"{ref}^{{commit}}")

    def resolve(self, commit: str) -> str:
        """The full id of the commit a caller named by its id, in full or its first 7+
        characters, in either case. Raises StoreError if it names none, or isn't an id."""
        full = None
        named = commit.lower()
        if _COMMIT_ID.fullmatch(named):
            out = self._run(
                "rev-parse", "--verify", "-q", "--end-of-options", f"{named}^{{commit}}"
            )
            full = out.stdout.decode().strip() or None
        if full is None:
            raise StoreError(f"there is no commit {commit!r}")
        return full

    def at(self, when: datetime, ref: str) -> str | None:
        """The commit `ref`'s history held just before `when`: the last one before the first
        dated in the second of `when` or later, each after its first parent; None when the
        first commit is. `ref` is a commit's full id.

        A commit's date is its committer's, kept to the second, so one dated in the second of
        `when` may have been made after it. The history is read in its own order: git's order
        by date would trust a clock that never went back. A kept transaction is one commit of
        that history, dated when it was kept (`merge`), whenever its calls ran. So no commit
        that reached the history at or after `when` is ever at or before the one returned.
        """
        stamp = math.floor(when.timestamp())
        before = None
        for commit, dated in self._first_parents(ref):
            if dated >= stamp:
                return before
            before = commit
        return before

    def _first_parents(self, ref: str) -> tuple[tuple[str, int], ...]:
        """`ref`'s commits, each after its first parent, oldest first, with their dates in
        Unix seconds, `ref` last. A commit's parents and date never change, so what was read
        is kept, and a later call reads only the commits after it."""
        chain, where = self._chain
        if ref in where:
            return chain[: where[ref] + 1]
        if chain:
            fmt = "--format=%H %ct %P"
            since = f"{chain[-1][0]}..{ref}"
            read = self._run("log", "--first-parent", "--reverse", fmt, since, "--")
            # A repository made again in the same folder no longer has the commits kept.
            out = read.stdout.decode() if read.returncode == 0 else ""
            rows = [line.split(" ") for line in out.splitlines() if line]
            joins = rows[0][2] if rows and len(rows[0]) > 2 else None
            if joins in where:
                found = chain[: where[joins] + 1] + tuple((c, int(d)) for c, d, *_ in rows)
                # The longest is kept: a transaction's branch, while it reads, gives way to
                # the live history once that is as long, so neither reads it all again.
                if len(found) >= len(chain):
                    self._chain = (found, {commit: i for i, (commit, _) in enumerate(found)})
                return found
        out = self._git("log", "--first-parent", "--reverse", "--format=%H %ct", ref, "--")
        rows = [line.split(" ") for line in out.splitlines() if line]
        found = tuple((c, int(d)) for c, d in rows)
        self._chain = (found, {commit: i for i, (commit, _) in enumerate(found)})
        return found

    def merge(self, base: str, tip: str, message: str, author: str, when: datetime) -> str:
        """A commit of `tip`'s data on top of `base`, with `tip` as its second parent, dated
        `when`: a transaction kept at `when`, its calls `tip`'s history since `base`. HEAD's
        first parents read it as one change made then, and each of its calls stays its own
        commit. It moves no ref."""
        tree = f"{tip}^{{tree}}"
        parents = ["-p", base, "-p", tip]
        args = [*_DURABLE, "commit-tree", tree, *parents, "-m", message]
        return self._git(*args, env=_commit_env(author, when)).strip()

    def kept_by(self, base: str, tip: str) -> str | None:
        """The commit of HEAD's first parents that kept a transaction from `base` to `tip`, its
        merge of the two (`merge`), or None when there is none."""
        out = self._git("log", "--first-parent", "--merges", "--format=%H %P", f"{base}..HEAD")
        for line in out.splitlines():
            commit, *parents = line.split()
            if parents == [base, tip]:
                return commit
        return None

    def parent(self, commit: str) -> str | None:
        return self._object(f"{commit}^")

    def is_ancestor(self, ancestor: str, commit: str) -> bool:
        return self._run("merge-base", "--is-ancestor", ancestor, commit).returncode == 0

    def read(self, commit: str | None, files: tuple[str, ...]) -> dict[str, bytes]:
        """The content of the given data files at a commit. Absent files are left out."""
        if commit is None or not files:
            return {}
        request = "".join(f"{commit}:{self._path(f)}\n" for f in files).encode()
        out = self._git_bytes("cat-file", "--batch", input=request)
        contents: dict[str, bytes] = {}
        position = 0
        for file in files:
            end = out.index(b"\n", position)
            header = out[position:end].split()
            position = end + 1
            if header[-1] == b"missing":
                continue
            size = int(header[2])
            contents[file] = out[position : position + size]
            position += size + 1
        return contents

    def read_declared(self, commit: str | None, declared: Sequence[str]) -> dict[str, bytes]:
        """The content at a commit of the data files an app declares, `declared`: each name,
        and each file a pattern matches in that commit's tree, so an old commit reads the set
        as it was then. Absent files are left out."""
        names = [entry for entry in declared if not is_pattern(entry)]
        if commit is not None and len(names) < len(declared):
            listed = self._git("ls-tree", "-r", "-z", "--name-only", commit, "--", self.prefix)
            found = (self._file(path) for path in listed.split("\0") if path)
            names += sorted(name for name in found if declares(declared, name))
        return self.read(commit, tuple(dict.fromkeys(names)))

    def dirty(self) -> list[str]:
        """Data files whose index entry or content on disk differs from the last commit."""
        out = self._git(
            "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", self.prefix
        )
        return [self._file(item[3:]) for item in out.split("\0") if item]

    @property
    def record_ref(self) -> str:
        """The ref naming this folder's record: the commit its files last matched."""
        return f"{RECORDS}/root" if self.prefix == "." else f"{RECORDS}/{self.prefix}"

    def recorded(self) -> str | None:
        """The commit this folder's files and their index entries last matched: None when
        there is no record, or its commit is gone."""
        return self.head(self.record_ref)

    @contextmanager
    def write_lock(self) -> Generator[None]:
        """Hold the app's write lock, which a write that touches the working tree, the index
        or a ref takes first. Not reentrant: it waits for itself."""
        with _file_lock(self.git_dir / "app.lock"):
            yield

    # Writing

    def commit(
        self,
        base: str | None,
        changes: dict[str, bytes | None],
        message: str,
        author: str,
        when: datetime,
        ref: str = "HEAD",
    ) -> str:
        """Commit changes to data files on top of `base`, then update the files on disk.

        `changes` maps each changed file to its new content, or None to delete it.
        Raises `Moved` if `ref` is no longer `base`: nothing is changed then. A commit on
        any ref but HEAD leaves the files on disk and the index alone.
        """
        for file in changes:
            self._check_name(file)
        if ref == "HEAD":
            # Finish a write cut off after its commit before HEAD moves past its files.
            self.repair()
        env = _commit_env(author, when)
        entries = self._index_entries(changes)
        with tempfile.TemporaryDirectory(prefix="cliquet-index-") as tmp:
            index = {**env, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
            if base is not None:
                self._git("read-tree", base, env=index)
            listed = self._run("update-index", "--index-info", input=entries, env=index)
            if listed.returncode != 0 or listed.stderr.strip():
                raise GitFailed(f"git refused the changes: {listed.stderr.decode().strip()}")
            tree = self._git("write-tree", env=index).strip()
        parents = ["-p", base] if base is not None else []
        commit = self._git(*_DURABLE, "commit-tree", tree, *parents, "-m", message, env=env).strip()
        label = message.splitlines()[0]
        moved = self._run(
            *_DURABLE, "update-ref", "-m", f"cliquet: {label}", ref, commit, base or ""
        )
        if moved.returncode != 0:
            raise Moved(
                f"{ref} moved while the change was checked: {moved.stderr.decode().strip()}"
            )
        if ref == "HEAD":
            self._mirror(changes, entries)
            self._record(commit)
            self._pack_if_crowded()
        return commit

    def advance(self, ref: str, base: str | None) -> None:
        """Move HEAD to the commit `ref` points at, if HEAD is still `base`, then update the
        files on disk and the index. Raises `Moved` otherwise: nothing is changed then.
        """
        # Finish a write cut off after its commit before HEAD moves past its files.
        self.repair()
        target = self.head(ref)
        if target is None:
            raise StoreError(f"there is no {ref}")
        names = self._git(
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            base or self._empty_tree,
            target,
            "--",
            self.prefix,
        )
        files = tuple(self._file(name) for name in names.split("\0") if name)
        moved = self._run(
            *_DURABLE, "update-ref", "-m", f"cliquet: advance to {ref}", "HEAD", target, base or ""
        )
        if moved.returncode != 0:
            raise Moved(f"HEAD moved since {ref} began: {moved.stderr.decode().strip()}")
        contents = self.read(target, files)
        changes = {file: contents.get(file) for file in files}
        self._mirror(changes, self._index_entries(changes))
        self._record(target)
        self._pack_if_crowded()

    def pack(self) -> bool:
        """Pack the repository, so each version of a data file stops taking a whole copy."""
        # Never raises: a pack follows the write that landed, and one that fails changes
        # nothing. git's own prune grace keeps a commit built but not yet referenced, as a
        # write builds one, and the record refs keep the commits they name.
        try:
            packed = self._run("-c", "gc.autoDetach=false", "gc", "-q")
        except OSError as error:
            _log.warning("could not run git gc in %s: %s", self.root, error)
            return False
        if packed.returncode == 0:
            return True
        text = (packed.stderr or packed.stdout).decode(errors="replace").strip()
        _log.warning("git gc failed in %s: %s", self.root, text)
        return False

    def _pack_if_crowded(self) -> None:
        """Pack the repository once it holds enough loose objects, on a thread of its own: a
        first pack can take minutes, and the write lock must not wait for it."""
        loose = self._loose_objects()
        if loose <= (PACK_AT if self._pack_at is None else self._pack_at):
            return
        if not self._packing.acquire(blocking=False):
            return
        try:
            threading.Thread(target=self._pack_in_background, args=(loose,), daemon=True).start()
        except RuntimeError as error:
            # No thread to pack on: the write stands, and the next one tries again.
            self._packing.release()
            _log.warning("could not pack %s yet: %s", self.root, error)

    def _pack_in_background(self, loose: int) -> None:
        """Pack, then let this store pack again: one that failed waits for more objects."""
        try:
            self._pack_at = None if self.pack() else loose * 2
        finally:
            self._packing.release()

    def _loose_objects(self) -> int:
        """How many loose objects the repository holds, or 0 when git can't say."""
        counted = self._run("count-objects", "-v")
        for line in counted.stdout.decode().splitlines():
            name, _, value = line.partition(": ")
            if name == "count" and value.isdigit():
                return int(value)
        return 0

    def drop(self, ref: str) -> None:
        """Delete a ref. Nothing happens if there is none."""
        if self.head(ref) is not None:
            self._git("update-ref", "-d", ref)

    def _record(self, commit: str | None) -> None:
        """Record `commit` as the commit this folder's files and their index entries match.

        Never raises: it follows a write that already landed, so a record that can't be kept
        is logged, and the next repair records the commit again.
        """
        if commit is None:
            return
        record = self._run(*_DURABLE, "update-ref", self.record_ref, commit)
        if record.returncode != 0:
            text = (record.stderr or record.stdout).decode(errors="replace").strip()
            _log.warning("could not record %s in %s: %s", commit, self.root, text)

    @functools.cached_property
    def _empty_tree(self) -> str:
        return self._git("hash-object", "-t", "tree", "--stdin", input=b"").strip()

    def branch(self, ref: str, base: str) -> None:
        """Make `ref` point at `base`, as the start of a transaction's branch."""
        self._git("update-ref", ref, base, "")

    def counts(self, commits: Sequence[str]) -> dict[str, list[Changed]]:
        """What each commit, named by its full id, changed in the data folder: each file, with
        how many lines it added and removed, None for a file git reads as binary. Never the
        lines themselves. One git process reads them all."""
        if not commits:
            return {}
        out = self._git(
            "diff-tree",
            "--stdin",
            "-r",
            "--root",
            "--numstat",
            "--no-renames",
            "-z",
            "--",
            self.prefix,
            input="".join(f"{commit}\n" for commit in commits).encode(),
        )
        # Each commit that changed the folder is its id, then one item per file it changed.
        each: dict[str, list[Changed]] = {commit: [] for commit in commits}
        changed = []
        for item in out.split("\0"):
            if not item:
                continue
            if "\t" not in item:
                changed = each[item]
                continue
            added, removed, path = item.split("\t", 2)
            changed.append(
                Changed(
                    self._file(path),
                    None if added == "-" else int(added),
                    None if removed == "-" else int(removed),
                )
            )
        return each

    def blob_at(self, revision: str | None, path: str) -> str | None:
        """The blob id of `path`, relative to the repository's root, at a revision: None
        when the file is absent there, or there is no revision."""
        return None if revision is None else self._object(f"{revision}:{path}")

    def tips(self, prefix: str) -> dict[str, Tip]:
        """Every ref under `prefix`, or the ref itself, with the commit it points at, in
        one call."""
        out = self._git(
            "for-each-ref", "--format=%(refname) %(objectname) %(committerdate:iso-strict)", prefix
        )
        tips = {}
        for line in out.splitlines():
            name, commit, time = line.split(" ")
            tips[name] = Tip(commit, datetime.fromisoformat(time))
        return tips

    def _check_name(self, file: str) -> None:
        """Refuse a path that would land outside the data folder or under a symbolic link."""
        path = PurePosixPath(file)
        if not file or path.is_absolute() or path.as_posix() != file or ".." in path.parts:
            raise StoreError(f"{file!r} isn't a plain path inside the data folder")
        target = self.data_dir.joinpath(*path.parts)
        for parent in [target, *target.parents]:
            if parent == self.data_dir:
                break
            if parent.is_symlink():
                raise StoreError(f"{file!r} passes through a symbolic link, so it isn't written")
        if not target.parent.resolve().is_relative_to(self.data_dir):
            raise StoreError(f"{file!r} would land outside the data folder, so it isn't written")

    def _index_entries(self, changes: dict[str, bytes | None]) -> bytes:
        """`git update-index --index-info` lines for the changes, storing new contents."""
        lines = []
        for file, content in sorted(changes.items()):
            if content is None:
                lines.append(f"0 {NULL_ID}\t{self._path(file)}\n")
            else:
                blob = self._git("hash-object", "-w", "--stdin", input=content).strip()
                lines.append(f"100644 {blob}\t{self._path(file)}\n")
        return "".join(lines).encode()

    def _mirror(self, changes: dict[str, bytes | None], entries: bytes) -> None:
        """Bring the files on disk and their index entries up to date with HEAD."""
        for file, content in changes.items():
            path = self.data_dir / file
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        self._git("update-index", "--index-info", input=entries)

    def repair(self) -> list[str]:
        """Finish a write cut off after its commit, and return the files still dirty.

        After HEAD moves, the files on disk and their index entries follow it. If that stops
        midway, a data file holds the content it had before, on disk or in the index, and
        the record names the commit that content was at, even once HEAD moved on. So a file
        whose content on disk and index entry are each at HEAD or at the record is Cliquet's
        own, and is put back at HEAD's. Anything else is someone else's edit, returned. A
        repository from before Cliquet kept the record falls back on the commit before HEAD,
        once, and keeps the record as soon as nothing is left dirty.
        """
        dirty = self.dirty()
        head = self.head()
        if not dirty:
            self._record(head)
            return dirty
        if head is None or not self._is_cliquet(head):
            return dirty
        matched = self.recorded() or self.parent(head)
        left = []
        for file in dirty:
            path = self._path(file)
            on_disk = self._disk_object(file)
            listed = self._git("ls-files", "-s", "--", path).split()
            in_index = listed[1] if listed else None
            accepted = {self.blob_at(head, path), self.blob_at(matched, path)}
            if {on_disk, in_index} <= accepted:
                content = self.read(head, (file,)).get(file)
                self._mirror({file: content}, self._index_entries({file: content}))
            else:
                left.append(file)
        if not left:
            # Written once nothing is left dirty: a repair itself cut off midway leaves the
            # record where it was, so the files it didn't reach are still its own to put
            # right.
            self._record(head)
        return left

    def check_clean(self) -> None:
        """Finish a write cut off midway, and refuse to write over anyone else's edit."""
        dirty = self.repair()
        if dirty:
            raise StoreError(
                f"the data folder has changes not made by Cliquet: {', '.join(dirty)}. "
                "Commit or discard them first."
            )

    def _disk_object(self, file: str) -> str | None:
        path = self.data_dir / file
        if not path.is_file():
            return None
        return self._git("hash-object", "--", str(path)).strip()

    def _is_cliquet(self, commit: str) -> bool:
        body = self._git("log", "-1", f"--format={trailer_format(ACTION)}", commit)
        return bool(body.strip())

    # History

    def log(
        self,
        since: str | None = None,
        limit: int | None = None,
        grep: str | None = None,
        ref: str = "HEAD",
        caller: str | None = None,
    ) -> list[Entry]:
        """Commits that changed the data, newest first: from `ref`, after `since`. `grep`
        keeps those whose message holds that text, and `caller` those that caller made. `ref`
        may be a commit's full id, as HEAD read once by the caller: git then reads no ref."""
        if not FULL_ID.fullmatch(ref) and self.head(ref) is None:
            return []
        trailers = [trailer_format(key, separator=True) for key in _TRAILERS]
        # The committer date: the one `at` reads.
        fmt = "%x1e%H%x1f%cI%x1f%s%x1f" + "%x1f".join(trailers) + "%x1f"
        # The first commit's files too, whatever `log.showRoot` says. Each call of a kept
        # transaction is listed, but not the merge that kept it (`merge`), which changed
        # nothing of its own: so every commit, not only those of the parent the data came
        # from. Each before its parents, whatever their dates: by date alone, the data a
        # transaction began from would come before its calls made in the same second.
        args = ["log", f"--format={fmt}", "--name-only", "--root"]
        args += ["--full-history", "--no-merges", "--topo-order"]
        if limit is not None:
            args.append(f"-{limit}")
        patterns = [] if grep is None else [_ere_escape(grep)]
        whose = None if caller is None else trailer_value(caller)
        if whose is not None:
            # The trailer's whole line, so that -N counts only the caller's own commits.
            patterns.append(f"{trailer_line(CALLER)}{_ere_escape(whose)}$")
        if patterns:
            args += ["--extended-regexp", *(f"--grep={p}" for p in patterns), "--all-match"]
        args.append(ref if since is None else f"{since}..{ref}")
        out = self._git(*args, "--", self.prefix)
        entries = []
        for record in out.split(_RECORD)[1:]:
            fields = record.split(_FIELD)
            commit, time, subject, action, caller, role, reverts, app, request = fields[:9]
            transaction, remark, version, restores, agent, names = fields[9:15]
            entries.append(
                Entry(
                    commit=commit,
                    # Git 2.56 writes UTC as Z, and 2.43 as +00:00: answer in one form.
                    time=datetime.fromisoformat(time).isoformat(),
                    subject=subject,
                    action=_action(action, reverts, version),
                    caller=caller.strip() or None,
                    role=role.strip() or None,
                    reverts=reverts.strip() or None,
                    app=app.strip() or None,
                    request=request.strip() or None,
                    files=[self._file(n) for n in names.split("\n") if n.strip()],
                    transaction=transaction.strip() or None,
                    remark=remark.strip() or None,
                    version=version.strip() or None,
                    restores=restores.strip() or None,
                    agent=agent.strip() or None,
                )
            )
        if whose is not None:
            entries = [entry for entry in entries if entry.caller == whose]
        return entries

    def find_request(self, request_id: str, base: str | None) -> Entry | None:
        """The commit made for a request id, if any, in the history of `base`, a commit's full
        id: None when there is no history yet."""
        if base is None:
            return None
        found = [e for e in self.log(grep=request_id, ref=base) if e.request == request_id]
        return found[0] if found else None

    def entry(self, full: str) -> Entry | None:
        """The log entry of one commit, named by its full id, or None if it didn't change the
        data."""
        entries = self.log(limit=1, ref=full)
        return entries[0] if entries and entries[0].commit == full else None


def _action(action: str, reverts: str, version: str) -> str | None:
    """A commit's action. An undo reads as `REVERT` and a publish as `PUBLISH`, whatever their
    label: only an undo has `App-Reverts`, and only a publish `App-Version`, which tell
    them from an app's own action named `revert` or `publish`."""
    if reverts.strip():
        return REVERT
    return PUBLISH if version.strip() else action.strip() or None


def _commit_env(author: str, when: datetime) -> dict[str, str]:
    """The environment git commits a change of `author`'s with, dated `when`."""
    return {
        "GIT_AUTHOR_NAME": author,
        "GIT_AUTHOR_EMAIL": "app@localhost",
        "GIT_COMMITTER_NAME": "app",
        "GIT_COMMITTER_EMAIL": "app@localhost",
        **_dated(when),
    }


def _dated(when: datetime) -> dict[str, str]:
    """The environment that dates a commit `when`, as its author's and its committer's."""
    stamp = when.isoformat()
    return {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}


def _names(key: str) -> tuple[str, str]:
    """The trailer `key`'s name, and its name from before the rename to Cliquet."""
    return key, _FORMERLY.get(key, "Sundry-" + key.removeprefix("App-"))


def trailer_format(key: str, separator: bool = False) -> str:
    """The `git log` format of the trailer `key`'s values, under either name."""
    spec = "valueonly,separator=%x2C" if separator else "valueonly"
    return "".join(f"%(trailers:key={name},{spec})" for name in _names(key))


def trailer_line(key: str) -> str:
    """An extended regular expression for the start of the trailer `key`'s line."""
    return "^({}|{}): ".format(*_names(key))


def trailer_value(value: str) -> str:
    """A value as a commit's trailer holds it: on one line, with single spaces."""
    return " ".join(value.split())


def _ere_escape(text: str) -> str:
    """`text` as a POSIX extended regular expression that matches only itself."""
    return re.sub(r"([\\.\[\]()*+?{}|^$])", r"\\\1", text)


def write_atomically(path: Path, data: bytes) -> None:
    """Write a file in one step, flushed to disk: a crash leaves the old one or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, partial = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(partial, path)
    except BaseException:
        Path(partial).unlink(missing_ok=True)
        raise


def write_files(folder: Path, files: Mapping[str, bytes]) -> None:
    """Write files under `folder`, by their relative names, such as data files by their names
    inside the data folder. The names are the caller's to check."""
    for name, content in files.items():
        path = folder.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def scratch_repository(root: Path, files: dict[str, bytes], when: datetime | None = None) -> Path:
    """Start a git repository in `root` that holds only these data files, for tests and
    previews, committed at `when`, by default now, with the files on disk matching that
    commit. Returns its data folder."""
    data = root / "data"
    data.mkdir()
    write_files(data, files)
    dated = {} if when is None else _dated(when)
    for args in (
        ["init", "-q", OBJECT_FORMAT],
        ["add", "-A"],
        ["commit", "-q", "--allow-empty", "-m", "given"],
    ):
        subprocess.run(
            [*_GIT, *args],
            cwd=root,
            check=True,
            capture_output=True,
            env={**os.environ, **_SCRATCH, **dated},
        )
    store = GitStore(data)
    store._record(store.head())
    return data


@contextmanager
def _file_lock(path: Path) -> Generator[None]:
    """Hold a lock on `path`, across this process's threads and every other process."""
    with open(path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
