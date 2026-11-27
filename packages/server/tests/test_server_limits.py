import logging
from datetime import timedelta

import anyio
import mcp_types as types
import pytest
from fastmcp import Client as BuilderClient
from fastmcp.exceptions import ToolError
from starlette.applications import Starlette

from cliquet import views
from cliquet_server.app import create_app
from cliquet_server.apps import Apps
from cliquet_server.builder import Builder
from cliquet_server.builder.errors import BuilderError
from cliquet_server.db import TokenLimit
from cliquet_server.executor import LocalExecutor
from cliquet_server.limiter import Limit, Limited, Limiter, Limits, Once, why, why_runs
from cliquet_server.me import READ
from cliquet_server.settings import Settings, SettingsError

from .server_helpers import (
    APPS,
    NOW,
    a_shop_member,
    app_client,
    app_status,
    app_token,
    call,
    me_server,
    running,
    signed_in,
)


class Clock:
    """A clock a test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def limits(clock: Clock, *, at_once: int = 8, per_minute: int = 3, runs: int = 300) -> Limits:
    return Limits(
        calls_at_once=at_once,
        calls_per_minute=per_minute,
        sandbox_runs_per_hour=runs,
        clock=clock,
    )


def text(answer: types.CallToolResult) -> str:
    (content,) = answer.content
    assert isinstance(content, types.TextContent)
    return content.text


def test_a_window_refuses_past_its_limit_until_it_ends() -> None:
    clock = Clock()
    limiter = Limiter(Limit(per_window=2, window=60), clock=clock)
    limiter.take("a")
    clock.now += 10
    limiter.take("a")
    with pytest.raises(Limited) as refused:
        limiter.take("a")
    assert (refused.value.at_once, refused.value.retry_after) == (False, 50)
    # Another key has its own window.
    limiter.take("b")
    clock.now += 50
    limiter.take("a")


def test_calls_at_once_are_refused_while_others_run() -> None:
    limiter = Limiter(Limit(per_window=100, window=60, at_once=2), clock=Clock())
    with limiter.call("a"), limiter.call("a"):
        with pytest.raises(Limited) as refused, limiter.call("a"):
            pass
        assert refused.value.at_once
        with limiter.call("b"):
            pass
    with limiter.call("a"):
        pass


def test_a_call_that_fails_still_ends() -> None:
    limiter = Limiter(Limit(per_window=100, window=60, at_once=1), clock=Clock())
    with pytest.raises(RuntimeError), limiter.call("a"):
        raise RuntimeError("the call failed")
    with limiter.call("a"):
        pass


def test_a_refused_call_counts_nothing() -> None:
    clock = Clock()
    limiter = Limiter(Limit(per_window=2, window=60, at_once=1), clock=clock)
    with limiter.call("a"), pytest.raises(Limited):
        limiter.take("a", run=True)
    limiter.take("a")


def test_a_limiter_forgets_ended_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    from cliquet_server import limiter as module

    monkeypatch.setattr(module, "_KEYS_KEPT", 3)
    clock = Clock()
    limiter = Limiter(Limit(per_window=1, window=60), clock=clock)
    for key in "abc":
        limiter.take(key)
    clock.now += 60
    limiter.take("d")
    assert list(limiter._windows) == ["d"]
    for key in "efg":
        limiter.take(key)
    # None has ended: the oldest is forgotten, and starts a new window.
    assert list(limiter._windows) == ["e", "f", "g"]


def test_once_says_yes_once_per_period() -> None:
    clock = Clock()
    once = Once(60, clock=clock)
    assert once.first("a")
    assert not once.first("a")
    assert once.first("b")
    clock.now += 60
    assert once.first("a")


def test_a_refusal_says_what_and_when() -> None:
    window = Limited("k", Limit(per_window=300, window=60), False, 42)
    assert why(window, "this app") == (
        "Too many calls to this app: at most 300 a minute. Try again in 42 seconds."
    )
    together = Limited("k", Limit(per_window=300, window=60, at_once=8), True, 1)
    assert why(together, "the builder") == (
        "Too many calls to the builder at once: at most 8 run together. "
        "Wait for one to finish, then try again."
    )
    runs = Limited("k", Limit(per_window=300, window=3600), False, 1800)
    assert "300 runs in your sandbox" in why_runs(runs)
    assert why_runs(runs).endswith("Try again in 30 minutes.")


def test_the_limits_are_settings(tmp_path) -> None:
    base = {"CLIQUET_DATA_DIR": str(tmp_path)}
    defaults = Settings.from_env(base)
    assert (defaults.calls_at_once, defaults.calls_per_minute) == (8, 300)
    assert (defaults.sandbox_runs_per_hour, defaults.apps_per_owner) == (300, 50)
    given = Settings.from_env({**base, "CLIQUET_CALLS_PER_MINUTE": "20"})
    assert given.calls_per_minute == 20
    with pytest.raises(SettingsError, match="CLIQUET_CALLS_AT_ONCE"):
        Settings.from_env({**base, "CLIQUET_CALLS_AT_ONCE": "0"})


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def limited(settings: Settings, executor: LocalExecutor, clock: Clock) -> Starlette:
    """The server, with etienne/shop and etienne/cafe, at three calls a minute per key."""
    app = create_app(settings, executor=executor, clock=lambda: NOW, limits=limits(clock))
    apps = app.state.apps
    apps.import_folder(APPS / "stock", "etienne", "shop", email="etienne@example.com")
    apps.import_folder(APPS / "stock", "etienne", "cafe")
    return app


def test_an_app_s_address_refuses_calls_over_its_limit(
    limited: Starlette, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    # Two tokens for the same app share its count: more tokens don't raise the limit.
    first, second = app_token(limited, "staff"), app_token(limited, "owner")
    cafe = app_token(limited, "staff", "cafe")

    async def calls(path: str, token: str, count: int) -> list[types.CallToolResult]:
        async with app_client(address, path, token) as client:
            return [await client.call_tool("stock", {}) for _ in range(count)]

    with running(limited) as address, caplog.at_level(logging.WARNING):
        assert not any(a.is_error for a in anyio.run(calls, "/apps/etienne/shop/mcp", first, 2))
        (refused, again) = anyio.run(calls, "/apps/etienne/shop/mcp", second, 2)
        # Another app, of the same owner, isn't slowed.
        assert not any(a.is_error for a in anyio.run(calls, "/apps/etienne/cafe/mcp", cafe, 3))
        clock.now += 60
        (later,) = anyio.run(calls, "/apps/etienne/shop/mcp", first, 1)

    assert not refused.is_error
    assert again.is_error
    assert text(again) == (
        "Too many calls to etienne/shop: at most 3 a minute. Try again in 60 seconds."
    )
    assert not later.is_error
    # Logged once for the minute, naming the token, never its value.
    lines = [r.getMessage() for r in caplog.records if "over its limit" in r.getMessage()]
    assert len(lines) == 1
    assert "app etienne/shop" in lines[0] and "shop as owner" in lines[0]
    assert second not in lines[0]
    # Its owner sees it among the app's errors, once an hour.
    errors = limited.state.apps.database.errors("etienne", "shop", NOW)
    assert len(errors) == 1
    assert "hit a limit at the app's address" in errors[0].text
    assert errors[0].tool == "stock"


def test_an_expired_token_at_an_app_s_address_is_an_error_of_the_app_once_an_hour(
    limited: Starlette, clock: Clock
) -> None:
    database = limited.state.apps.database
    etienne = database.person_by_login("etienne")
    # Made 8 days before the server's clock, for 7 days.
    made = NOW - timedelta(days=8)
    shop, cafe = TokenLimit("etienne", "shop", "staff"), TokenLimit("etienne", "cafe", "staff")
    _, till = database.create_token(etienne.id, "Till", made, [shop, cafe], days=7)
    _, other = database.create_token(etienne.id, "Cafe", made, [cafe], days=7)
    _, laptop = database.create_token(etienne.id, "Laptop", made, days=7)

    with running(limited) as address:
        assert [app_status(address, till) for _ in range(2)] == [401, 401]
        # Only a token limited to the app is the app's error: not one for other apps, nor one
        # for everywhere.
        assert app_status(address, other) == 401 and app_status(address, laptop) == 401
        kept = database.errors("etienne", "shop", NOW)
        clock.now += 3600
        assert app_status(address, till) == 401

    (error,) = kept
    assert (error.caller, error.role, error.tool, error.start) == (
        "etienne",
        "staff",
        "(connect)",
        False,
    )
    assert error.text == (
        "etienne with the token 'Till' was refused at the app's address: the token expired on "
        "10 Nov 2026, 12:00 UTC. If an agent still uses it, give it a new token, made at "
        "http://127.0.0.1:8800/tokens."
    )
    assert len(database.errors("etienne", "shop", NOW)) == 2
    assert database.errors("etienne", "cafe", NOW) == []


def test_an_expired_token_whose_share_was_revoked_is_no_error_of_the_app(
    limited: Starlette,
) -> None:
    database = limited.state.apps.database
    sam = a_shop_member(limited, "staff")
    made = NOW - timedelta(days=8)
    shop = TokenLimit("etienne", "shop", "staff")
    _, till = database.create_token(sam.id, "Till", made, [shop], days=7)
    # The owner no longer shares the app: what sam's agent does there is not theirs to see.
    database.remove_member("etienne", "shop", sam.id)

    with running(limited) as address:
        assert app_status(address, till) == 401

    assert database.errors("etienne", "shop", NOW) == []


def test_an_app_s_address_refuses_calls_at_once_over_its_limit(
    settings: Settings, executor: LocalExecutor, clock: Clock
) -> None:
    server = create_app(
        settings, executor=executor, clock=lambda: NOW, limits=limits(clock, at_once=1)
    )
    server.state.apps.import_folder(APPS / "stock", "etienne", "shop")
    token = app_token(server, "staff")

    async def call() -> types.CallToolResult:
        async with app_client(address, "/apps/etienne/shop/mcp", token) as client:
            return await client.call_tool("stock", {})

    async def read() -> str:
        async with app_client(address, "/apps/etienne/shop/mcp", token) as client:
            try:
                await client.read_resource(views.renderer_uri())
            except Exception as error:
                return str(error)
            return "read"

    with running(server) as address:
        # Another call of the app is running.
        with server.state.limits.calls.call("app etienne/shop"):
            refused = anyio.run(call)
            unread = anyio.run(read)
        done = anyio.run(call)
        assert anyio.run(read) == "read"

    assert "at most 1 run together" in unread

    assert refused.is_error
    assert "at most 1 run together" in text(refused)
    assert not done.is_error


def test_the_person_s_endpoint_refuses_calls_over_its_limit(
    limited: Starlette, clock: Clock
) -> None:
    async def calls(count: int) -> list[types.CallToolResult]:
        async with app_client(address, "/me/mcp", "dev") as client:
            return [await client.call_tool("list_apps", {}) for _ in range(count)]

    with running(limited) as address:
        answers = anyio.run(calls, 4)
        clock.now += 60
        (later,) = anyio.run(calls, 1)

    assert [a.is_error for a in answers] == [False, False, False, True]
    assert text(answers[3]) == (
        "Too many calls to your apps: at most 3 a minute. Try again in 60 seconds."
    )
    assert not later.is_error


def test_the_builder_refuses_calls_over_its_limit(
    apps: Apps, settings: Settings, clock: Clock
) -> None:
    async def main() -> None:
        async with BuilderClient(me_server(apps, limits=limits(clock))) as client:
            for _ in range(3):
                await client.call_tool("read_guide", {})
            with pytest.raises(ToolError, match="Too many calls to the builder: at most 3"):
                await client.call_tool("read_guide", {})
            # The person's own tools are counted apart.
            listed = await client.call_tool("list_apps", {})
            assert not listed.is_error
            clock.now += 60
            await client.call_tool("read_guide", {})

    anyio.run(main)


def test_the_builder_counts_runs_in_the_owner_s_sandbox(
    apps: Apps, settings: Settings, clock: Clock
) -> None:
    given = limits(clock, per_minute=100, runs=1)
    given.sandbox_runs.take("sandbox the builder etienne")

    async def main() -> None:
        async with BuilderClient(me_server(apps, limits=given)) as client:
            with pytest.raises(ToolError, match="1 runs in your sandbox in the last hour"):
                await client.call_tool("check_app", {"app": "shop"})
            # What runs nothing in the sandbox still answers.
            await client.call_tool("read_guide", {})

    anyio.run(main)


def test_an_owner_has_at_most_so_many_apps(apps: Apps) -> None:
    few = Settings.from_env(
        {
            "CLIQUET_DATA_DIR": str(apps.settings.data_dir),
            "CLIQUET_DEV_LOGIN": "etienne",
            "CLIQUET_APPS_PER_OWNER": "2",
        }
    )
    builder = Builder(apps, few)
    builder.create_app("etienne", "one")
    builder.create_app("etienne", "two")
    with pytest.raises(BuilderError, match="You have 2 apps, the most an owner can have"):
        builder.create_app("etienne", "three")
    builder.create_app("sam", "one")


def test_a_screen_s_calls_over_the_limit_get_a_429(limited: Starlette, clock: Clock) -> None:
    stock = {"method": "tools/call", "params": {"name": "stock", "arguments": {}}}
    with signed_in(limited) as client:
        answers = [client.post("/apps/etienne/shop/owner/rpc", json=stock) for _ in range(4)]
        other = client.post("/apps/etienne/cafe/owner/rpc", json=stock)
    assert [a.status_code for a in answers] == [200, 200, 200, 429]
    assert answers[3].headers["Retry-After"] == "60"
    assert answers[3].json() == {
        "error": "Too many calls to etienne/shop: at most 3 a minute. Try again in 60 seconds."
    }
    assert other.status_code == 200


def test_the_person_s_endpoint_counts_an_app_s_calls_for_the_app(limited: Starlette) -> None:
    # Calls through /me/mcp and at the app's address share the app's limit.
    read = {"app": "etienne/shop", "tool": "stock", "role": "owner"}
    answers = [call(limited, "etienne", READ, read) for _ in range(4)]
    assert [a.is_error for a in answers] == [False, False, False, True]
    assert text(answers[3]) == (
        "Too many calls to etienne/shop: at most 3 a minute. Try again in 60 seconds."
    )
    assert not call(
        limited, "etienne", READ, {"app": "etienne/cafe", "tool": "stock", "role": "owner"}
    ).is_error
