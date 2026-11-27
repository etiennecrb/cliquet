import base64
import subprocess
from typing import Any

import pytest

from cliquet.context import Caller
from cliquet.sandbox import version_id
from cliquet.transactions import TransactionEnded
from cliquet.worker import Committed
from cliquet_server.apps import PublishRefused
from cliquet_server.builder import Builder
from cliquet_server.builder.publishing import Publishing
from cliquet_server.executor import SandboxClient

from .server_helpers import MIGRATION, draft_files, fail_first_end, names, published

pytestmark = pytest.mark.slow

BAD_RULE = '''\
import csv


def migrate() -> None:
    """Write the same item twice."""
    with open("items.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "note"])
        writer.writeheader()
        writer.writerows([{"name": "MILK", "note": ""}, {"name": "MILK", "note": ""}])
'''


FAILS = '''\
def migrate() -> None:
    """Fail."""
    raise ValueError("no")
'''


RENAME = '''\
import csv
import os


def migrate() -> None:
    """Move items.csv to things.csv."""
    with open("items.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    with open("things.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "note"])
        writer.writeheader()
        writer.writerows(rows)
    os.remove("items.csv")
'''


LEAVE_BEHIND = '''\
import csv


def migrate() -> None:
    """Write things.csv but keep items.csv."""
    with open("items.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    with open("things.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "note"])
        writer.writeheader()
        writer.writerows(rows)
'''


GROW = '''\
def migrate() -> None:
    """Add an item with a long note."""
    with open("items.csv", "a") as f:
        f.write("bread," + "x" * 500 + "\\n")
'''


SAME_SIZE = '''\
from pathlib import Path


def migrate() -> None:
    """Write milk in capitals: the data keeps its size."""
    Path("items.csv").write_bytes(Path("items.csv").read_bytes().replace(b"milk", b"MILK"))
'''


def test_a_failed_end_at_a_publish_that_kept_app_py_ends_at_the_next_start(
    builder: Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    published(builder)
    owner = Caller("etienne", None)
    transaction = builder.apps.live("etienne", "tasks").runtime.begin(owner)
    fail_first_end(monkeypatch)
    code = builder.drafts.code("etienne", "tasks")
    with pytest.raises(OSError, match="disk full"):
        builder.apps.publish(
            "etienne", "tasks", code, note="Capitals", migration=MIGRATION.encode()
        )
    assert builder.apps.current("etienne", "tasks") is None

    # The same app.py, so only the publish's commit says the transaction is of the old one.
    live = builder.apps.live("etienne", "tasks")
    with pytest.raises(TransactionEnded):
        live.runtime.transaction_calls(owner, transaction)
    # A transaction begun with the version now live stays open when it starts again.
    again = live.runtime.begin(owner)
    builder.apps.forget("etienne", "tasks")
    live = builder.apps.live("etienne", "tasks")
    assert live.runtime.transaction_calls(owner, again)


def test_a_migration_rewrites_the_data_at_publish(builder: Builder) -> None:
    published(builder)
    builder.write_file("etienne", "tasks", "migrate.py", MIGRATION)
    code = builder.drafts.code("etienne", "tasks")
    version = version_id(code)

    builder.apps.publish("etienne", "tasks", code, note="Capitals", migration=MIGRATION.encode())

    assert names(builder) == ["MILK", "EGGS"]
    repo = builder.apps.repo("etienne", "tasks")
    assert repo.migration_names() == [f"0001-{version}.py"]
    head = repo.head()
    assert head is not None
    shown = subprocess.run(
        ["git", "show", "--name-only", "--format=", head],
        cwd=repo.folder,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "data/items.csv" in shown
    assert f"migrations/0001-{version}.py" in shown
    entry = builder.apps.live("etienne", "tasks").runtime.history(Caller("etienne", None))[0]
    # Cliquet's publish reads as its own, by its trailer, with the version it made live.
    assert (entry.action, entry.version) == ("app_publish", version)
    assert "items.csv" in entry.files


def test_a_second_migration_is_numbered(builder: Builder) -> None:
    published(builder)
    builder.publish_app("etienne", "tasks", "First")
    builder.write_file("etienne", "tasks", "migrate.py", MIGRATION)
    assert builder.publish_app("etienne", "tasks", "Capitals")["published"] is True
    builder.write_file("etienne", "tasks", "migrate.py", MIGRATION)
    assert builder.publish_app("etienne", "tasks", "Capitals again")["published"] is True

    migrations = builder.apps.repo("etienne", "tasks").migration_names()
    assert len(migrations) == 2
    assert migrations[1].startswith("0002-")


def test_a_result_that_breaks_a_rule_publishes_nothing(builder: Builder) -> None:
    published(builder)
    code = builder.drafts.code("etienne", "tasks")
    repo = builder.apps.repo("etienne", "tasks")
    before = repo.head()

    with pytest.raises(PublishRefused, match="Item names are unique"):
        builder.apps.publish("etienne", "tasks", code, note="Bad", migration=BAD_RULE.encode())

    assert repo.head() == before


# A rule of the template's that says what Cliquet checked the data for, once an item is done.
TOLD = b"""

@tasks.invariant("Nothing is done")
def nothing_done(ctx: InvariantContext) -> list[str]:
    return [f"done, as {ctx.action}"] if any(item.name == "DONE" for item in _items()) else []
"""
DONE = '''\
def migrate() -> None:
    """Add an item that is done."""
    with open("items.csv", "a") as f:
        f.write("DONE,\\n")
'''


def test_a_migration_s_change_is_checked_as_the_publish_that_keeps_it(builder: Builder) -> None:
    published(builder)
    code = builder.drafts.code("etienne", "tasks")
    code["app.py"] += TOLD

    # The name it has in the history, never that of an app's own action named migrate.
    with pytest.raises(PublishRefused, match="done, as app_publish"):
        builder.apps.publish("etienne", "tasks", code, note="Done", migration=DONE.encode())


def test_a_migration_that_fails_publishes_nothing(builder: Builder) -> None:
    published(builder)
    code = builder.drafts.code("etienne", "tasks")
    repo = builder.apps.repo("etienne", "tasks")
    before = repo.head()

    with pytest.raises(PublishRefused, match="ValueError"):
        builder.apps.publish("etienne", "tasks", code, note="Bad", migration=FAILS.encode())

    assert repo.head() == before


def counted_migrations(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count the migration's runs in the sandbox: one entry each."""
    runs: list[str] = []
    run = SandboxClient.run

    def counted(
        self: SandboxClient, login: str, app: str, version: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        if request["op"] == "migrate":
            runs.append(version)
        return run(self, login, app, version, request)

    monkeypatch.setattr(SandboxClient, "run", counted)
    return runs


def test_a_publish_runs_the_migration_once(
    builder: Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    published(builder)
    builder.write_file("etienne", "tasks", "migrate.py", MIGRATION)
    runs = counted_migrations(monkeypatch)

    answer = builder.publish_app("etienne", "tasks", "Capitals")

    assert answer["published"] is True, answer
    assert len(runs) == 1
    assert names(builder) == ["MILK", "EGGS"]


def test_a_publish_runs_the_migration_again_on_data_that_changed(
    builder: Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    published(builder)
    builder.write_file("etienne", "tasks", "migrate.py", MIGRATION)
    runs = counted_migrations(monkeypatch)
    checks = Publishing._checks

    def then_a_change(self: Publishing, login: str, app: str) -> Any:
        checked = checks(self, login, app)
        # Someone adds an item once the checks ran the migration.
        live = builder.apps.live("etienne", "tasks")
        outcome = live.runtime.call(Caller("etienne", None), "add_item", {"name": "tea"})
        assert isinstance(outcome, Committed), outcome
        return checked

    monkeypatch.setattr(Publishing, "_checks", then_a_change)
    answer = builder.publish_app("etienne", "tasks", "Capitals")

    assert answer["published"] is True, answer
    assert len(runs) == 2
    assert names(builder) == ["MILK", "EGGS", "TEA"]


def test_a_migration_cannot_write_outside(
    builder: Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    published(builder)
    code = builder.drafts.code("etienne", "tasks")
    repo = builder.apps.repo("etienne", "tasks")
    before = repo.head()
    run = SandboxClient.run

    def answers_outside(
        self: SandboxClient, login: str, app: str, version: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        if request["op"] == "migrate":
            return {"changes": {"../x": base64.b64encode(b"outside").decode()}}
        return run(self, login, app, version, request)

    monkeypatch.setattr(SandboxClient, "run", answers_outside)
    with pytest.raises(PublishRefused, match=r"a change to \.\./x, which the app doesn't declare"):
        builder.apps.publish("etienne", "tasks", code, note="Out", migration=MIGRATION.encode())

    assert repo.head() == before
    assert not (repo.folder / "x").exists()
    assert not (repo.folder.parent / "x").exists()


def test_a_migration_can_rename_a_file(builder: Builder) -> None:
    published(builder)
    app = builder.read_file("etienne", "tasks", "app.py")["text"].replace(
        'ITEMS = "items.csv"', 'ITEMS = "things.csv"'
    )
    tests = builder.read_file("etienne", "tasks", "tests/test_app.py")["text"].replace(
        "items.csv", "things.csv"
    )
    builder.write_file("etienne", "tasks", "app.py", app)
    builder.write_file("etienne", "tasks", "tests/test_app.py", tests)
    builder.write_file("etienne", "tasks", "migrate.py", RENAME)

    assert builder.publish_app("etienne", "tasks", "Rename the file")["published"] is True

    repo = builder.apps.repo("etienne", "tasks")
    assert repo.data_names() == ["things.csv"]
    assert names(builder) == ["milk", "eggs"]


def test_a_file_left_behind_is_refused(builder: Builder) -> None:
    published(builder)
    app = builder.read_file("etienne", "tasks", "app.py")["text"].replace(
        'ITEMS = "items.csv"', 'ITEMS = "things.csv"'
    )
    tests = builder.read_file("etienne", "tasks", "tests/test_app.py")["text"].replace(
        "items.csv", "things.csv"
    )
    builder.write_file("etienne", "tasks", "app.py", app)
    builder.write_file("etienne", "tasks", "tests/test_app.py", tests)
    builder.write_file("etienne", "tasks", "migrate.py", LEAVE_BEHIND)
    code = builder.drafts.code("etienne", "tasks")

    with pytest.raises(PublishRefused, match=r"items\.csv"):
        builder.apps.publish(
            "etienne", "tasks", code, note="Rename", migration=LEAVE_BEHIND.encode()
        )


def test_a_migration_needs_a_live_app(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    code = builder.drafts.code("etienne", "tasks")

    with pytest.raises(PublishRefused, match="a migration is for an app that is live"):
        builder.apps.publish("etienne", "tasks", code, note="First", migration=MIGRATION.encode())


def test_check_app_tries_the_migration_on_a_copy(builder: Builder) -> None:
    published(builder)
    builder.write_file("etienne", "tasks", "migrate.py", MIGRATION)

    checked = builder.check_app("etienne", "tasks")

    assert checked["ok"] is True
    assert checked["migration"] == "The migration rewrites: items.csv."
    assert names(builder) == ["milk", "eggs"]


def test_check_app_reports_a_bad_migration(builder: Builder) -> None:
    published(builder)
    builder.write_file("etienne", "tasks", "migrate.py", FAILS)

    checked = builder.check_app("etienne", "tasks")

    assert checked["ok"] is False
    assert any("ValueError" in problem for problem in checked["problems"])


def test_a_migration_before_the_first_publish_is_a_problem(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "migrate.py", MIGRATION)

    checked = builder.check_app("etienne", "tasks")

    assert checked["ok"] is False
    assert "migrate.py is for an app that is live" in checked["problems"][0]
    assert checked["data"] == "migrate.py is only for an app that is live: see the problems."


def test_check_app_and_publish_name_the_migration(builder: Builder) -> None:
    published(builder)
    without = builder.check_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "migrate.py", MIGRATION)

    checked = builder.check_app("etienne", "tasks")
    result = builder.publish_app("etienne", "tasks", "Capitals")

    assert without["data"] == "The live data is kept as it is."
    assert checked["data"] == "The migration rewrites: items.csv."
    assert result["data"] == "The migration rewrites: items.csv."


def test_publishing_runs_the_migration_once(builder: Builder) -> None:
    published(builder)
    builder.write_file("etienne", "tasks", "migrate.py", MIGRATION)
    result = builder.publish_app("etienne", "tasks", "Capitals")

    assert result["published"] is True
    assert names(builder) == ["MILK", "EGGS"]
    assert "migrate.py" not in draft_files(builder.drafts, "etienne", "tasks")
    assert builder.list_files("etienne", "tasks")["changed"] is False


def test_a_migration_that_leaves_too_much_data_publishes_nothing(
    builder: Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    published(builder)
    monkeypatch.setattr("cliquet.limits.MAX_APP_DATA", 300)
    code = builder.drafts.code("etienne", "tasks")
    repo = builder.apps.repo("etienne", "tasks")
    before = repo.head()

    with pytest.raises(PublishRefused, match="more than the 300 bytes an app keeps"):
        builder.apps.publish("etienne", "tasks", code, note="Grow", migration=GROW.encode())

    assert repo.head() == before
    # check_app says so too, from its copy of the data.
    builder.write_file("etienne", "tasks", "migrate.py", GROW)
    checked = builder.check_app("etienne", "tasks")
    assert checked["ok"] is False
    assert any("more than the 300 bytes an app keeps" in one for one in checked["problems"])


def test_first_data_over_the_cap_publishes_nothing(
    builder: Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder.create_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "data/items.csv", "name,note\n" + "milk,x\n" * 50)
    monkeypatch.setattr("cliquet.limits.MAX_APP_DATA", 300)
    code = builder.drafts.code("etienne", "tasks")

    with pytest.raises(PublishRefused, match="more than the 300 bytes an app keeps"):
        builder.apps.publish(
            "etienne", "tasks", code, note="First", data=builder.drafts.data("etienne", "tasks")
        )

    assert builder.apps.repo("etienne", "tasks").head() is None


def test_a_migration_that_doesnt_grow_the_data_passes_over_the_cap(
    builder: Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    published(builder)
    # The app is over the cap, as an app may be that was live before there was one.
    monkeypatch.setattr("cliquet.limits.MAX_APP_DATA", 5)
    code = builder.drafts.code("etienne", "tasks")

    builder.apps.publish("etienne", "tasks", code, note="Milk", migration=SAME_SIZE.encode())

    assert names(builder) == ["MILK", "eggs"]


def test_an_app_over_the_cap_still_publishes_new_code(
    builder: Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    published(builder)
    monkeypatch.setattr("cliquet.limits.MAX_APP_DATA", 5)
    code = builder.drafts.code("etienne", "tasks")
    code["app.py"] += b"\n# A new version.\n"

    live = builder.apps.publish("etienne", "tasks", code, note="New code")

    assert live.version == version_id(code)


def test_a_version_that_drops_a_data_file_needs_a_migration(builder: Builder) -> None:
    published(builder)
    app = builder.read_file("etienne", "tasks", "app.py")["text"].replace(
        'ITEMS = "items.csv"', 'ITEMS = "things.csv"'
    )
    tests = builder.read_file("etienne", "tasks", "tests/test_app.py")["text"].replace(
        "items.csv", "things.csv"
    )
    builder.write_file("etienne", "tasks", "app.py", app)
    builder.write_file("etienne", "tasks", "tests/test_app.py", tests)
    code = builder.drafts.code("etienne", "tasks")
    repo = builder.apps.repo("etienne", "tasks")
    before = repo.head()
    left = (
        "data/items.csv is in the live data, but this version doesn't declare it. Declare it "
        'again, or remove it with a migration: see read_guide("publishing").'
    )

    # A file kept but no longer declared could be declared again later, past the cap.
    with pytest.raises(PublishRefused) as refused:
        builder.apps.publish("etienne", "tasks", code, note="Rename")
    checked = builder.check_app("etienne", "tasks")

    assert refused.value.reasons == [left]
    assert repo.head() == before
    assert checked["ok"] is False and checked["problems"] == [left]
