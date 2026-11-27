"""The builder's working copies of apps: plain folders, no git."""

import os
import shutil
import tempfile
import threading
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from cliquet.files import LOCK, MIGRATION, Kind, kind_of, path_problem
from cliquet.limits import MAX_DATA_FILE, MAX_DRAFT
from cliquet.store import write_files
from cliquet_server.settings import Settings

from .errors import BuilderError

# Code, the migration, and any file written by the builder's tools. A data file put in a draft
# through the upload page may be up to `MAX_DATA_FILE`, and the whole draft up to `MAX_DRAFT`.
MAX_FILE = 200_000


def check_path(path: str) -> Kind:
    """Which kind of file a draft's path names: code, lock, migration or data."""
    kind = kind_of(path)
    if kind is None:
        problem = path_problem(path)
        if not path.startswith("data/"):
            problem = f"{problem} Data goes in a draft only before the first publish."
        raise BuilderError(f"{path!r} isn't a draft file. {problem}")
    return kind


def max_size(kind: Kind) -> int:
    """The most one file of this kind may hold: an uploaded data file, or anything else."""
    return MAX_DATA_FILE if kind is Kind.DATA else MAX_FILE


class Drafts:
    """The builder's working copies of apps.

    Every method that changes a draft holds that draft's lock, so that two changes at once can't
    both pass the draft's total limit. Publish holds it too, from reading the draft's data to
    starting the draft again, slow as that is, so that an upload meanwhile is published or
    refused, never lost. The builder's tools check their write under it too. An edit to the
    code or the migration made during publish's checks, without the lock, makes it refuse.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        # How many times each draft's data, and its code, was written: what names the draft's
        # current data for a preview, and tells a preview its code is unchanged without reading
        # it. Previews end when the server stops, so these need to last no longer.
        self._data_writes: dict[tuple[str, str], int] = {}
        self._code_writes: dict[tuple[str, str], int] = {}
        self._locks: dict[tuple[str, str], threading.RLock] = {}
        self._locks_lock = threading.Lock()

    @classmethod
    def of(cls, settings: Settings) -> Drafts:
        """The server's drafts, in its data folder."""
        return cls(settings.data_dir / "drafts")

    def folder(self, login: str, app: str) -> Path:
        """The draft's folder, whether it exists or not."""
        return self.root / login / app

    def owner_folders(self, login: str) -> list[Path]:
        """The folders the owner's drafts and their restore markers are kept in, made or
        not."""
        return [self.root / login, self._restores(login)]

    def exists(self, login: str, app: str) -> bool:
        return self.folder(login, app).is_dir()

    def start(
        self, login: str, app: str, files: dict[str, bytes], restores: str | None = None
    ) -> None:
        """Replace the whole draft by `files`: the version that the publish `restores`
        published, if given (`restoring`)."""
        for path, content in files.items():
            _check_size(path, check_path(path), len(content))
        total = sum(len(content) for content in files.values())
        if total > MAX_DRAFT:
            raise BuilderError(_too_big_draft(total))
        with self._lock(login, app):
            shutil.rmtree(self.folder(login, app), ignore_errors=True)
            self._forget_restore(login, app)
            write_files(self.folder(login, app), files)
            if restores is not None:
                marker = self._restore_marker(login, app)
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(restores)
            self._wrote(self._data_writes, login, app)
            self._wrote(self._code_writes, login, app)

    def restoring(self, login: str, app: str) -> str | None:
        """The publish whose version the draft restores, as `start` was given it: None once
        the draft's code or migration changed since, since it is then a version of its own."""
        try:
            return self._restore_marker(login, app).read_text().strip() or None
        except FileNotFoundError:
            return None

    def sizes(self, login: str, app: str, kind: Kind | None = None) -> dict[str, int]:
        """The size of every file of the draft, or of one kind, by path, without reading them."""
        return {
            name: path.stat().st_size
            for name, path in self._paths(login, app)
            if kind is None or check_path(name) == kind
        }

    def data_names(self, login: str, app: str) -> list[str]:
        """The names of the data files, inside `data/`, without reading them."""
        return [name[len("data/") :] for name in self.sizes(login, app, kind=Kind.DATA)]

    @contextmanager
    def locked(self, login: str, app: str) -> Generator[None]:
        """Hold the draft's lock: no other change to it runs meanwhile."""
        with self._lock(login, app):
            yield

    def write(self, login: str, app: str, path: str, content: bytes) -> None:
        """Create or replace one file of the draft."""
        self._put(login, app, path, len(content), lambda out: out.write(content))

    def copy(self, login: str, app: str, path: str, source: BinaryIO, size: int) -> None:
        """Create or replace one file of the draft from an open file of `size` bytes."""

        def put(out: BinaryIO) -> None:
            source.seek(0)
            shutil.copyfileobj(source, out)

        self._put(login, app, path, size, put)

    def delete(self, login: str, app: str, path: str) -> None:
        """Remove one file of the draft."""
        kind = check_path(path)
        target = self.folder(login, app) / path
        with self._lock(login, app):
            if not target.is_file():
                raise BuilderError(f"{path} isn't in the draft. Call list_files to see the files.")
            target.unlink()
            self._wrote_kind(kind, login, app)

    def remove(self, login: str, app: str) -> None:
        """Delete the draft's folder, if there is one."""
        with self._lock(login, app):
            shutil.rmtree(self.folder(login, app), ignore_errors=True)
            self._forget_restore(login, app)
            self._wrote(self._code_writes, login, app)

    def code(self, login: str, app: str) -> dict[str, bytes]:
        """The files that make a version."""
        return self._of_kind(login, app, Kind.CODE)

    def data(self, login: str, app: str) -> dict[str, bytes]:
        """The data files, by their name inside `data/`."""
        return {
            path[len("data/") :]: content
            for path, content in self._of_kind(login, app, Kind.DATA).items()
        }

    def data_revision(self, login: str, app: str) -> int:
        """How many times the draft's data was written through this object."""
        return self._data_writes.get((login, app), 0)

    def code_revision(self, login: str, app: str) -> int:
        """How many times the draft's code was written or removed through this object: the
        same number means the same code. Read it before the code."""
        return self._code_writes.get((login, app), 0)

    def lock_file(self, login: str, app: str) -> bytes | None:
        """`app.py.lock`, or None."""
        return self._named(login, app, LOCK)

    def migration(self, login: str, app: str) -> bytes | None:
        """`migrate.py`, or None."""
        return self._named(login, app, MIGRATION)

    def _put(
        self, login: str, app: str, path: str, size: int, put: Callable[[BinaryIO], object]
    ) -> None:
        """Check one file's size and the draft's total, then let `put` write it, under the lock.

        The draft must exist: a change never makes one again after `remove`. `put` writes a
        temporary file outside the draft, which then replaces the file at once, so a reader never
        sees half a file.
        """
        kind = check_path(path)
        _check_size(path, kind, size)
        folder = self.folder(login, app)
        target = folder / path
        with self._lock(login, app):
            if not folder.is_dir():
                raise BuilderError(f"{app} has no draft. Call list_files to see its files.")
            current = self.sizes(login, app)
            total = sum(one for name, one in current.items() if name != path) + size
            if total > MAX_DRAFT:
                raise BuilderError(_too_big_draft(total))
            partial = self.root / ".partial"
            partial.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=partial)
            try:
                with os.fdopen(handle, "wb") as out:
                    put(out)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary, target)
            except FileExistsError, IsADirectoryError, NotADirectoryError:
                raise BuilderError(
                    f"{path} can't be written: a file and a folder of the draft would share a name."
                ) from None
            finally:
                Path(temporary).unlink(missing_ok=True)
            self._wrote_kind(kind, login, app)

    def _named(self, login: str, app: str, name: str) -> bytes | None:
        target = self.folder(login, app) / name
        return target.read_bytes() if target.is_file() else None

    def _of_kind(self, login: str, app: str, kind: Kind) -> dict[str, bytes]:
        """The draft's files of one kind, reading only those."""
        return {
            name: path.read_bytes()
            for name, path in self._paths(login, app)
            if check_path(name) == kind
        }

    def _lock(self, login: str, app: str) -> threading.RLock:
        # Kept for the server's life, even after `remove`: a thread may still wait on it.
        with self._locks_lock:
            return self._locks.setdefault((login, app), threading.RLock())

    def _paths(self, login: str, app: str) -> Iterator[tuple[str, Path]]:
        """The draft's files, by path, leaving out Python's caches."""
        folder = self.folder(login, app)
        if not folder.is_dir():
            return
        for path in sorted(folder.rglob("*")):
            parts = path.relative_to(folder).parts
            if path.is_file() and "__pycache__" not in parts:
                yield "/".join(parts), path

    def _restore_marker(self, login: str, app: str) -> Path:
        """Where `restoring` is kept: outside the draft, whose files are only its own."""
        return self._restores(login) / app

    def _restores(self, login: str) -> Path:
        """Where the owner's `restoring` markers are kept."""
        return self.root / ".restoring" / login

    def _forget_restore(self, login: str, app: str) -> None:
        self._restore_marker(login, app).unlink(missing_ok=True)

    def _wrote_kind(self, kind: Kind, login: str, app: str) -> None:
        """Count a write of one file of this kind: data, or code. The lock and the migration
        are no part of either. Any write but the lock's ends a restore (`restoring`)."""
        if kind is not Kind.LOCK:
            self._forget_restore(login, app)
        if kind is Kind.DATA:
            self._wrote(self._data_writes, login, app)
        elif kind is Kind.CODE:
            self._wrote(self._code_writes, login, app)

    def _wrote(self, counts: dict[tuple[str, str], int], login: str, app: str) -> None:
        key = (login, app)
        counts[key] = counts.get(key, 0) + 1


def _check_size(path: str, kind: Kind, size: int) -> None:
    limit = max_size(kind)
    if size > limit:
        how = (
            "Split it into files the app declares by a pattern."
            if kind is Kind.DATA
            else "Make it shorter."
        )
        raise BuilderError(f"A file is at most {limit:,} bytes; {path} is {size:,}. {how}")


def _too_big_draft(total: int) -> str:
    """The refusal of a draft that would grow past `MAX_DRAFT`, and what to do instead."""
    return (
        f"A draft is at most {MAX_DRAFT:,} bytes; it would be {total:,}. Delete a data file it "
        "no longer needs with delete_file, or make this one smaller."
    )
