from pathlib import Path

import pytest

from cliquet.files import (
    VERSION_KINDS,
    Kind,
    data_name_problem,
    declared_problem,
    declares,
    kind_of,
    matches,
    path_problem,
    versioned,
)
from cliquet.sandbox import bundle_of, version_id


@pytest.mark.parametrize(
    ("path", "kind", "counted"),
    [
        ("app.py", Kind.CODE, True),
        ("instructions.md", Kind.CODE, True),
        ("tests/test_app.py", Kind.CODE, True),
        ("tests/helpers_2.py", Kind.CODE, True),
        ("app.py.lock", Kind.LOCK, False),
        ("migrate.py", Kind.MIGRATION, False),
        ("data/items.csv", Kind.DATA, False),
        ("data/2026/a-1_2.txt", Kind.DATA, False),
        ("notes.txt", None, False),
        ("app.pyc", None, False),
        ("helpers.py", None, False),
        ("tests/a/b.py", None, False),
        ("tests/conftest.txt", None, False),
        ("tests/.py", None, False),
        ("tests/__pycache__/test_app.cpython-314.pyc", None, False),
        ("data/.git/config", None, False),
        ("data/../app.py", None, False),
        ("data/", None, False),
        ("../app.py", None, False),
        ("/etc/passwd", None, False),
        ("", None, False),
    ],
)
def test_each_path_has_one_kind(path: str, kind: Kind | None, counted: bool) -> None:
    assert kind_of(path) == kind
    assert versioned(path) is counted
    assert (path_problem(path) is None) is (kind is not None)
    assert (version_id({path: b"file"}) != version_id({})) is counted


def test_only_the_data_is_out_of_a_version() -> None:
    assert set(Kind) - VERSION_KINDS == {Kind.DATA}


def test_a_bad_data_name_says_why() -> None:
    problem = path_problem("data/a/.x")
    assert problem == data_name_problem("a/.x")
    assert problem is not None and "starts with a dot" in problem


@pytest.mark.parametrize(
    "entry",
    [
        "stock.csv",
        "2026/january.csv",
        "**/*.beancount",
        "2026/*.csv",
        "ledger/**/*.beancount",
        "*/notes.md",
        "*.csv",
        "notes-*.md",
        "**/ledger.beancount",
        "a/b/c/d/e/f/g/*.csv",
    ],
)
def test_a_name_or_a_pattern_may_be_declared(entry: str) -> None:
    assert declared_problem(entry) is None


@pytest.mark.parametrize(
    ("entry", "why"),
    [
        ("**/*", "its last part is only a wildcard"),
        ("2026/*", "its last part is only a wildcard"),
        ("*", "its last part is only a wildcard"),
        ("ledger/**", "its last part is only a wildcard"),
        ("a**/x.csv", "its ** is inside a part"),
        ("**.csv", "its ** is inside a part"),
        ("**/**/x.csv", "more than one ** or more than one *"),
        ("*/*.csv", "more than one ** or more than one *"),
        ("*.c*v", "more than one ** or more than one *"),
        ("*?.csv", "a character other than"),
        ("[ab]*.csv", "a character other than"),
        (".*.csv", "starts with a dot"),
        ("../*.csv", "starts with a dot"),
        ("**/.git/*.csv", "starts with a dot"),
        ("/*.csv", "it starts with /"),
        ("2026//*.csv", "an empty part"),
        ("*.csv/", "an empty part"),
        ("a/b/c/d/e/f/g/h/*.csv", "more than 8 parts"),
        ("*" + "a" * 200, "longer than 200 characters"),
        ("*" + "a" * 100, "longer than 100 characters"),
    ],
)
def test_a_bad_pattern_says_why(entry: str, why: str) -> None:
    problem = declared_problem(entry)
    assert problem is not None and why in problem
    assert problem.startswith(f"{entry!r} can't be") and "**/*.beancount" in problem


def test_a_declared_name_is_checked_as_a_data_file_s() -> None:
    assert declared_problem("a/.x") == data_name_problem("a/.x")


@pytest.mark.parametrize(
    ("entry", "name"),
    [
        ("ledger.beancount", "ledger.beancount"),
        ("**/*.beancount", "ledger.beancount"),
        ("**/*.beancount", "2026/01.beancount"),
        ("**/*.beancount", "a/b/c/d/e/f/g/x.beancount"),
        ("2026/*.csv", "2026/january.csv"),
        ("2026/*.csv", "2026/x.y.csv"),
        ("ledger/**/*.beancount", "ledger/main.beancount"),
        ("ledger/**/*.beancount", "ledger/2026/01.beancount"),
        ("*/notes.md", "work/notes.md"),
        ("notes-*.md", "notes-.md"),
        ("**/ledger.beancount", "2026/ledger.beancount"),
    ],
)
def test_a_pattern_matches_names_as_path_glob_does(entry: str, name: str) -> None:
    assert matches(entry, name)
    assert declares(["other.csv", entry], name)


@pytest.mark.parametrize(
    ("entry", "name"),
    [
        ("ledger.beancount", "2026/ledger.beancount"),
        ("**/*.beancount", "ledger.beancount.bak"),
        ("**/*.beancount", "ledger.Beancount"),
        ("**/*.beancount", ".cache/ledger.beancount"),
        ("**/*.beancount", ".beancount"),
        ("**/*.beancount", "a/b/c/d/e/f/g/h/x.beancount"),
        ("**/*.beancount", "a//x.beancount"),
        ("2026/*.csv", "january.csv"),
        ("2026/*.csv", "2026/q1/january.csv"),
        ("2026/*.csv", "2027/january.csv"),
        ("ledger/**/*.beancount", "main.beancount"),
        ("*/notes.md", "notes.md"),
        ("*/notes.md", "a/b/notes.md"),
        ("*.csv", "stock.csv-wal"),
        ("*.csv", "stock.csv-journal"),
    ],
)
def test_a_pattern_matches_no_other_name(entry: str, name: str) -> None:
    assert not matches(entry, name)
    assert not declares(["other.csv", entry], name)


def test_a_path_of_no_kind_lists_the_kinds() -> None:
    problem = path_problem("notes.txt")
    assert problem is not None
    for name in ("app.py", "instructions.md", "tests/<name>.py", "app.py.lock", "migrate.py"):
        assert name in problem


def test_bundle_of_takes_every_file_of_a_version_and_nothing_else(tmp_path: Path) -> None:
    for path in (
        "app.py",
        "instructions.md",
        "app.py.lock",
        "migrate.py",
        "tests/test_app.py",
        "tests/a/b.py",
        "tests/notes.txt",
        "notes.txt",
        "data/items.csv",
    ):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_bytes(path.encode())
    bundle = bundle_of(tmp_path)
    assert list(bundle) == [
        "app.py",
        "app.py.lock",
        "instructions.md",
        "migrate.py",
        "tests/test_app.py",
    ]
    assert all(kind_of(name) in VERSION_KINDS for name in bundle)
    assert bundle["tests/test_app.py"] == b"tests/test_app.py"
