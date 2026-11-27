"""One app's git repository on the server: its code and its data."""

import os
import shutil
import stat
import subprocess
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cliquet import Refused
from cliquet.files import Kind, kind_of
from cliquet.sandbox import version_id
from cliquet.scaffold import IGNORE
from cliquet.store import (
    ACTION,
    CALLER,
    GIT_FLAGS,
    OBJECT_FORMAT,
    RESTORES,
    VERSION,
    Entry,
    GitStore,
    trailer_format,
    trailer_line,
)
from cliquet.tools import PUBLISH
from cliquet.worker import undo_files

# The subject of a publish's commit, before its note.
_PUBLISH = "Publish"
# What a draft starts from: the code and its lock. The migration ran once, and is left behind.
_CODE = frozenset({Kind.CODE, Kind.LOCK})


class NoCommit(Exception):
    """The repository has no commit, so there is nothing to download."""


class CantRestore(Exception):
    """The data from before the publishes a restore undoes can't be put back. The message says
    why, in one sentence."""


@dataclass(frozen=True)
class Publish:
    """One publish of the live history: the commit that made a version live."""

    commit: str
    time: str
    login: str
    note: str
    version: str
    # The publish whose version this one restored, if it restored one.
    restores: str | None


_FORMAT = f"%H%x1f%cI%x1f%an%x1f%s%x1f{trailer_format(VERSION)}%x1f{trailer_format(RESTORES)}%x1e"


class AppRepo:
    """One app's git repository on the server: its code and its data."""

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)

    def exists(self) -> bool:
        """True when this folder holds a repository, not just a folder inside another one."""
        return (self.folder / ".git").is_dir()

    def head(self) -> str | None:
        """The current commit, or None when there is no repository or no commit yet."""
        if not self.exists():
            return None
        return GitStore(self.folder).head()

    def published(self) -> str | None:
        """The commit that made the live version live: the last publish HEAD holds."""
        if self.head() is None:
            return None
        found = subprocess.run(
            [
                *["git", *GIT_FLAGS, "log", "-1", "--format=%H"],
                *["--extended-regexp", f"--grep={trailer_line(VERSION)}", "HEAD"],
            ],
            cwd=self.folder,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return found.strip() or None

    def publishes(self, at: str = "HEAD", limit: int | None = None) -> list[Publish]:
        """Every publish of the live history at HEAD, or at the commit `at`, newest first:
        the `limit` latest ones, if given."""
        if self.head() is None:
            return []
        most = [] if limit is None else [f"-{limit}"]
        found = subprocess.run(
            [
                *["git", *GIT_FLAGS, "log", "--first-parent", f"--format={_FORMAT}", *most],
                *["--extended-regexp", f"--grep={trailer_line(VERSION)}", at],
            ],
            cwd=self.folder,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        publishes = []
        for record in found.split("\x1e"):
            if not record.strip():
                continue
            commit, time, login, subject, version, restores = record.strip("\n").split("\x1f")
            publishes.append(
                Publish(
                    commit=commit,
                    time=datetime.fromisoformat(time).isoformat(),
                    login=login,
                    note=publish_note(subject),
                    version=version.strip(),
                    restores=restores.strip() or None,
                )
            )
        return publishes

    def data_store(self) -> GitStore:
        """The app's data, as git keeps it, with its history."""
        return GitStore(self.folder / "data")

    def restored_data(self, commit: str, at: str) -> dict[str, bytes]:
        """All the data at the commit `at`, with what each publish after `commit` did to it
        undone, the newest first, by revert's own rule. Raises `CantRestore` when a change
        made since touched the same lines.

        A publish changes the data through its migration, or through a restore: one that
        changed none leaves the data as it is.
        """
        store = self.data_store()
        undone = self.undone(commit, at)
        data = self.data(self.data_names(at), at)
        counts = store.counts([one.commit for one in undone])
        for one in undone:
            if not counts[one.commit]:
                continue
            parent = store.parent(one.commit)
            before = self.data(self.data_names(parent), parent) if parent is not None else {}
            after = self.data(self.data_names(one.commit), one.commit)
            try:
                data = undo_files(data, before, after, one.commit)
            except Refused:
                touched = set(changed(before, after))
                skipped = {other.commit for other in undone}
                later = [
                    entry
                    for entry in store.log(since=one.commit, ref=at)
                    if entry.commit not in skipped and touched & set(entry.files)
                ]
                count = f"{len(later)} change{'s' if len(later) != 1 else ''}"
                raise CantRestore(
                    f"The data from before the publish {one.commit[:7]} can't be put back: "
                    f"{count} made since touched the same lines."
                ) from None
        return data

    def undone(self, commit: str, at: str = "HEAD") -> list[Publish]:
        """The publishes after the publish `commit`, in the live history at `at`, newest first:
        those a restore of its version undoes. Raises `CantRestore` when `commit` isn't one."""
        publishes = self.publishes(at)
        commits = [one.commit for one in publishes]
        if commit not in commits:
            raise CantRestore(f"{commit[:7]} isn't a publish of the app's live history.")
        return publishes[: commits.index(commit)]

    def changes_since(self, commit: str) -> int:
        """How many changes to the data were made since `commit`, publishes left out."""
        store = self.data_store()
        return sum(1 for entry in store.log(since=commit) if entry.action != PUBLISH)

    def code(self, at: str | None = None) -> dict[str, bytes]:
        """The code at HEAD, or at the commit `at`, by path: app.py, its lock, instructions,
        and the tests."""
        head = at or self.head()
        if head is None:
            return {}
        listed = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", head],
            cwd=self.folder,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        paths = [name for name in listed.splitlines() if kind_of(name) in _CODE]
        return GitStore(self.folder).read(head, tuple(paths))

    def version(self) -> str:
        """The version the code at HEAD makes."""
        return version_id(self.code())

    def migration_names(self) -> list[str]:
        """The files under `migrations/` at HEAD, without the `migrations/`, sorted."""
        head = self.head()
        if head is None:
            return []
        listed = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", head, "--", "migrations"],
            cwd=self.folder,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return sorted(
            name[len("migrations/") :]
            for name in listed.splitlines()
            if name.startswith("migrations/")
        )

    def data_names(self, at: str | None = None) -> list[str]:
        """The files under `data/` at HEAD, or at the commit `at`, without the `data/`,
        sorted."""
        head = at or self.head()
        if head is None:
            return []
        listed = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", head, "--", "data"],
            cwd=self.folder,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return sorted(
            name[len("data/") :] for name in listed.splitlines() if name.startswith("data/")
        )

    def data(self, files: Sequence[str], at: str | None = None) -> dict[str, bytes]:
        """The content at HEAD, or at the commit `at`, of the given data files, by their name
        inside `data/`: names, or what an app declares, whose patterns are resolved there."""
        head = at or self.head()
        if head is None:
            return {}
        return self.data_store().read_declared(head, files)

    def publish(
        self,
        base: str | None,
        changes: dict[str, bytes | None],
        *,
        version: str,
        note: str,
        login: str,
        when: datetime,
        restores: str | None = None,
    ) -> str:
        """Commit code and data on top of `base`, making the repository if needed. A publish
        that `restores` an earlier version names the commit that published it.

        It holds the app's write lock, so a call running meanwhile never writes its files and
        its index at the same time as a publish does."""
        if not self.exists():
            self.folder.mkdir(parents=True, exist_ok=True)
            init = ["git", "init", "-q", OBJECT_FORMAT]
            subprocess.run(init, cwd=self.folder, capture_output=True, check=True)
            changes = {**changes, ".gitignore": IGNORE.encode()}
        message = _message(note, login, version, restores)
        store = GitStore(self.folder)
        with store.write_lock():
            return store.commit(base, changes, message, login, when)

    def export(self, into: Path, name: str) -> Path:
        """Zip a copy of the live branch into `into/<name>.zip`, as one folder `<name>/`.

        The copy holds the live branch and what it reaches, nothing else: no other ref, no
        object only the server's own refs reach, no remote, and nothing of `.git/app/`.
        On a failure, it leaves neither the copy nor a part of the zip.
        """
        if self.head() is None:
            raise NoCommit(f"{self.folder.name} has no commit to download")
        clone = into / name
        target = into / f"{name}.zip"
        try:
            _copy(self.folder, clone)
            _zip(clone, target, name)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(clone, ignore_errors=True)
        return target

    def delete(self) -> None:
        """Remove the folder, if it exists."""
        shutil.rmtree(self.folder, ignore_errors=True)


def changed(old: dict[str, bytes], new: dict[str, bytes]) -> dict[str, bytes | None]:
    """What turns `old` into `new`: each new or changed file, and None for each removed one."""
    result: dict[str, bytes | None] = {
        name: content for name, content in new.items() if old.get(name) != content
    }
    result |= {name: None for name in old if name not in new}
    return result


# The branch a download gets when the server's repository has a detached HEAD.
_DETACHED_BRANCH = "main"


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run git for a download: no hook, and no configuration or variable of the server's."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env |= {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", *GIT_FLAGS, "-c", "core.fsmonitor=false", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )


def _copy(source: Path, target: Path) -> None:
    """Make in `target` a repository that holds the live branch of `source`, and nothing else.

    A fetch sends a pack of what HEAD reaches, so no other ref, no object only the server's own
    refs reach, no remote, no reflog and no `FETCH_HEAD` naming `source` is ever made. A link in
    the repository's files is written as a plain file holding its target (`core.symlinks`).
    """
    found = _git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=source, check=False)
    branch = found.stdout.strip() or _DETACHED_BRANCH
    target.mkdir()
    _git("init", "--quiet", "--template=", OBJECT_FORMAT, f"--initial-branch={branch}", cwd=target)
    _git("config", "core.symlinks", "false", cwd=target)
    _git(
        *("-c", "core.logAllRefUpdates=false", "fetch", "--quiet", "--no-tags"),
        *("--no-write-fetch-head", "--update-head-ok", str(source), f"HEAD:refs/heads/{branch}"),
        cwd=target,
    )
    _git("read-tree", "--reset", "-u", "HEAD", cwd=target)
    # The index now keeps each file's times, inode, owner and size here: write it again from
    # HEAD alone, with none of them. Git fills them in at the first `git status`.
    _git("read-tree", "HEAD", cwd=target)


def _zip(folder: Path, target: Path, name: str) -> None:
    """Zip the folders and regular files under `folder` as `<name>/`, never following a link."""
    objects = folder / ".git" / "objects"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for root, folders, files in os.walk(folder, followlinks=False):
            here = Path(root)
            folders[:] = sorted(one for one in folders if not (here / one).is_symlink())
            inside = Path(name) / here.relative_to(folder)
            archive.mkdir(inside.as_posix())
            for file in sorted(files):
                path = here / file
                if stat.S_ISREG(path.lstat().st_mode):
                    # Git's objects are compressed already.
                    packed = path.is_relative_to(objects)
                    kind = zipfile.ZIP_STORED if packed else zipfile.ZIP_DEFLATED
                    archive.write(path, (inside / file).as_posix(), compress_type=kind)


def publish_note(subject: str) -> str:
    """The note of a publish, from its commit's subject: empty when it had none."""
    return subject.removeprefix(_PUBLISH).removeprefix(":").strip()


def is_publish(entry: Entry) -> bool:
    """Whether a change is a publish, told by its trailer, as the history tells it."""
    return entry.action == PUBLISH


def remark_of(entry: Entry) -> str | None:
    """A change's remark, as its history shows it: a publish's is its note, None when it had
    none."""
    return (publish_note(entry.subject) or None) if is_publish(entry) else entry.remark


def _message(note: str, login: str, version: str, restores: str | None) -> str:
    subject = " ".join(note.split())[:200]
    head = f"{_PUBLISH}: {subject}" if subject else _PUBLISH
    lines = [
        head,
        "",
        f"{ACTION}: publish",
        f"{CALLER}: {login}",
        f"{VERSION}: {version}",
    ]
    if restores is not None:
        lines.append(f"{RESTORES}: {restores}")
    return "\n".join(lines) + "\n"
