import json
import re

import anyio
import httpx
import pytest
from fastmcp import Client as BuilderClient
from fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from cliquet.files import Kind
from cliquet_server.app import create_app
from cliquet_server.apps import Apps
from cliquet_server.builder import Builder, BuilderError
from cliquet_server.builder.core import EXAMPLES, TOPICS
from cliquet_server.db import Right
from cliquet_server.executor import LocalExecutor
from cliquet_server.me import (
    ACCEPT,
    ASK_FILE,
    DECLINE,
    DESCRIBE,
    LIST,
    READ,
    SHOW,
    UPLOAD_FILE,
    WRITE,
)
from cliquet_server.settings import Settings, SettingsError

from .server_helpers import (
    NOW,
    app_client,
    app_token,
    draft_example,
    draft_files,
    me_server,
    one_unexpected,
    running,
    signed_in,
    unexpected,
)

BASE = "http://127.0.0.1:8800"
ISSUER = "https://example.eu.auth0.com/"


def test_read_app_overview_of_a_published_app(shop, builder: Builder) -> None:
    info = builder.read_app_overview("etienne", "shop")

    assert info["app"] == "shop"
    assert info["published"] is True
    assert info["web_url"] == f"{BASE}/apps/etienne/shop"
    # The person's assistant opens it from their own endpoint; an agent on its own uses the
    # app's, with a token limited to it.
    assert info["assistant_url"] == f"{BASE}/me/mcp"
    assert info["agent_url"] == f"{BASE}/apps/etienne/shop/mcp"
    assert info["tokens_url"] == f"{BASE}/tokens?app=etienne/shop"
    # The builder is the owner's, who downloads the app's data and history from its page.
    assert info["download_url"] == f"{BASE}/manage/etienne/shop/download"
    assert info["web_urls"] == {
        "owner": f"{BASE}/apps/etienne/shop/owner",
        "staff": f"{BASE}/apps/etienne/shop/staff",
    }
    assert info["next"].startswith(
        f"The web pages: [Shop]({BASE}/apps/etienne/shop), "
        f"[Shop · owner]({BASE}/apps/etienne/shop/owner), "
    )
    assert "MCP addresses, assistant_url and agent_url, as code to copy" in info["next"]
    tools = {tool["name"]: tool for tool in info["tools"]}
    assert tools["record_sale"]["kind"] == "action"
    assert tools["record_sale"]["roles"] == ["owner", "staff"]
    assert tools["shelves"]["kind"] == "view"
    assert "Stock never goes below zero" in info["rules"]
    # By app_history's keys, the commit first, which read_change takes.
    (published,) = info["latest_changes"]
    assert list(published) == [
        "commit",
        "time",
        "caller",
        "role",
        "agent",
        "action",
        "remark",
        "files",
    ]
    assert published["action"] == "app_publish" and len(published["commit"]) == 40
    assert info["errors"]["count"] == 0 and info["errors"]["latest"] == []
    assert info["version"]


def test_read_app_overview_of_an_unknown_app_says_what_to_do(builder: Builder) -> None:
    with pytest.raises(BuilderError) as error:
        builder.read_app_overview("etienne", "nope")

    assert "list_apps" in str(error.value)
    assert "create_app" in str(error.value)


def test_read_app_overview_of_an_unpublished_app(apps: Apps, builder: Builder) -> None:
    apps.database.create_app("etienne", "draft", NOW)

    info = builder.read_app_overview("etienne", "draft")

    assert info["published"] is False
    assert "publish_app" in info["note"]


def test_the_tools_are_served_over_mcp(shop, apps: Apps, settings: Settings) -> None:
    async def main() -> None:
        async with BuilderClient(me_server(apps)) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert {"list_apps", "read_app_overview"} <= names
            assert 'read_guide("start")' in (client.instructions or "")
            result = await client.call_tool("read_app_overview", {"app": "etienne/shop"})
            assert result.structured_content["app"] == "shop"
            # An app is named alone or as the caller's own owner/name: never another's.
            with pytest.raises(ToolError, match="Only sam builds sam/shop"):
                await client.call_tool("read_app_overview", {"app": "sam/shop"})

    anyio.run(main)


def test_the_tools_run_in_the_builder_s_own_threads(
    shop, apps: Apps, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    borrowed = []
    described = Builder.read_app_overview

    def counted(self: Builder, login: str, app: str) -> dict[str, object]:
        # A builder's tool holds none of the default threads that pages and app calls need.
        default = anyio.to_thread.current_default_thread_limiter
        borrowed.append(anyio.from_thread.run_sync(lambda: default().borrowed_tokens))
        return described(self, login, app)

    monkeypatch.setattr(Builder, "read_app_overview", counted)

    async def main() -> None:
        async with BuilderClient(me_server(apps)) as client:
            answer = await client.call_tool("read_app_overview", {"app": "shop"})
            assert answer.structured_content["app"] == "shop"

    anyio.run(main)
    assert borrowed == [0]


TOOLS = {
    "read_app_overview",
    "read_change",
    "read_error",
    "create_app",
    "list_files",
    "read_file",
    "write_file",
    "ask_for_file",
    "edit_file",
    "delete_file",
    "discard_draft",
    "restore_version",
    "delete_app",
    "read_guide",
    "read_example",
    "find_classes",
    "find_icons",
    "check_app",
    "run_tests",
    "start_preview",
    "stop_preview",
    "try_call",
    "try_view",
    "show_preview",
    "take_screenshots",
    "publish_app",
    "preview_call",
    "upload_file",
    "share_app",
    "revoke_share",
    "create_token",
    "issue_token",
    "list_tokens",
    "revoke_token",
}


# Only the screens and the builder's panels call these: the model never sees them.
APP_ONLY = {"preview_call", "upload_file", "issue_token", UPLOAD_FILE}
# The person's endpoint's own tools, which a builder sees before the builder's.
ME_TOOLS = {LIST, DESCRIBE, READ, WRITE, SHOW, ACCEPT, DECLINE, ASK_FILE, UPLOAD_FILE}


def test_the_tools_have_the_new_names(apps: Apps, settings: Settings) -> None:
    async def main() -> None:
        async with BuilderClient(me_server(apps)) as client:
            tools = {
                tool.name: tool for tool in await client.list_tools() if tool.name not in ME_TOOLS
            }
        assert set(tools) == TOOLS
        for name, tool in tools.items():
            if name in APP_ONLY:
                assert tool.meta["ui"]["visibility"] == ["app"], name
            else:
                assert "visibility" not in (tool.meta or {}).get("ui", {}), name
        assert set(tools["create_app"].input_schema["properties"]) == {"app"}
        assert set(tools["find_classes"].input_schema["properties"]) == {"text"}
        assert set(tools["find_icons"].input_schema["properties"]) == {"text"}
        # In the order of the person's read_app_data: the app, the tool, the role, then its
        # arguments.
        assert list(tools["try_call"].input_schema["properties"]) == [
            "app",
            "tool",
            "role",
            "arguments",
        ]
        assert set(tools["publish_app"].input_schema["properties"]) == {"app", "remark"}
        assert set(tools["discard_draft"].input_schema["properties"]) == {"app"}
        assert set(tools["read_file"].input_schema["properties"]) == {
            "app",
            "path",
            "offset",
            "limit",
        }
        assert set(tools["ask_for_file"].input_schema["properties"]) == {"app", "path"}
        assert tools["discard_draft"].annotations.destructive_hint is True
        assert tools["delete_app"].annotations.destructive_hint is True
        assert tools["publish_app"].annotations.read_only_hint is False
        assert tools["publish_app"].annotations.open_world_hint is False
        assert "only changes the app on this Cliquet server" in (
            tools["publish_app"].description or ""
        )
        # A model picks a guide or an example from the schema, not from a guess.
        topic = tools["read_guide"].input_schema["properties"]["topic"]
        assert topic["enum"] == list(TOPICS)
        assert tools["read_example"].input_schema["properties"]["example"]["enum"] == list(EXAMPLES)
        width = tools["try_view"].input_schema["properties"]["width"]
        assert (width["minimum"], width["maximum"]) == (320, 1280)
        remark = tools["publish_app"].input_schema["properties"]["remark"]
        assert (remark["minLength"], remark["maxLength"]) == (1, 200)
        steps = tools["try_view"].input_schema["properties"]["steps"]["description"]
        assert all(form in steps for form in ('"click"', '"fill"', '"choose"', '"wait"'))
        # Each description is one paragraph, whatever the docstring's line breaks.
        assert all("\n" not in (tool.description or "") for tool in tools.values())

    anyio.run(main)


# What a model reads of each tool before its first call, in bytes: its name, title,
# description and input schema, never the host's own `_meta`. Every conversation carries them
# all, so details go in the guides and in the answers.
# The person's tools are everyone's, a user's who never builds too: about 5.5 KB, of which
# ask_for_app_file is 0.65 KB.
MAX_ME_TOOLS_SIZE = 5_700
# The builder's tools that a model calls, besides the person's: about 16.4 KB.
MAX_BUILDER_TOOLS_SIZE = 16_500


def test_the_tool_definitions_stay_short(apps: Apps, settings: Settings) -> None:
    async def main() -> None:
        async with BuilderClient(me_server(apps)) as client:
            listed = await client.session.list_tools()
        seen = {"name", "title", "description", "input_schema"}
        sizes = {
            tool.name: len(json.dumps(tool.model_dump(mode="json", by_alias=True, include=seen)))
            for tool in listed.tools
            # The model never sees the tools that only a screen or the upload panel calls.
            if tool.name not in APP_ONLY
        }
        assert set(sizes) == (ME_TOOLS | TOOLS) - APP_ONLY
        mine = {name: size for name, size in sizes.items() if name in ME_TOOLS}
        built = {name: size for name, size in sizes.items() if name not in ME_TOOLS}
        assert sum(mine.values()) <= MAX_ME_TOOLS_SIZE, sorted(mine.items(), key=lambda s: -s[1])
        assert sum(built.values()) <= MAX_BUILDER_TOOLS_SIZE, sorted(
            built.items(), key=lambda s: -s[1]
        )

    anyio.run(main)


# What each builder tool does, as its annotations tell the host: "reads", "writes", or
# "destroys" what the person keeps. A new tool must say which.
WHAT_TOOLS_DO = {
    "read_app_overview": "reads",
    "read_change": "reads",
    "read_error": "reads",
    "list_files": "reads",
    "read_file": "reads",
    "ask_for_file": "reads",
    "read_guide": "reads",
    "read_example": "reads",
    "find_classes": "reads",
    "find_icons": "reads",
    "list_tokens": "reads",
    "create_app": "writes",
    # These build the draft, which writes its app.py.lock.
    "check_app": "writes",
    "run_tests": "writes",
    "take_screenshots": "writes",
    "show_preview": "writes",
    # These change only the preview, a throwaway copy.
    "start_preview": "writes",
    "stop_preview": "writes",
    "try_view": "writes",
    "try_call": "writes",
    "preview_call": "writes",
    # This puts the file the person chose in the panel into the draft, as the upload page does.
    "upload_file": "writes",
    # This replaces a draft that has no change of its own, so it loses nothing.
    "restore_version": "writes",
    # These hand out access, which revoke_share and revoke_token take back.
    "share_app": "writes",
    "create_token": "writes",
    "issue_token": "writes",
    # These may remove a draft's file, its changes, the live version and its data, or the app,
    # for good: a publish replaces the live version, and its migration may rewrite the data.
    "publish_app": "destroys",
    "write_file": "destroys",
    "edit_file": "destroys",
    "delete_file": "destroys",
    "discard_draft": "destroys",
    "delete_app": "destroys",
    # These take back access the person gave.
    "revoke_share": "destroys",
    "revoke_token": "destroys",
}


# The tools that change something whose call, made again with the same arguments, changes
# nothing more: it does the same again, or is refused, as a second delete is. A host may make
# such a call again, such as after a timeout.
IDEMPOTENT = {
    "create_app",
    "write_file",
    "delete_file",
    "discard_draft",
    "restore_version",
    "delete_app",
    "check_app",
    "run_tests",
    "take_screenshots",
    "show_preview",
    "start_preview",
    "stop_preview",
    "share_app",
    "revoke_share",
    "revoke_token",
}

# What a host shows the person for each tool, such as in the prompt that asks them to allow it.
TITLES = {
    "read_app_overview": "Read an app's overview",
    "read_change": "Read a change",
    "read_error": "Read an app's error",
    "create_app": "Create an app",
    "list_files": "List an app's files",
    "read_file": "Read an app's file",
    "write_file": "Write a draft's file",
    "ask_for_file": "Ask for a data file",
    "upload_file": "Upload a data file",
    "edit_file": "Edit a draft's file",
    "delete_file": "Delete a draft's file",
    "discard_draft": "Discard a draft",
    "restore_version": "Restore an earlier version",
    "delete_app": "Delete an app",
    "read_guide": "Read a guide to building",
    "read_example": "Read an example app",
    "find_classes": "Find classes for a view",
    "find_icons": "Find icons",
    "publish_app": "Publish an app",
    "check_app": "Check a draft",
    "start_preview": "Start a preview",
    "stop_preview": "Stop a preview",
    "take_screenshots": "Take screenshots of views",
    "try_view": "Try a view on the preview",
    "try_call": "Try a call on the preview",
    "show_preview": "Show a view of the preview",
    "preview_call": "Call the preview from a view",
    "run_tests": "Run a draft's tests",
    "share_app": "Share an app",
    "revoke_share": "Revoke a share",
    "create_token": "Create a token",
    "issue_token": "Create a token from the panel",
    "list_tokens": "List your tokens",
    "revoke_token": "Revoke a token",
}


def test_each_tool_s_annotations_say_what_it_does(apps: Apps, settings: Settings) -> None:
    async def main() -> None:
        async with BuilderClient(me_server(apps)) as client:
            tools = await client.list_tools()
        found, repeats, titles = {}, set(), {}
        for tool in (tool for tool in tools if tool.name not in ME_TOOLS):
            titles[tool.name] = tool.title
            hints = tool.annotations
            assert hints is not None, tool.name
            if hints.read_only_hint:
                # MCP reads the idempotent hint only for a tool that writes.
                assert hints.idempotent_hint is None, tool.name
                found[tool.name] = "reads"
            else:
                assert hints.read_only_hint is False, tool.name
                assert hints.destructive_hint is not None, tool.name
                assert hints.idempotent_hint is not None, tool.name
                found[tool.name] = "destroys" if hints.destructive_hint else "writes"
                if hints.idempotent_hint:
                    repeats.add(tool.name)
        assert found == WHAT_TOOLS_DO
        assert repeats == IDEMPOTENT
        assert titles == TITLES

    anyio.run(main)


def test_create_app_takes_no_example(apps: Apps, settings: Settings, builder: Builder) -> None:
    made = builder.create_app("etienne", "tasks")

    assert {"app.py", "instructions.md", "tests/test_app.py"} <= set(made["files"])
    assert not any(
        path.startswith("data/") for path in draft_files(builder.drafts, "etienne", "tasks")
    )

    async def main() -> None:
        async with BuilderClient(me_server(apps)) as client:
            with pytest.raises(ToolError):
                await client.call_tool("create_app", {"app": "shop", "example": "stock"})
            answer = await client.call_tool("create_app", {"app": "notes"})
            assert answer.data["app"] == "notes"

    anyio.run(main)
    assert apps.database.app("etienne", "shop") is None


def test_an_error_reaches_the_assistant_as_text(shop, apps: Apps, settings: Settings) -> None:
    async def main() -> None:
        async with BuilderClient(me_server(apps)) as client:
            with pytest.raises(ToolError, match="create_app"):
                await client.call_tool("read_app_overview", {"app": "nope"})

    anyio.run(main)


def test_a_tool_s_refusal_is_not_unexpected(
    shop, apps: Apps, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    async def main() -> None:
        async with BuilderClient(me_server(apps)) as client:
            with pytest.raises(ToolError, match="create_app"):
                await client.call_tool("read_app_overview", {"app": "nope"})

    anyio.run(main)
    assert not unexpected(caplog)


def test_a_tool_that_crashes_is_logged_once_as_unexpected(
    shop,
    apps: Apps,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(*args: object, **options: object) -> None:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(apps.database, "app", broken)

    async def main() -> str:
        async with BuilderClient(me_server(apps)) as client:
            with pytest.raises(ToolError) as error:
                await client.call_tool("read_app_overview", {"app": "shop"})
            return str(error.value)

    said = anyio.run(main)
    # The assistant reads Cliquet's fixed words and the id, never the crash's text. The id is
    # the server's operator's, not one read_error reads, and the words say so.
    told = re.fullmatch(
        r"Cliquet failed on this call, not the app\. Try again in a moment\. If it fails "
        r"again, give the person error (e-[0-9a-f]{12}) for the server's operator: read_error "
        r"can't read it\.",
        said,
    )
    assert told is not None, said
    record = one_unexpected(caplog, RuntimeError)
    assert record.getMessage() == (
        f"unexpected: error {told.group(1)}: the builder's tool read_app_overview failed: "
        "RuntimeError: the database went away"
    )


def test_without_a_login_the_builder_refuses(apps: Apps, settings: Settings) -> None:
    no_login = Settings.from_env({"CLIQUET_DATA_DIR": str(settings.data_dir)})
    unsigned = Apps(no_login, apps.database, apps.executor, clock=lambda: NOW)

    async def main() -> None:
        async with BuilderClient(me_server(unsigned)) as client:
            assert "read_app_overview" not in {tool.name for tool in await client.list_tools()}
            with pytest.raises(ToolError, match="Sign in"):
                await client.call_tool("read_app_overview", {"app": "shop"})

    anyio.run(main)
    unsigned.close()


def test_me_serves_the_builder_next_to_the_apps(server, address: str) -> None:
    # Its old address is gone, whatever the sign-in.
    assert httpx.post(f"{address}/builder/mcp", json={}).status_code == 410

    async def main() -> None:
        # A token, as on an app's endpoint: under the development login, any value.
        async with app_client(address, "/me/mcp", "dev") as me:
            names = {tool.name for tool in (await me.list_tools()).tools}
            assert {"create_app", "read_app_overview", LIST} <= names
        token = app_token(server, "staff")
        async with app_client(address, "/apps/etienne/shop/mcp", token) as app:
            names = {tool.name for tool in (await app.list_tools()).tools}
            assert "record_sale" in names

    anyio.run(main)


def test_the_builder_can_be_off(tmp_path, executor: LocalExecutor) -> None:
    settings = Settings.from_env({"CLIQUET_DATA_DIR": str(tmp_path / "off")})
    app = create_app(settings, executor=executor, clock=lambda: NOW)
    database = app.state.apps.database
    ann = database.person_for(ISSUER, "auth0|ann", "ann@example.com", "ann", NOW)
    database.grant("ann", Right.BUILD)
    _, token = database.create_token(ann.id, "laptop", NOW)

    async def names(address: str) -> set[str]:
        async with app_client(address, "/me/mcp", token) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    with running(app) as address:
        listed = anyio.run(names, address)
    with TestClient(app) as client:
        home = client.get("/", follow_redirects=False)

    assert home.headers["location"] == "/login?next=%2F"
    assert app.state.builder is None
    # Even a person who may build gets the person's tools alone.
    assert listed == ME_TOOLS


def test_the_home_page_shows_how_to_build_an_app(server) -> None:
    with signed_in(server) as client:
        home = client.get("/").text
        asked = client.get("/?new").text
        build = client.get("/build", follow_redirects=False)

    # With an app of their own, the guide waits for the New app button.
    assert "Build a new app" not in home and 'href="/?new#new-app"' in home
    assert "Build a new app" in asked
    assert f"{BASE}/me/mcp" in asked
    assert "builder/mcp" not in asked
    assert "Connect your AI assistant" in asked
    # Each idea starts near one of the examples.
    assert "meeting rooms" in asked and "deployed environments" in asked
    assert "household accounts" in asked
    # The build page's old address leads to it.
    assert build.status_code == 303 and build.headers["location"] == "/?new#new-app"


def test_the_build_page_warns_about_a_remote_development_login(
    tmp_path, executor: LocalExecutor
) -> None:
    settings = Settings.from_env(
        {
            "CLIQUET_DATA_DIR": str(tmp_path / "demo"),
            "CLIQUET_BASE_URL": "https://cliquet.example",
            "CLIQUET_ALLOW_REMOTE_LOCAL_EXECUTOR": "1",
            "CLIQUET_DEV_LOGIN": "etienne",
            "CLIQUET_ALLOW_REMOTE_DEV_LOGIN": "1",
        }
    )
    app = create_app(settings, executor=executor, clock=lambda: NOW)

    with signed_in(app, base_url="https://testserver") as client:
        pages = [client.get("/").text, client.get("/assistants").text]

    for text in pages:
        assert "can build as etienne" in text
        assert "https://cliquet.example/me/mcp" in text


def test_a_dev_login_needs_a_local_address(tmp_path) -> None:
    with pytest.raises(SettingsError, match="CLIQUET_ALLOW_REMOTE_DEV_LOGIN"):
        Settings.from_env(
            {
                "CLIQUET_DATA_DIR": str(tmp_path),
                "CLIQUET_BASE_URL": "https://cliquet.example",
                "CLIQUET_ALLOW_REMOTE_LOCAL_EXECUTOR": "1",
                "CLIQUET_DEV_LOGIN": "etienne",
            }
        )


def test_the_remote_flag_allows_a_development_login(tmp_path) -> None:
    settings = Settings.from_env(
        {
            "CLIQUET_DATA_DIR": str(tmp_path),
            "CLIQUET_BASE_URL": "https://cliquet.example",
            "CLIQUET_ALLOW_REMOTE_LOCAL_EXECUTOR": "1",
            "CLIQUET_DEV_LOGIN": "Etienne",
            "CLIQUET_ALLOW_REMOTE_DEV_LOGIN": "1",
        }
    )

    assert settings.dev_login == "etienne"
    assert settings.allow_remote_dev_login is True


def test_the_remote_flag_takes_a_boolean(tmp_path) -> None:
    with pytest.raises(SettingsError, match="CLIQUET_ALLOW_REMOTE_DEV_LOGIN"):
        Settings.from_env(
            {
                "CLIQUET_DATA_DIR": str(tmp_path),
                "CLIQUET_BASE_URL": "https://cliquet.example",
                "CLIQUET_ALLOW_REMOTE_LOCAL_EXECUTOR": "1",
                "CLIQUET_DEV_LOGIN": "etienne",
                "CLIQUET_ALLOW_REMOTE_DEV_LOGIN": "perhaps",
            }
        )


def test_ask_for_file_gives_the_link(builder: Builder) -> None:
    builder.create_app("etienne", "books")

    asked = builder.ask_for_file("etienne", "books", "data/2026/ledger.beancount")

    # A data name holds only letters, digits, ., _, - and /, which a query keeps as they are.
    assert asked["link"] == f"{BASE}/build/books/upload?path=data/2026/ledger.beancount"
    assert "list_files" in asked["next"]
    assert "check_app" in asked["next"]
    assert "try_call" in asked["next"]
    for path in ("app.py", "data/.x", "../x", "migrate.py", "data/my ledger.beancount"):
        with pytest.raises(BuilderError):
            builder.ask_for_file("etienne", "books", path)
    with pytest.raises(BuilderError, match="no app named nope"):
        builder.ask_for_file("etienne", "nope", "data/a.csv")


def test_ask_for_file_refuses_a_published_app(shop, builder: Builder) -> None:
    with pytest.raises(BuilderError) as refused:
        builder.ask_for_file("etienne", "shop", "data/products.csv")

    assert "actions" in str(refused.value)
    assert "migration" in str(refused.value)
    # Asking never makes a draft of a published app.
    assert not builder.drafts.exists("etienne", "shop")


def numbered(count: int, width: int = 0) -> str:
    return "".join(f"line {number}".ljust(width) + "\n" for number in range(1, count + 1))


def test_read_file_in_parts(builder: Builder) -> None:
    builder.create_app("etienne", "books")
    text = numbered(2_500)
    builder.drafts.write("etienne", "books", "data/notes.txt", text.encode())

    first = builder.read_file("etienne", "books", "data/notes.txt")
    middle = builder.read_file("etienne", "books", "data/notes.txt", offset=1_001, limit=10)
    last = builder.read_file("etienne", "books", "data/notes.txt", offset=2_401, limit=2_000)

    assert first["path"] == "data/notes.txt"
    assert (first["from"], first["to"], first["lines"]) == (1, 1_000, 2_500)
    assert first["size"] == len(text)
    assert first["text"] == numbered(1_000)
    assert first["next"] == (
        "Lines 1,001 to 2,500 weren't read. Read more with offset=1001, "
        "or ask the app's queries with try_call."
    )
    assert (middle["from"], middle["to"]) == (1_001, 1_010)
    assert middle["text"] == "".join(f"line {n}\n" for n in range(1_001, 1_011))
    assert "offset=1011" in middle["next"]
    assert (last["from"], last["to"], last["lines"]) == (2_401, 2_500, 2_500)
    assert last["text"].startswith("line 2401\n")
    assert "next" not in last
    assert builder.read_file("etienne", "books", "data/notes.txt", offset=2_500)["text"] == (
        "line 2500\n"
    )
    builder.drafts.write("etienne", "books", "data/empty.txt", b"")
    builder.drafts.write("etienne", "books", "data/open.txt", b"a\nb")
    empty = builder.read_file("etienne", "books", "data/empty.txt")
    assert (empty["from"], empty["to"], empty["lines"], empty["text"]) == (1, 0, 0, "")
    unended = builder.read_file("etienne", "books", "data/open.txt", offset=2)
    assert (unended["to"], unended["lines"], unended["text"]) == (2, 2, "b")
    for offset, limit in ((0, 10), (2_501, 10), (1, 0), (1, 2_001)):
        with pytest.raises(BuilderError):
            builder.read_file("etienne", "books", "data/notes.txt", offset=offset, limit=limit)


def test_read_file_reads_a_code_file_whole(builder: Builder) -> None:
    draft_example(builder, "shop")
    source = draft_files(builder.drafts, "etienne", "shop")["app.py"].decode()
    assert len(source) > 20_000

    read = builder.read_file("etienne", "shop", "app.py")

    assert read["text"] == source
    assert read["from"] == 1
    assert read["to"] == read["lines"] == source.count("\n")
    assert "next" not in read


def test_read_file_stops_at_40000_characters(builder: Builder) -> None:
    builder.create_app("etienne", "books")
    wide = numbered(10_000, width=59)
    assert len(wide) == 600_000
    builder.drafts.write("etienne", "books", "data/wide.txt", wide.encode())
    builder.drafts.write("etienne", "books", "data/one.txt", b"x" * 600_000)

    part = builder.read_file("etienne", "books", "data/wide.txt")
    one = builder.read_file("etienne", "books", "data/one.txt")

    assert len(part["text"]) <= 40_000
    assert part["text"].endswith("\n")
    assert part["to"] == 40_000 // 60
    assert part["text"] == wide[: part["to"] * 60]
    assert f"offset={part['to'] + 1}" in part["next"]
    assert one["text"] == "x" * 40_000
    assert (one["from"], one["to"], one["lines"]) == (1, 1, 1)
    assert "can't read the rest of that line" in one["next"]
    assert "try_call" in one["next"]


def test_read_file_in_parts_on_live_data(shop, builder: Builder) -> None:
    live = builder.apps.repo("etienne", "shop").data(["movements.csv"])["movements.csv"].decode()
    lines = live.splitlines(keepends=True)

    part = builder.read_file("etienne", "shop", "data/movements.csv", offset=5, limit=3)

    assert (part["from"], part["to"], part["lines"]) == (5, 7, len(lines))
    assert part["size"] == len(live.encode())
    assert part["text"] == "".join(lines[4:7])
    assert "offset=8" in part["next"]
    assert not builder.drafts.sizes("etienne", "shop", kind=Kind.DATA)


def test_read_file_counts_characters_not_bytes(builder: Builder) -> None:
    builder.create_app("etienne", "books")
    accented = "".join(f"café {number} é\n" for number in range(1, 101))
    builder.drafts.write("etienne", "books", "data/cafe.txt", accented.encode())
    builder.drafts.write("etienne", "books", "data/long.txt", ("é" * 50_000).encode())

    part = builder.read_file("etienne", "books", "data/cafe.txt", offset=10, limit=2)
    long = builder.read_file("etienne", "books", "data/long.txt")

    assert part["text"] == "café 10 é\ncafé 11 é\n"
    assert part["size"] == len(accented.encode())
    assert long["text"] == "é" * 40_000
    assert (long["to"], long["lines"]) == (1, 1)
    assert "can't read the rest of that line" in long["next"]
