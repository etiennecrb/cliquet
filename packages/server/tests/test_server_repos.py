import fcntl
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cliquet.sandbox import version_id
from cliquet.scaffold import IGNORE
from cliquet.store import GitStore, Moved
from cliquet_server.repos import AppRepo, changed, publish_note

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
VERSION = "0123456789abcdef"


def git(folder: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=folder, capture_output=True, text=True, check=True
    ).stdout


def test_the_first_publish_makes_the_repository(tmp_path: Path) -> None:
    repo = AppRepo(tmp_path / "apps" / "etienne" / "shop")
    assert not repo.exists()
    assert repo.head() is None
    assert repo.code() == {}
    files = {
        "app.py": b"a",
        "instructions.md": b"i",
        "tests/test_a.py": b"t",
        "data/items.json": b"[]",
    }
    commit = repo.publish(
        None, dict(files), version=VERSION, note="first", login="etienne", when=NOW
    )
    assert repo.exists()
    assert repo.head() == commit
    for name, content in {**files, ".gitignore": IGNORE.encode()}.items():
        assert (repo.folder / name).read_bytes() == content
    assert git(repo.folder, "status", "--porcelain") == ""


def test_the_commit_says_who_published_what(tmp_path: Path) -> None:
    repo = AppRepo(tmp_path / "shop")
    repo.publish(
        None,
        {"app.py": b"a"},
        version=VERSION,
        note="First\n  publish\nhere",
        login="etienne",
        when=NOW,
    )
    out = git(repo.folder, "log", "-1", "--format=%an%n%B")
    expected = (
        "Publish: First publish here\n"
        "\n"
        "App-Action: publish\n"
        "App-Caller: etienne\n"
        f"App-Version: {VERSION}\n"
    )
    assert out == "etienne\n" + expected + "\n"
    # The history reads the note back, and a publish without one has none.
    assert publish_note("Publish: First publish here") == "First publish here"
    assert publish_note("Publish") == ""


def test_code_is_read_at_head(tmp_path: Path) -> None:
    repo = AppRepo(tmp_path / "shop")
    repo.publish(
        None,
        {
            "app.py": b"a",
            "app.py.lock": b"l",
            "instructions.md": b"i",
            "tests/test_a.py": b"t",
            "data/items.json": b"[]",
        },
        version=VERSION,
        note="first",
        login="etienne",
        when=NOW,
    )
    code = repo.code()
    assert set(code) == {"app.py", "app.py.lock", "instructions.md", "tests/test_a.py"}
    assert repo.version() == version_id(code)


def test_data_is_read_at_head(tmp_path: Path) -> None:
    repo = AppRepo(tmp_path / "shop")
    repo.publish(
        None,
        {"app.py": b"a", "data/items.json": b"[]"},
        version=VERSION,
        note="first",
        login="etienne",
        when=NOW,
    )
    assert repo.data(["items.json", "missing.json"]) == {"items.json": b"[]"}


def test_a_later_publish_changes_and_removes_files(tmp_path: Path) -> None:
    repo = AppRepo(tmp_path / "shop")
    first = repo.publish(
        None,
        {"app.py": b"a", "tests/test_a.py": b"t"},
        version=VERSION,
        note="first",
        login="etienne",
        when=NOW,
    )
    repo.publish(
        first,
        {"app.py": b"b", "tests/test_a.py": None},
        version="fedcba9876543210",
        note="second",
        login="etienne",
        when=NOW,
    )
    assert repo.code()["app.py"] == b"b"
    assert "tests/test_a.py" not in repo.code()
    assert not (repo.folder / "tests" / "test_a.py").exists()


def test_a_publish_holds_the_app_s_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call and a publish never write one repository at once: the publish takes the lock a
    runtime holds for the writes it commits."""
    repo = AppRepo(tmp_path / "shop")
    others: list[bool] = []
    real = GitStore.commit

    def commit(self: GitStore, *args: Any, **kwargs: Any) -> str:
        with open(self.git_dir / "app.lock", "a") as other:
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                others.append(False)
            else:
                others.append(True)
                fcntl.flock(other, fcntl.LOCK_UN)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(GitStore, "commit", commit)
    repo.publish(
        None,
        {"app.py": b"code", "data/a.csv": b"x\n"},
        version=VERSION,
        note="first",
        login="sam",
        when=NOW,
    )
    # A second descriptor couldn't take the lock, so nothing else writes meanwhile.
    assert others == [False]


def test_a_publish_on_an_old_base_changes_nothing(tmp_path: Path) -> None:
    repo = AppRepo(tmp_path / "shop")
    first = repo.publish(
        None,
        {"app.py": b"a", "data/items.json": b"[]"},
        version=VERSION,
        note="first",
        login="etienne",
        when=NOW,
    )
    data_commit = GitStore(repo.folder / "data").commit(
        first, {"items.json": b"[1]"}, "data", "sam", NOW
    )
    with pytest.raises(Moved):
        repo.publish(
            first,
            {"app.py": b"b"},
            version="fedcba9876543210",
            note="second",
            login="etienne",
            when=NOW,
        )
    assert repo.head() == data_commit


def test_the_repository_is_its_own(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    repo = AppRepo(tmp_path / "apps" / "etienne" / "shop")
    assert not repo.exists()
    repo.publish(None, {"app.py": b"a"}, version=VERSION, note="first", login="etienne", when=NOW)
    assert (repo.folder / ".git").is_dir()
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode != 0


def test_changed() -> None:
    assert changed({"a": b"1", "b": b"2"}, {"a": b"1", "b": b"3", "c": b"4"}) == {
        "b": b"3",
        "c": b"4",
    }
    assert changed({"a": b"1"}, {}) == {"a": None}


def test_delete(tmp_path: Path) -> None:
    repo = AppRepo(tmp_path / "shop")
    repo.publish(None, {"app.py": b"a"}, version=VERSION, note="first", login="etienne", when=NOW)
    repo.delete()
    assert not repo.folder.exists()
    repo.delete()


def test_a_repository_s_ids_are_sha1_whatever_git_s_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A commit id is 40 characters wherever Cliquet reads one, as a version or a revert's.
    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha256")
    repo = AppRepo(tmp_path / "apps" / "etienne" / "shop")
    commit = repo.publish(
        None, {"app.py": b"a"}, version=VERSION, note="first", login="etienne", when=NOW
    )
    assert len(commit) == 40
    shown = ["git", "rev-parse", "--show-object-format"]
    out = subprocess.run(shown, cwd=repo.folder, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "sha1"
