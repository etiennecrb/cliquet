import contextlib
import copy
import io
import threading

import anyio
import pytest
from fastmcp import Client as BuilderClient
from fastmcp.exceptions import ToolError

from cliquet.context import Caller
from cliquet.runtime import Committed, InvalidArguments
from cliquet_server.apps import left_behind
from cliquet_server.builder import Builder, BuilderError
from cliquet_server.builder.errors import LiveData
from cliquet_server.builder.publishing import manifest_changes
from cliquet_server.executor import BuildProblems, SandboxError

from .server_helpers import STAFF, Watched, draft_files, me_server

pytestmark = pytest.mark.slow


def manifest(
    *,
    actions=(),
    queries=(),
    views=(),
    rules=(),
    roles=(),
    files=(),
) -> dict:
    return {
        "actions": list(actions),
        "queries": list(queries),
        "views": list(views),
        "invariants": [{"name": name, "description": text} for name, text in rules],
        "roles": list(roles),
        "files": list(files),
    }


def tool(name: str, roles=None, arguments=None, result=None) -> dict:
    return {
        "name": name,
        "description": name,
        "roles": roles,
        "input_schema": arguments or {},
        "output_schema": result or {},
    }


def test_a_first_version_lists_everything() -> None:
    new = manifest(
        actions=[tool("add", arguments={"properties": {"name": {}}})],
        rules=[("unique", "Names are unique")],
    )

    changes = manifest_changes(None, new)

    assert changes[0] == "This is the app's first version."
    assert "New action add, for everyone." in changes
    assert "New rule: Names are unique." in changes


def test_tools_added_removed_and_changed() -> None:
    add = tool("add", arguments={"properties": {"name": {}}})
    assert manifest_changes(manifest(), manifest(actions=[add])) == [
        "New action add, for everyone."
    ]
    assert manifest_changes(manifest(actions=[add]), manifest()) == ["Removed action add."]
    assert manifest_changes(
        manifest(actions=[add]),
        manifest(actions=[{**add, "input_schema": {"properties": {"title": {}}}}]),
    ) == ["The arguments of action add changed."]
    assert manifest_changes(
        manifest(queries=[tool("total", result={"type": "number"})]),
        manifest(queries=[tool("total", result={"type": "string"})]),
    ) == ["The result of query total changed."]
    assert manifest_changes(
        manifest(actions=[add]), manifest(actions=[{**add, "output_schema": {"type": "integer"}}])
    ) == ["The result of action add changed."]
    # A version from before actions had results has no output_schema: it returned None.
    older = {k: v for k, v in add.items() if k != "output_schema"}
    assert manifest_changes(
        manifest(actions=[older]), manifest(actions=[{**add, "output_schema": None}])
    ) == ["No change to the tools, the rules, the roles or the data files."]
    assert manifest_changes(
        manifest(actions=[add]), manifest(actions=[{**add, "roles": ["owner"]}])
    ) == ["Action add is now for owner. It was for everyone."]


def test_a_removed_rule_is_flagged() -> None:
    changes = manifest_changes(
        manifest(rules=[("unique", "Names are unique")]),
        manifest(),
    )

    assert changes == ["Removed rule: Names are unique. Changes it refused will now pass."]


def test_roles_files_and_reworded_rules() -> None:
    assert manifest_changes(manifest(), manifest(roles=["owner"])) == ["New role owner."]
    assert manifest_changes(manifest(roles=["owner"]), manifest()) == ["Removed role owner."]
    assert manifest_changes(manifest(), manifest(files=["items.csv"])) == [
        "New data file items.csv."
    ]
    assert manifest_changes(manifest(files=["items.csv"]), manifest()) == [
        "The app no longer keeps items.csv."
    ]
    assert manifest_changes(
        manifest(rules=[("unique", "Names are unique")]),
        manifest(rules=[("unique", "Every name is unique")]),
    ) == ["Rule reworded: Every name is unique. It was: Names are unique."]


def test_a_pattern_of_data_files_says_it_matches() -> None:
    assert manifest_changes(
        manifest(files=["ledger.beancount"]), manifest(files=["**/*.beancount"])
    ) == [
        "New data files matching **/*.beancount.",
        "The app no longer keeps ledger.beancount.",
    ]
    assert manifest_changes(manifest(files=["2026/*.csv"]), manifest(files=["items.csv"])) == [
        "New data file items.csv.",
        "The app no longer keeps the files matching 2026/*.csv.",
    ]


def test_a_live_file_a_pattern_matches_is_not_left_behind() -> None:
    live = ["2026/01.beancount", "ledger.beancount", "notes.txt"]
    assert left_behind(live, ["notes.txt", "**/*.beancount"]) == []
    assert left_behind(live, ["notes.txt", "*.beancount"]) == [
        "data/2026/01.beancount is in the live data, but this version doesn't declare it. "
        'Declare it again, or remove it with a migration: see read_guide("publishing").'
    ]
    (restored,) = left_behind(live, ["**/*.beancount"], restored=True)
    assert restored.startswith("data/notes.txt is in the live data, but the restored version")


def test_no_change() -> None:
    same = manifest(actions=[tool("add")], rules=[("unique", "Names are unique")])

    assert manifest_changes(same, same) == [
        "No change to the tools, the rules, the roles or the data files."
    ]


FAILING_TEST = "def test_wrong() -> None:\n    assert 1 == 2\n"


def test_publish_in_one_step(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "tests/test_extra.py", FAILING_TEST)

    stopped = builder.publish_app("etienne", "tasks", "First version")

    assert stopped["published"] is False
    assert "code" not in stopped
    assert [check["ok"] for check in stopped["checks"]] == [True, False]
    assert "test_wrong" in stopped["checks"][-1]["detail"]
    assert stopped["next"] == "Fix this, then call publish_app again. Nothing was published."
    assert builder.read_app_overview("etienne", "tasks")["published"] is False

    builder.delete_file("etienne", "tasks", "tests/test_extra.py")
    result = builder.publish_app("etienne", "tasks", "First version")

    assert result["published"] is True
    assert "code" not in result
    assert result["changes"][0] == "This is the app's first version."
    assert result["data"] == "The app starts with no data."
    assert result["assistant_url"] == "http://127.0.0.1:8800/me/mcp"
    assert result["agent_url"] == "http://127.0.0.1:8800/apps/etienne/tasks/mcp"
    assert result["download_url"] == "http://127.0.0.1:8800/manage/etienne/tasks/download"
    assert "assistant_url" in result["next"] and "agent_url" in result["next"]
    assert result["web_urls"] == {"-": "http://127.0.0.1:8800/apps/etienne/tasks/-"}
    # An app without roles has one page to give, named by the app.
    assert "first: [Tasks](http://127.0.0.1:8800/apps/etienne/tasks). Present" in result["next"]
    assert "/tasks/-" not in result["next"]
    live = builder.apps.live("etienne", "tasks")
    assert live.runtime.query_json(Caller("etienne", None), "items", {}) == []
    assert builder.list_files("etienne", "tasks")["changed"] is False


def test_check_app_says_what_changes(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "data/items.csv", "name,note\nMilk,one\n")

    checked = builder.check_app("etienne", "tasks")

    assert checked["ok"] is True
    assert checked["changes"][0] == "This is the app's first version."
    assert checked["data"] == "First data: items.csv."
    assert "tests" not in checked
    assert builder.read_app_overview("etienne", "tasks")["published"] is False

    builder.publish_app("etienne", "tasks", "First version")
    builder.write_file(
        "etienne",
        "tasks",
        "migrate.py",
        "from pathlib import Path\n\n\ndef migrate() -> None:\n"
        '    """Write Milk in capitals."""\n'
        "    path = Path('items.csv')\n"
        "    path.write_text(path.read_text().replace('Milk', 'MILK'))\n",
    )
    again = builder.check_app("etienne", "tasks")
    assert again["ok"] is True, again["problems"]
    assert again["changes"] == ["No change to the tools, the rules, the roles or the data files."]
    assert again["data"] == "The migration rewrites: items.csv."
    builder.delete_file("etienne", "tasks", "migrate.py")
    assert builder.check_app("etienne", "tasks")["data"] == "The live data is kept as it is."


def test_a_changed_action_result_is_reported(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.publish_app("etienne", "tasks", "First version")
    source = builder.read_file("etienne", "tasks", "app.py")["text"]
    old = ') -> None:\n    """Add an item to the list."""\n'
    assert old in source
    returning = source.replace(old, old.replace("None", "Item")).replace(
        "        writer.writerows(row.model_dump() for row in rows)\n",
        "        writer.writerows(row.model_dump() for row in rows)\n    return rows[-1]\n",
    )
    builder.write_file("etienne", "tasks", "app.py", returning)

    checked = builder.check_app("etienne", "tasks")

    assert checked["ok"] is True, checked["problems"]
    assert checked["changes"] == ["The result of action add_item changed."]


def test_check_app_says_nothing_of_changes_when_it_can_t_build(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.edit_file(
        "etienne", "tasks", "app.py", '"""Tasks: a list of items, kept by an agent."""', ""
    )

    checked = builder.check_app("etienne", "tasks")

    assert checked["ok"] is False
    assert "changes" not in checked and "data" not in checked


def test_first_data_goes_live_with_the_app(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "data/items.csv", "name,note\nMilk,one\n")

    result = builder.publish_app("etienne", "tasks", "First version")

    assert result["published"] is True
    assert result["data"] == "First data: items.csv."
    live = builder.apps.live("etienne", "tasks")
    assert live.runtime.query_json(Caller("etienne", None), "items", {}) == [
        {"name": "Milk", "note": "one"}
    ]
    assert "data/items.csv" not in draft_files(builder.drafts, "etienne", "tasks")


LIVE_ITEMS = (
    "data/items.csv is live data. Change it through the app's actions, or with a migration: "
    'see read_guide("publishing").'
)


def during_the_first_publish(builder: Builder, monkeypatch, write) -> list[BuilderError]:
    """Publish tasks, its draft holding items.csv, while `write` runs in another thread: what
    `write` was refused with. Asserts the publish went live with the first items only."""
    builder.create_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "data/items.csv", "name,note\nMilk,one\n")
    # Set once the write is done, or waits for the draft's lock.
    moved = threading.Event()
    builder.drafts._locks[("etienne", "tasks")] = Watched(moved)  # ty: ignore[invalid-assignment]
    refused: list[BuilderError] = []

    def send() -> None:
        try:
            write()
        except BuilderError as error:
            refused.append(error)
        finally:
            moved.set()

    sender = threading.Thread(target=send)
    real = builder.apps.publish

    def publish(*args, **kwargs):
        sender.start()
        assert moved.wait(timeout=30)
        return real(*args, **kwargs)

    monkeypatch.setattr(builder.apps, "publish", publish)
    result = builder.publish_app("etienne", "tasks", "First version")
    sender.join(timeout=30)

    assert not sender.is_alive()
    assert result["published"] is True
    live = builder.apps.repo("etienne", "tasks").data(["items.csv"])["items.csv"]
    assert live == b"name,note\nMilk,one\n"
    assert "data/items.csv" not in draft_files(builder.drafts, "etienne", "tasks")
    return refused


def test_an_upload_during_the_first_publish_is_refused(builder: Builder, monkeypatch) -> None:
    content = b"name,note\nTea,two\n"

    def upload() -> None:
        # The person sends the file while the app goes live.
        info = builder.workshop.find("etienne", "tasks")
        builder.upload(info, "data/items.csv", io.BytesIO(content), len(content))

    refused = during_the_first_publish(builder, monkeypatch, upload)

    # The file isn't lost without a word: it waited for the publish, then was refused.
    assert [str(error) for error in refused] == [LIVE_ITEMS]


def test_a_data_write_during_the_first_publish_is_refused(builder: Builder, monkeypatch) -> None:
    def write() -> None:
        builder.write_file("etienne", "tasks", "data/items.csv", "name,note\nTea,two\n")

    refused = during_the_first_publish(builder, monkeypatch, write)

    assert [str(error) for error in refused] == [LIVE_ITEMS]


def test_a_publish_during_another_s_checks_is_refused(builder: Builder, monkeypatch) -> None:
    builder.create_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "data/items.csv", "name,note\nMilk,one\n")
    checks = builder.publishing._checks

    def checked(login: str, app: str):
        found = checks(login, app)
        assert found.build is not None
        # Another call publishes the app first.
        builder.apps.publish(login, app, found.build.code, note="Other", data={})
        return found

    monkeypatch.setattr(builder.publishing, "_checks", checked)

    with pytest.raises(BuilderError) as error:
        builder.publish_app("etienne", "tasks", "First version")
    assert str(error.value) == (
        "tasks was published by another call during this one's checks, so nothing was "
        "published. Call publish_app again."
    )
    assert builder.apps.repo("etienne", "tasks").data(["items.csv"]) == {}


CAPITALS = (
    "from pathlib import Path\n\n\ndef migrate() -> None:\n"
    '    """Write Milk in capitals."""\n'
    "    path = Path('items.csv')\n"
    "    path.write_text(path.read_text().replace('Milk', 'MILK'))\n"
)


def edited_during_the_tests(
    builder: Builder, monkeypatch, path: str, content: str
) -> tuple[str, dict]:
    """Publish tasks while an edit to `path` is made as its tests run, and give why the
    publish was refused, and the live version's code and data."""
    client = builder.apps.executor.sandbox("etienne")
    real = type(client).test

    def test(self, login: str, app: str, *args, **kwargs):
        # The assistant edits the draft while the tests run.
        builder.write_file("etienne", "tasks", path, content)
        return real(self, login, app, *args, **kwargs)

    monkeypatch.setattr(type(client), "test", test)
    repo = builder.apps.repo("etienne", "tasks")
    head = repo.head()
    with pytest.raises(BuilderError) as error:
        builder.publish_app("etienne", "tasks", "Second version")
    monkeypatch.setattr(type(client), "test", real)
    assert repo.head() == head
    return str(error.value), repo.code()


def test_a_code_edit_during_the_publish_s_tests_is_kept(builder: Builder, monkeypatch) -> None:
    builder.create_app("etienne", "tasks")
    builder.publish_app("etienne", "tasks", "First version")
    source = builder.read_file("etienne", "tasks", "app.py")["text"]
    edited = source + "\n# Edited while the tests ran.\n"

    refused, live = edited_during_the_tests(builder, monkeypatch, "app.py", edited)

    assert refused == (
        "The draft changed while publishing, so nothing was published. Call publish_app again."
    )
    assert builder.read_file("etienne", "tasks", "app.py")["text"] == edited
    assert live["app.py"].decode() == source
    # Published again, the edit goes live.
    assert builder.publish_app("etienne", "tasks", "Second version")["published"] is True
    assert builder.apps.repo("etienne", "tasks").code()["app.py"].decode() == edited


def test_a_migration_edit_during_the_publish_s_tests_is_kept(builder: Builder, monkeypatch) -> None:
    builder.create_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "data/items.csv", "name,note\nMilk,one\n")
    builder.publish_app("etienne", "tasks", "First version")
    builder.write_file("etienne", "tasks", "migrate.py", CAPITALS)
    edited = CAPITALS.replace("'MILK'", "'Oat milk'")

    refused, _ = edited_during_the_tests(builder, monkeypatch, "migrate.py", edited)

    assert refused == (
        "The draft changed while publishing, so nothing was published. Call publish_app again."
    )
    assert builder.read_file("etienne", "tasks", "migrate.py")["text"] == edited
    repo = builder.apps.repo("etienne", "tasks")
    assert repo.data(["items.csv"]) == {"items.csv": b"name,note\nMilk,one\n"}


def test_a_live_data_refusal_keeps_its_app_when_copied(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.publish_app("etienne", "tasks", "First version")

    with pytest.raises(LiveData) as error:
        builder.write_file("etienne", "tasks", "data/items.csv", "name,note\n")
    copied = copy.deepcopy(error.value)

    assert str(copied) == str(error.value) and copied.info == error.value.info
    assert copied.info.published_at is not None


def test_the_first_data_said_is_what_went_live(builder: Builder, monkeypatch) -> None:
    builder.create_app("etienne", "tasks")
    checks = builder.publishing._checks

    def checked(login: str, app: str):
        found = checks(login, app)
        # A file the person uploads once the checks passed goes live with the rest.
        builder.drafts.write(login, app, "data/items.csv", b"name,note\nMilk,one\n")
        return found

    monkeypatch.setattr(builder.publishing, "_checks", checked)
    result = builder.publish_app("etienne", "tasks", "First version")

    assert result["published"] is True
    assert result["data"] == "First data: items.csv."
    live = builder.apps.repo("etienne", "tasks").data(["items.csv"])["items.csv"]
    assert live == b"name,note\nMilk,one\n"


def test_first_data_must_pass_the_rules(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "data/items.csv", "name,note\nMilk,one\nMilk,two\n")

    result = builder.publish_app("etienne", "tasks", "First version")

    assert result["published"] is False
    assert "Item names are unique" in result["checks"][-1]["detail"]
    assert builder.read_app_overview("etienne", "tasks")["published"] is False


def test_failing_tests_stop_the_publish(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "tests/test_extra.py", FAILING_TEST)

    result = builder.publish_app("etienne", "tasks", "First version")

    assert result["published"] is False
    assert len(result["checks"]) == 2
    assert "test_wrong" in result["checks"][-1]["detail"]
    again = builder.publish_app("etienne", "tasks", "First version")
    assert again["published"] is False
    assert builder.read_app_overview("etienne", "tasks")["published"] is False


def test_an_app_that_doesn_t_build_stops_the_publish(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.edit_file(
        "etienne",
        "tasks",
        "app.py",
        '"""Tasks: a list of items, kept by an agent."""',
        "",
    )

    result = builder.publish_app("etienne", "tasks", "First version")

    assert result["published"] is False
    assert len(result["checks"]) == 1
    assert "docstring" in result["checks"][0]["detail"]
    assert builder.read_app_overview("etienne", "tasks")["published"] is False


def test_publish_checks_the_draft_as_it_is(builder: Builder) -> None:
    # There is no code to tie a publish to an earlier report: each publish checks again.
    builder.create_app("etienne", "tasks")
    assert builder.check_app("etienne", "tasks")["ok"] is True
    builder.write_file("etienne", "tasks", "tests/test_extra.py", FAILING_TEST)

    result = builder.publish_app("etienne", "tasks", "First version")

    assert result["published"] is False
    assert builder.read_app_overview("etienne", "tasks")["published"] is False


def test_publish_checks_the_live_data_as_it_is(shop, builder: Builder) -> None:
    builder.check_app("etienne", "shop")
    shop.runtime.call(STAFF, "record_sale", {"sku": "TEA-SEN", "quantity": 2, "receipt": "R-1000"})

    result = builder.publish_app("etienne", "shop", "Same version")

    assert result["published"] is True
    assert result["data"] == "The live data is kept as it is."
    # The app's page, then each role's, as links named by the app: the MCP addresses are code.
    page = "http://127.0.0.1:8800/apps/etienne/shop"
    assert (
        f"first: [Shop]({page}), [Shop · owner]({page}/owner), [Shop · staff]({page}/staff). "
        "Present web pages as Markdown links, and the MCP addresses, assistant_url and "
        "agent_url, as code to copy, never as links."
    ) in result["next"]
    live = builder.apps.live("etienne", "shop").runtime.query_json(STAFF, "stock", {})
    assert next(row["quantity"] for row in live if row["sku"] == "TEA-SEN") == 9


def test_a_new_version_says_what_changes(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.publish_app("etienne", "tasks", "First version")
    source = builder.read_file("etienne", "tasks", "app.py")["text"]
    source += '''

@tasks.query
def count(ctx: QueryContext) -> int:
    """How many items."""
    return len(_items())
'''
    builder.write_file("etienne", "tasks", "app.py", source)

    assert "New query count, for everyone." in builder.check_app("etienne", "tasks")["changes"]
    result = builder.publish_app("etienne", "tasks", "Add a count")
    assert result["published"] is True
    assert "New query count, for everyone." in result["changes"]
    live = builder.apps.live("etienne", "tasks")
    assert "count" in {query["name"] for query in live.runtime.manifest["queries"]}


def test_a_publish_ends_open_transactions(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.publish_app("etienne", "tasks", "First version")
    caller = Caller("etienne", None)
    old = builder.apps.live("etienne", "tasks").runtime
    t = old.begin(caller)
    assert isinstance(old.call(caller, "add_item", {"name": "Milk"}, transaction=t), Committed)
    source = builder.read_file("etienne", "tasks", "app.py")["text"]
    builder.write_file("etienne", "tasks", "app.py", source + "\n# another version\n")
    assert builder.publish_app("etienne", "tasks", "Second version")["published"] is True

    live = builder.apps.live("etienne", "tasks").runtime
    ended = f"Transaction {t} ended when a new version of the app was published."
    for use in (
        lambda: live.query_json(caller, "items", {}, transaction=t),
        lambda: live.call(caller, "add_item", {"name": "Eggs"}, transaction=t),
        lambda: live.commit_transaction(caller, t),
    ):
        with pytest.raises(InvalidArguments) as answer:
            use()
        assert str(answer.value) == ended
    assert live.query_json(caller, "items", {}) == []
    assert live.begin(caller) != t


def test_a_publish_that_keeps_app_py_ends_open_transactions(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.publish_app("etienne", "tasks", "First version")
    caller = Caller("etienne", None)
    old = builder.apps.live("etienne", "tasks").runtime
    t = old.begin(caller)
    old.call(caller, "add_item", {"name": "Milk"}, transaction=t)
    builder.write_file("etienne", "tasks", "instructions.md", "Keep the list short.\n")
    assert builder.publish_app("etienne", "tasks", "New instructions")["published"] is True

    live = builder.apps.live("etienne", "tasks").runtime
    with pytest.raises(InvalidArguments) as answer:
        live.commit_transaction(caller, t)
    assert (
        str(answer.value) == f"Transaction {t} ended when a new version of the app was published."
    )
    assert live.query_json(caller, "items", {}) == []
    # Publishing the same version again ends nothing.
    kept = live.begin(caller)
    live.call(caller, "add_item", {"name": "Eggs"}, transaction=kept)
    assert builder.publish_app("etienne", "tasks", "Same again")["published"] is True
    again = builder.apps.live("etienne", "tasks").runtime
    assert isinstance(again.commit_transaction(caller, kept), Committed)
    assert again.query_json(caller, "items", {}) != []


def test_a_remark_is_one_line(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")

    for remark in ("", "one\ntwo", "x" * 201):
        with pytest.raises(BuilderError, match="The remark is one line"):
            builder.publish_app("etienne", "tasks", remark)


def test_publish_over_mcp(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")

    async def main() -> None:
        async with BuilderClient(me_server(builder.apps)) as client:
            result = await client.call_tool(
                "publish_app", {"app": "tasks", "remark": "First version"}
            )
            assert result.data["published"] is True
            assert result.data["changes"]
            with pytest.raises(ToolError):
                await client.call_tool(
                    "publish_app", {"app": "tasks", "remark": "Again", "confirm": "00000000"}
                )

    anyio.run(main)


def test_a_live_version_that_can_t_load_is_not_the_draft_s_fault(
    shop, builder: Builder, monkeypatch
) -> None:
    def broken(login: str, name: str, *, again: bool = True):
        raise BuildProblems(["the live version doesn't build"])

    monkeypatch.setattr(builder.apps, "live", broken)

    checked = builder.check_app("etienne", "shop")

    assert checked["ok"] is True, checked["problems"]
    assert "definition" in checked
    assert checked["changes"] is None
    assert "live version of shop can't be loaded" in checked["changes_unknown"]


def test_a_delete_during_a_publish_waits_for_it(builder: Builder, monkeypatch) -> None:
    builder.create_app("etienne", "tasks")
    # Set once the delete is done, or waits for the draft's lock.
    moved = threading.Event()
    builder.drafts._locks[("etienne", "tasks")] = Watched(moved)  # ty: ignore[invalid-assignment]
    deleting = threading.Thread(target=builder.delete_app, args=("etienne", "tasks", "tasks"))
    real = builder.apps.publish

    def publish(*args, **kwargs):
        # The owner deletes the app while it goes live.
        deleting.start()
        assert moved.wait(timeout=30)
        return real(*args, **kwargs)

    monkeypatch.setattr(builder.apps, "publish", publish)
    # The delete runs as soon as the app is live, so the answer may find it gone.
    with contextlib.suppress(BuilderError):
        builder.publish_app("etienne", "tasks", "First version")
    deleting.join(timeout=60)

    assert not deleting.is_alive()
    # The publish ended before the delete, which left no draft behind.
    assert builder.apps.database.app("etienne", "tasks") is None
    assert not builder.drafts.exists("etienne", "tasks")


def test_a_live_version_that_can_t_load_is_fixed_by_publishing(
    builder: Builder, monkeypatch
) -> None:
    builder.create_app("etienne", "tasks")
    builder.publish_app("etienne", "tasks", "First version")
    builder.apps.forget("etienne", "tasks")
    client = builder.apps.executor.sandbox("etienne")
    real = type(client).put_version
    broken: set[str] = set()
    starts: list[str] = []

    def put_version(self, login: str, app: str, version: str, files: dict[str, bytes]):
        # The live version no longer loads, as after an SDK that refuses it.
        if threading.current_thread().name == "start etienne/tasks":
            starts.append(version)
            broken.add(starts[0])
            if version in broken:
                raise SandboxError("the live version doesn't load")
        return real(self, login, app, version, files)

    monkeypatch.setattr(type(client), "put_version", put_version)

    checked = builder.check_app("etienne", "tasks")
    # Its start began with the call, and isn't started again once it failed.
    assert len(starts) == 1
    assert "the live version of tasks can't be loaded" in checked["changes_unknown"]
    builder.write_file("etienne", "tasks", "instructions.md", "Fixed.\n")
    published = builder.publish_app("etienne", "tasks", "Fixed")

    assert published["published"] is True
    assert published["changes"] == [
        "What changes isn't known: the live version of tasks can't be loaded "
        "(the live version doesn't load)."
    ]
