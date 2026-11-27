import shutil
import subprocess
from pathlib import Path

import pytest

from cliquet_server.apps import PublishRefused, UnknownApp
from cliquet_server.db import AppExists, Right, may
from cliquet_server.executor import BuildProblems

from .server_helpers import APPS, NOW, STAFF, example_code, fail_first_end


def git(folder: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=folder, capture_output=True, text=True, check=True
    ).stdout


def test_importing_an_example(shop, apps) -> None:
    assert shop.info.published_at is not None
    assert shop.info.description.startswith("Stock for a small tea shop")
    assert len(shop.version) == 16
    assert shop.runtime.manifest["name"] == "shop"
    assert shop.instructions
    folder = apps.repo("etienne", "shop").folder
    assert "tool.uv.sources" not in (folder / "app.py").read_text()
    assert (folder / "app.py.lock").read_bytes()
    assert (folder / "data" / "products.csv").is_file()
    assert git(folder, "log", "-1", "--format=%s").strip() == "Publish: Imported from stock"


def test_an_imported_app_answers(shop) -> None:
    assert len(shop.runtime.query_json(STAFF, "stock", {})) == 5


def test_live_is_built_once(shop, apps) -> None:
    assert apps.live("etienne", "shop") is shop
    apps.forget("etienne", "shop")
    assert apps.current("etienne", "shop") is None
    assert apps.live("etienne", "shop") is not shop


def test_an_app_that_is_not_published_is_unknown(apps, database) -> None:
    with pytest.raises(UnknownApp):
        apps.live("etienne", "nope")
    database.create_app("etienne", "draft", NOW)
    with pytest.raises(UnknownApp):
        apps.live("etienne", "draft")


def test_a_taken_name_is_refused(shop, apps) -> None:
    with pytest.raises(AppExists):
        apps.import_folder(APPS / "stock", "etienne", "shop")


def test_a_bad_name_is_refused(apps, tmp_path: Path) -> None:
    folder = tmp_path / "My Shop"
    folder.mkdir()
    with pytest.raises(ValueError):
        apps.import_folder(folder, "etienne")


def test_data_that_breaks_a_rule_is_refused(apps, tmp_path: Path) -> None:
    folder = tmp_path / "bad"
    shutil.copytree(APPS / "stock", folder)
    movements = folder / "data" / "movements.csv"
    movements.write_text(
        movements.read_text() + "2026-11-10,POT-GLS,-99,sale,R-9999/POT-GLS,24.00\n"
    )
    with pytest.raises(PublishRefused) as error:
        apps.import_folder(folder, "etienne", "bad")
    assert "Stock never goes below zero" in str(error.value)
    assert apps.database.app("etienne", "bad") is None
    assert not apps.repo("etienne", "bad").folder.exists()


def test_an_invariant_that_fails_on_the_data_is_refused(apps, tmp_path: Path) -> None:
    folder = tmp_path / "failing"
    shutil.copytree(APPS / "stock", folder)
    with (folder / "app.py").open("a") as source:
        source.write(
            '\n\n@shop.invariant("Never checked")\n'
            "def fails(ctx: InvariantContext) -> list[str]:\n"
            '    raise RuntimeError("it went boom")\n'
        )
    with pytest.raises(PublishRefused) as error:
        apps.import_folder(folder, "etienne", "failing")
    assert "An invariant failed on the data: RuntimeError: it went boom" in str(error.value)
    assert apps.database.app("etienne", "failing") is None


def test_a_failed_import_grants_its_new_owner_nothing(apps, tmp_path: Path) -> None:
    folder = tmp_path / "failing"
    shutil.copytree(APPS / "stock", folder)
    with (folder / "app.py").open("a") as source:
        source.write(
            '\n\n@shop.invariant("Never checked")\n'
            "def fails(ctx: InvariantContext) -> list[str]:\n"
            '    raise RuntimeError("it went boom")\n'
        )
    with pytest.raises(PublishRefused):
        apps.import_folder(folder, "newcomer", "failing")
    person = apps.database.person_by_login("newcomer")
    assert person is not None and not may(person, Right.BUILD)


def test_an_app_with_problems_is_refused(apps, tmp_path: Path) -> None:
    folder = tmp_path / "broken"
    shutil.copytree(APPS / "stock", folder)
    source = (folder / "app.py").read_text()
    (folder / "app.py").write_text(
        source.replace(
            '"""Stock for a small tea shop: products, deliveries and sales, kept by an agent."""\n',
            "",
        )
    )
    with pytest.raises(BuildProblems):
        apps.import_folder(folder, "etienne", "broken")
    assert apps.database.app("etienne", "broken") is None
    assert not apps.repo("etienne", "broken").folder.exists()


def test_publishing_a_new_version(shop, apps) -> None:
    repo = apps.repo("etienne", "shop")
    head = repo.head()
    data = repo.data(shop.runtime.manifest["files"])
    code = repo.code()
    code["app.py"] += b"\n# a comment\n"
    live = apps.publish("etienne", "shop", code, note="A comment")
    assert live.version != shop.version
    assert repo.head() != head
    assert repo.data(live.runtime.manifest["files"]) == data
    assert apps.current("etienne", "shop") is live


def test_a_new_version_whose_transactions_don_t_end_is_not_served(
    shop, apps, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = fail_first_end(monkeypatch)
    code = apps.repo("etienne", "shop").code()
    code["app.py"] += b"\n# a comment\n"
    with pytest.raises(OSError, match="disk full"):
        apps.publish("etienne", "shop", code, note="A comment")

    assert len(failed) == 1
    assert apps.current("etienne", "shop") is None
    # The next call builds the new version again, rather than serve the one that failed.
    live = apps.live("etienne", "shop")
    assert live.runtime is not failed[0]
    assert live.runtime is not shop.runtime


def test_publishing_the_same_code_commits_nothing(shop, apps) -> None:
    repo = apps.repo("etienne", "shop")
    head = repo.head()
    apps.publish("etienne", "shop", repo.code(), note="Same")
    assert repo.head() == head


def test_first_data_must_be_declared(apps, database) -> None:
    database.create_app("etienne", "fresh", NOW)
    with pytest.raises(PublishRefused) as error:
        apps.publish("etienne", "fresh", example_code(), note="first", data={"other.csv": b""})
    assert "other.csv" in str(error.value)


def test_data_is_for_the_first_publish_only(shop, apps) -> None:
    with pytest.raises(PublishRefused) as error:
        apps.publish("etienne", "shop", apps.repo("etienne", "shop").code(), note="x", data={})
    assert "the app is live" in str(error.value)
