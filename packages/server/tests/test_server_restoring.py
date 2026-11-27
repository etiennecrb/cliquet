import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from cliquet.context import Caller
from cliquet_server.apps import PublishRefused
from cliquet_server.builder import Builder, BuilderError

from .server_helpers import (
    MIGRATION,
    a_shop_member,
    act_as,
    csrf_for,
    git,
    names,
    published,
    signed_in,
)

pytestmark = pytest.mark.slow

OWNER = Caller("etienne", None)

# A query the second version of tasks adds.
COUNT = '''

@tasks.query
def count(ctx: QueryContext) -> int:
    """How many items are listed."""
    return len(_items())
'''

# An action the second version of tasks adds, that changes an item's line.
RENAME = '''

@tasks.action
def rename(ctx: ActionContext, old: str, new: str) -> None:
    """Rename an item."""
    rows = [Item(name=new if row.name == old else row.name, note=row.note) for row in _items()]
    with open(ITEMS, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "note"])
        writer.writeheader()
        writer.writerows(row.model_dump() for row in rows)
'''

# An action that writes a data file the first version of tasks doesn't declare.
EXTRA = '''

@tasks.action
def keep_extra(ctx: ActionContext) -> None:
    """Write extra.csv."""
    with open("extra.csv", "w") as f:
        f.write("kept\\n")
'''

# An action that writes a data file in a new folder, as a pattern such as notes/*.csv allows.
NOTE = '''

@tasks.action
def keep_note(ctx: ActionContext) -> None:
    """Write notes/kept.csv."""
    import os

    os.makedirs("notes", exist_ok=True)
    with open("notes/kept.csv", "w") as f:
        f.write("kept\\n")
'''

# A migration that adds an item: run twice, it would list bread twice, which the rule refuses.
BREAD = '''\
def migrate() -> None:
    """Add bread."""
    with open("items.csv", "a") as f:
        f.write("bread,\\n")
'''


def first_version(builder: Builder) -> dict[str, str]:
    """Publish tasks, and give its version as read_app_overview lists it."""
    published(builder)
    (version,) = builder.read_app_overview("etienne", "tasks")["versions"]
    return version


def publish_with(builder: Builder, added: str, migration: str | None = None) -> None:
    """Publish a new version of tasks, with `added` at the end of its app.py."""
    app_py = builder.drafts.code("etienne", "tasks")["app.py"].decode()
    builder.write_file("etienne", "tasks", "app.py", app_py + added)
    if migration is not None:
        builder.write_file("etienne", "tasks", "migrate.py", migration)
    answer = builder.publish_app("etienne", "tasks", "Next version")
    assert answer["published"] is True, answer


def tool_names(builder: Builder) -> set[str]:
    return {tool["name"] for tool in builder.read_app_overview("etienne", "tasks")["tools"]}


def head_message(builder: Builder) -> str:
    return git(builder.apps.repo("etienne", "tasks").folder, "log", "-1", "--format=%B")


def head_files(builder: Builder) -> list[str]:
    folder = builder.apps.repo("etienne", "tasks").folder
    return git(folder, "show", "--name-only", "--format=", "HEAD").split()


def test_restoring_a_version_without_a_migration(builder: Builder) -> None:
    first = first_version(builder)
    publish_with(builder, COUNT)
    live = builder.apps.live("etienne", "tasks")
    live.runtime.call(OWNER, "add_item", {"name": "bread"})
    assert "count" in tool_names(builder)

    restored = builder.restore_version("etienne", "tasks", first["version"])

    assert restored["restores"]["commit"] == first["commit"]
    assert [one["note"] for one in restored["undoes"]] == ["Next version"]
    assert restored["data"] == "The live data is kept as it is."
    assert "migrate.py" not in restored["files"]
    # Nothing is live until publish.
    assert "count" in tool_names(builder)
    preview = builder.start_preview("etienne", "tasks")
    assert "1 change was made to the data since" in preview["restore"]
    assert "may drop fields of the data it doesn't know" in preview["restore"]
    tried = builder.try_call("etienne", "tasks", "items")
    assert "bread" in str(tried["result"])

    answer = builder.publish_app("etienne", "tasks", "Back to the first version")

    assert answer["published"] is True, answer
    assert answer["restores"] == first["commit"]
    assert answer["version"] == first["version"]
    assert "count" not in tool_names(builder)
    assert names(builder) == ["milk", "eggs", "bread"]
    message = head_message(builder)
    assert f"App-Restores: {first['commit']}" in message
    assert f"App-Version: {first['version']}" in message
    # One new commit: the history before it is kept.
    versions = builder.read_app_overview("etienne", "tasks")["versions"]
    assert [one["note"] for one in versions] == [
        "Back to the first version",
        "Next version",
        "First version",
    ]
    assert versions[0]["restores"] == first["commit"]
    # The draft is the live version again.
    assert builder.drafts.restoring("etienne", "tasks") is None
    info = builder.apps.database.app("etienne", "tasks")
    assert info is not None and builder.workshop.changed("etienne", info) is False


def test_restoring_past_a_migration_puts_the_data_back_in_the_same_commit(
    builder: Builder,
) -> None:
    first = first_version(builder)
    publish_with(builder, COUNT, MIGRATION)
    assert names(builder) == ["MILK", "EGGS"]

    restored = builder.restore_version("etienne", "tasks", first["commit"][:7])
    put_back = "The data from before the publishes it undoes is put back: items.csv."
    assert restored["data"] == put_back
    checked = builder.check_app("etienne", "tasks")
    assert checked["ok"] is True, checked
    assert checked["data"] == restored["data"]
    preview = builder.start_preview("etienne", "tasks")
    assert "0 changes were made to the data since" in preview["restore"]
    assert "milk" in str(builder.try_call("etienne", "tasks", "items")["result"])
    assert names(builder) == ["MILK", "EGGS"]

    answer = builder.publish_app("etienne", "tasks", "Back to the first version")

    assert answer["published"] is True, answer
    assert names(builder) == ["milk", "eggs"]
    assert "count" not in tool_names(builder)
    files = head_files(builder)
    assert "app.py" in files and "data/items.csv" in files
    # The restore runs no migration, and keeps none.
    assert not any(name.startswith("migrations/") for name in files)
    # It reads in the history as a publish that restored a version.
    entry = builder.apps.live("etienne", "tasks").runtime.history(OWNER)[0]
    assert (entry.action, entry.restores) == ("app_publish", first["commit"])


def test_restoring_past_a_migration_a_later_change_touched_is_refused(
    builder: Builder,
) -> None:
    first = first_version(builder)
    publish_with(builder, RENAME, MIGRATION)
    live = builder.apps.live("etienne", "tasks")
    live.runtime.call(OWNER, "rename", {"old": "MILK", "new": "Butter"})
    repo = builder.apps.repo("etienne", "tasks")
    head = repo.head()
    draft = builder.drafts.code("etienne", "tasks")

    with pytest.raises(BuilderError) as refused:
        builder.restore_version("etienne", "tasks", first["commit"])

    assert "1 change made since touched the same lines" in str(refused.value)
    assert "Nothing was changed" in str(refused.value)
    assert builder.drafts.code("etienne", "tasks") == draft
    assert builder.drafts.restoring("etienne", "tasks") is None
    # Publish refuses it the same way, and nothing changes.
    code = repo.code(first["commit"])
    with pytest.raises(PublishRefused) as again:
        builder.apps.publish("etienne", "tasks", code, note="Back", restores=first["commit"])
    assert "1 change made since touched the same lines" in str(again.value)
    assert repo.head() == head
    assert names(builder) == ["Butter", "EGGS"]


def test_a_restore_is_refused_while_the_draft_has_changes(builder: Builder) -> None:
    first = first_version(builder)
    publish_with(builder, COUNT)
    builder.write_file("etienne", "tasks", "instructions.md", "Changed.\n")

    with pytest.raises(BuilderError) as refused:
        builder.restore_version("etienne", "tasks", first["version"])

    assert "changes that aren't published" in str(refused.value)
    assert "discard_draft" in str(refused.value)
    assert builder.drafts.code("etienne", "tasks")["instructions.md"] == b"Changed.\n"

    # The live version itself has nothing to restore.
    builder.discard_draft("etienne", "tasks")
    live = builder.read_app_overview("etienne", "tasks")["versions"][0]
    with pytest.raises(BuilderError, match="nothing to restore"):
        builder.restore_version("etienne", "tasks", live["commit"])
    with pytest.raises(BuilderError, match="read_app_overview"):
        builder.restore_version("etienne", "tasks", "0123456789abcdef")


def test_a_restored_version_that_doesn_t_declare_a_live_file_is_refused(
    builder: Builder,
) -> None:
    first = first_version(builder)
    app_py = builder.drafts.code("etienne", "tasks")["app.py"].decode()
    declared = app_py.replace("files=[ITEMS]", 'files=[ITEMS, "extra.csv"]')
    assert declared != app_py
    builder.write_file("etienne", "tasks", "app.py", declared + EXTRA)
    assert builder.publish_app("etienne", "tasks", "Extra")["published"] is True
    builder.apps.live("etienne", "tasks").runtime.call(OWNER, "keep_extra", {})
    head = builder.apps.repo("etienne", "tasks").head()
    builder.restore_version("etienne", "tasks", first["version"])

    answer = builder.publish_app("etienne", "tasks", "Back")

    assert answer["published"] is False
    details = " ".join(check["detail"] for check in answer["checks"])
    assert "data/extra.csv is in the live data, but the restored version doesn't declare it" in (
        details
    )
    assert builder.apps.repo("etienne", "tasks").head() == head
    assert "keep_extra" in tool_names(builder)


def test_a_restored_version_of_names_is_refused_once_a_pattern_s_file_is_live(
    builder: Builder,
) -> None:
    first = first_version(builder)
    app_py = builder.drafts.code("etienne", "tasks")["app.py"].decode()
    declared = app_py.replace("files=[ITEMS]", 'files=[ITEMS, "notes/*.csv"]')
    assert declared != app_py
    builder.write_file("etienne", "tasks", "app.py", declared + NOTE)
    assert builder.publish_app("etienne", "tasks", "Notes")["published"] is True
    builder.apps.live("etienne", "tasks").runtime.call(OWNER, "keep_note", {})
    builder.restore_version("etienne", "tasks", first["version"])

    answer = builder.publish_app("etienne", "tasks", "Back")

    assert answer["published"] is False
    details = " ".join(check["detail"] for check in answer["checks"])
    assert "data/notes/kept.csv is in the live data, but the restored version doesn't" in details


def test_the_restored_version_s_migration_doesn_t_run_again(builder: Builder) -> None:
    first_version(builder)
    publish_with(builder, COUNT, BREAD)
    assert names(builder) == ["milk", "eggs", "bread"]
    second = builder.read_app_overview("etienne", "tasks")["versions"][0]
    builder.write_file("etienne", "tasks", "instructions.md", "A third version.\n")
    assert builder.publish_app("etienne", "tasks", "Third")["published"] is True
    migrations = builder.apps.repo("etienne", "tasks").migration_names()

    restored = builder.restore_version("etienne", "tasks", second["commit"])

    assert restored["data"] == "The live data is kept as it is."
    assert builder.drafts.migration("etienne", "tasks") is None
    answer = builder.publish_app("etienne", "tasks", "Back to the second version")
    assert answer["published"] is True, answer
    # Run again, the migration would have listed bread twice.
    assert names(builder) == ["milk", "eggs", "bread"]
    assert builder.apps.repo("etienne", "tasks").migration_names() == migrations
    instructions = builder.drafts.code("etienne", "tasks")["instructions.md"]
    assert b"A third version" not in instructions


def test_an_edit_makes_a_restore_a_version_of_its_own(builder: Builder) -> None:
    first = first_version(builder)
    publish_with(builder, COUNT)
    builder.restore_version("etienne", "tasks", first["version"])
    assert builder.drafts.restoring("etienne", "tasks") == first["commit"]
    # Restoring again replaces the restore: it was no change of the person's.
    builder.restore_version("etienne", "tasks", first["commit"])

    builder.write_file("etienne", "tasks", "instructions.md", "Edited.\n")

    assert builder.drafts.restoring("etienne", "tasks") is None
    assert "restore" not in builder.start_preview("etienne", "tasks")


HISTORY = "/manage/etienne/shop/history"

# A query the second version of the shop adds.
STOCK_COUNT = '''

@shop.query(roles=["owner"])
def product_count(ctx: QueryContext) -> int:
    """How many products there are."""
    return 0
'''


def second_shop_version(server: Starlette) -> str:
    """Publish a second version of etienne/shop, and give the commit of the first."""
    apps = server.state.apps
    repo = apps.repo("etienne", "shop")
    first = repo.published()
    code = repo.code()
    code["app.py"] += STOCK_COUNT.encode()
    apps.publish("etienne", "shop", code, note="Count")
    return first


def shop_tools(server: Starlette) -> set[str]:
    manifest = server.state.apps.live("etienne", "shop").runtime.manifest
    return {query["name"] for query in manifest["queries"]}


def test_the_console_lists_the_publishes_and_restores_one(server: Starlette) -> None:
    first = second_shop_version(server)
    assert "product_count" in shop_tools(server)

    with signed_in(server) as client:
        page = client.get(HISTORY).text
        # Each publish is listed, the live one first, and only the others may be restored.
        assert "Versions" in page and "Count" in page
        assert page.count("Restore this version") == 1
        assert f'action="/manage/etienne/shop/versions/{first}/restore"' in page
        assert 'href="/build/shop/preview"' in page
        # Without the form's token, nothing is restored.
        expired = client.post(f"/manage/etienne/shop/versions/{first}/restore", data={})
        assert expired.status_code == 403
        assert "product_count" in shop_tools(server)

        restored = client.post(
            f"/manage/etienne/shop/versions/{first}/restore",
            data={"csrf": csrf_for(server, client)},
            follow_redirects=False,
        )
        assert restored.status_code == 303, restored.text
        assert restored.headers["location"] == f"{HISTORY}?notice=restored"
        after = client.get(restored.headers["location"]).text

    assert "is live again" in after
    assert "product_count" not in shop_tools(server)
    publishes = server.state.apps.repo("etienne", "shop").publishes()
    assert publishes[0].restores == first
    assert publishes[0].note.startswith("Restore version")
    # The draft is the live version again.
    assert server.state.builder.drafts.restoring("etienne", "shop") is None


def test_the_console_says_why_a_restore_is_refused(server: Starlette) -> None:
    first = second_shop_version(server)
    builder = server.state.builder
    builder.write_file("etienne", "shop", "instructions.md", "Changed.\n")

    with signed_in(server) as client:
        refused = client.post(
            f"/manage/etienne/shop/versions/{first}/restore",
            data={"csrf": csrf_for(server, client)},
        )

    assert refused.status_code == 409
    assert "Not restored" in refused.text and "discard them first" in refused.text
    assert "product_count" in shop_tools(server)


def test_a_member_can_t_restore_a_version(server: Starlette) -> None:
    first = second_shop_version(server)
    sam = a_shop_member(server)

    with TestClient(server) as client:
        act_as(server, client, sam)
        history = client.get(HISTORY)
        restored = client.post(
            f"/manage/etienne/shop/versions/{first}/restore",
            data={"csrf": csrf_for(server, client)},
        )

    assert history.status_code == 404
    assert restored.status_code == 404
    assert "product_count" in shop_tools(server)


def test_an_edit_during_a_restore_s_publish_is_refused_in_the_same_words(
    builder: Builder, monkeypatch
) -> None:
    first = first_version(builder)
    publish_with(builder, COUNT)
    builder.restore_version("etienne", "tasks", first["version"])
    client = builder.apps.executor.sandbox("etienne")
    real = type(client).test

    def test(self, login: str, app: str, *args, **kwargs):
        # The same text again: the code is as checked, but the draft is no restore now.
        source = builder.read_file("etienne", "tasks", "app.py")["text"]
        builder.write_file("etienne", "tasks", "app.py", source)
        return real(self, login, app, *args, **kwargs)

    monkeypatch.setattr(type(client), "test", test)
    head = builder.apps.repo("etienne", "tasks").head()
    with pytest.raises(BuilderError) as error:
        builder.publish_app("etienne", "tasks", "Back to the first")

    assert str(error.value) == (
        "The draft changed while publishing, so nothing was published. Call publish_app again."
    )
    assert builder.apps.repo("etienne", "tasks").head() == head
