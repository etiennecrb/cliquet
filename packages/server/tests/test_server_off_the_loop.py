"""The server's async handlers never call the database on the event loop: a slow disk or a
write's lock would stall every request the process serves."""

import asyncio
import functools
import inspect
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from cliquet_server.db import Database
from cliquet_server.executor import LocalExecutor
from cliquet_server.me import DESCRIBE, LIST

from .server_helpers import (
    APPS,
    NOW,
    a_shop_member,
    app_client,
    app_token,
    oidc_callback,
    oidc_server,
    text,
)

# Called only as the server stops, once nothing is served.
_AT_SHUTDOWN = {"close"}


@pytest.fixture
def on_the_loop(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Make every `Database` method raise when called on an event loop's thread, and give
    the names of those that were: the test checks none was, whatever the handler did with
    the error."""
    called: list[str] = []

    def guarded(name: str, method: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(method)
        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return method(*args, **kwargs)
            # Where the server called it from, for the test's message.
            frames = traceback.extract_stack()[:-1]
            where = next(
                (
                    f"{f.filename.split('/src/')[-1]}:{f.lineno} {f.name}"
                    for f in reversed(frames)
                    if "cliquet_server" in f.filename and not f.filename.endswith("db.py")
                ),
                "?",
            )
            called.append(f"{name} from {where}")
            raise AssertionError(f"Database.{name} was called on the event loop, from {where}")

        return call

    for name, method in inspect.getmembers(Database, inspect.isfunction):
        if not name.startswith("_") and name not in _AT_SHUTDOWN:
            monkeypatch.setattr(Database, name, guarded(name, method))
    yield called
    assert called == []


def test_the_app_s_pages_and_endpoints_call_the_database_off_the_loop(
    server: Starlette, address: str, on_the_loop: list[str]
) -> None:
    sam = a_shop_member(server, None)
    owner_token = app_token(server, "owner")
    # Through the server that serves the MCP endpoints, in its own loop, as in production.
    with httpx.Client(base_url=address) as client, httpx.Client(base_url=address) as other:
        assert client.get("/healthz").status_code == 200
        assert client.get("/login", follow_redirects=False).status_code == 303
        # The console's pages, each with the sidebar's apps.
        manage = "/manage/etienne/shop"
        tabs = ("views", "sharing", "tools", "history", "errors")
        pages = ("/", "/assistants", "/tokens", "/account", manage)
        for page in (*pages, *(f"{manage}/{tab}" for tab in tabs)):
            assert client.get(page).status_code == 200, page
        opened = client.get("/apps/etienne/shop", follow_redirects=False)
        assert opened.headers["location"] == "/apps/etienne/shop/owner"
        # The app's own icon: public, and read from its record, never started.
        assert client.get("/apps/etienne/shop/icon.svg").status_code == 404
        assert client.get("/apps/etienne/shop/icon-192.png").status_code == 200
        assert client.get("/apps/etienne/shop/owner").status_code == 200
        assert client.get("/apps/etienne/shop/owner/manifest.webmanifest").status_code == 200
        assert client.get("/apps/etienne/shop/owner/screen").status_code == 200
        called = client.post(
            "/apps/etienne/shop/owner/rpc",
            json={"method": "tools/call", "params": {"name": "stock", "arguments": {}}},
            headers={"Origin": address},
        )
        assert called.status_code == 200, called.text
        # Each refusal too: an app that isn't yours, a share that waits, and a role not held.
        other.cookies.set("session", server.state.web_auth.session_cookie(sam))
        assert other.get("/apps/etienne/shop/staff").status_code == 404
        assert other.get("/apps/etienne/shop/staff/manifest.webmanifest").status_code == 404
        database = server.state.apps.database
        database.share("etienne", "shop", "sam@example.com", "staff", NOW)
        assert "/shares/etienne/shop/accept" in other.get("/apps/etienne/shop/staff").text
        database.accept_share(sam.id, "etienne", "shop", NOW)
        assert other.get("/apps/etienne/shop/owner").status_code == 403
        # sam deletes their account, and is signed out.
        csrf = server.state.web_auth.csrf_for_session(other.cookies.get("session"))
        refused = other.post("/account/delete", data={"csrf": csrf, "login": "etienne"})
        assert refused.status_code == 400
        deleted = other.post("/account/delete", data={"csrf": csrf, "login": "sam"})
        assert deleted.status_code == 200, deleted.text

    async def endpoints() -> None:
        async with app_client(address, "/apps/etienne/shop/mcp", owner_token) as shop:
            assert (await shop.list_tools()).tools
        async with app_client(address, "/me/mcp") as me:
            assert "etienne/shop" in text(await me.call_tool(LIST, {}))
            assert '<app_instructions app="etienne/shop">' in text(
                await me.call_tool(DESCRIBE, {"app": "shop", "role": "owner"})
            )

    anyio.run(endpoints)


@pytest.fixture
def oidc_running(tmp_path: Path, executor: LocalExecutor) -> Iterator[Starlette]:
    """The server signing in through a faked provider."""
    app = oidc_server(tmp_path, executor)
    app.state.apps.import_folder(APPS / "stock", "etienne", "shop", email="etienne@example.com")
    yield app


def test_signing_in_calls_the_database_off_the_loop(
    oidc_running: Starlette, on_the_loop: list[str]
) -> None:
    with TestClient(oidc_running) as client:

        def back_from_the_provider() -> str:
            return oidc_callback(oidc_running, client, "kim")

        # A new person chooses a login, then comes back as a known one.
        chosen = client.get(back_from_the_provider())
        assert chosen.status_code == 200
        csrf = oidc_running.state.web_auth.csrf_for_session(client.cookies.get("new_person"))
        taken = client.post(
            "/auth/choose-login",
            data={"login": "kim", "csrf": csrf or ""},
            follow_redirects=False,
        )
        assert taken.status_code == 303, taken.text
        assert back_from_the_provider() == "/"
