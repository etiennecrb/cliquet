"""Each owner's sandbox time per month, and the budgets that refuse a call past it."""

import logging
import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
import pytest
from sqlalchemy.exc import OperationalError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from cliquet_server.app import create_app
from cliquet_server.db import Database, Right
from cliquet_server.executor import LocalExecutor, SandboxClient
from cliquet_server.me import READ, SHOW, WRITE
from cliquet_server.sandbox_time import SandboxTime, runs_app_code
from cliquet_server.settings import Settings, SettingsError

from .server_helpers import (
    APPS,
    NOW,
    a_shop_member,
    app_client,
    app_token,
    call,
    csrf_for,
    running,
    signed_in,
    text,
    tools,
    unexpected,
    with_accent,
)

BASE = "http://127.0.0.1:8800"
# When November 2026's budgets reset.
RESET = datetime(2026, 12, 1, tzinfo=UTC)
OWNER_WORDS = (
    "Your apps have used this month's sandbox time, 10 minutes, so no call runs in them until "
    "it resets on 1 December 2026 at 00:00 UTC. The Account tab of your settings shows it: "
    f"{BASE}/account"
)
OTHERS_WORDS = (
    "The apps of etienne have used this month's sandbox time, so no call runs in them until it "
    "resets on 1 December 2026 at 00:00 UTC."
)
SERVER_WORDS = (
    "This server has used this month's sandbox time, so no app's code runs until it resets on "
    "1 December 2026 at 00:00 UTC."
)
STOCK = {"app": "etienne/shop", "tool": "stock", "role": "owner"}
SALE = {"sku": "TEA-SEN", "quantity": 1, "receipt": "R-0951", "app_remark": "A sale"}
SCREEN_CALL = {"method": "tools/call", "params": {"name": "stock", "arguments": {}}}


class Clock:
    """The server's clock, which a test moves by hand, and which moves `tick` seconds at
    each reading, once a test sets it, so that time passes in every call."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now
        self.tick = 0.0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=self.tick)
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def metered(
    database: Database,
    clock: Clock,
    *,
    per_owner: int | None = 10,
    per_server: int | None = None,
    per_plus_owner: int | None = None,
) -> SandboxTime:
    return SandboxTime(
        database,
        per_owner=per_owner,
        per_server=per_server,
        base_url=BASE,
        clock=clock,
        per_plus_owner=per_plus_owner,
    )


def spend(meter: SandboxTime, clock: Clock, login: str, seconds: float) -> None:
    """One call that keeps the owner's sandbox busy `seconds`."""
    with meter.busy(login):
        clock.advance(seconds)


def wait(clock: Clock) -> int:
    """The seconds a refusal says to wait: until the budgets reset."""
    return math.ceil((RESET - clock.now).total_seconds())


@pytest.fixture
def owners(database: Database) -> Database:
    """The database, with two owners, etienne and lou."""
    for login in ("etienne", "lou"):
        database.create_app(login, "shop", NOW)
    return database


@pytest.fixture
def clock() -> Clock:
    return Clock()


def test_calls_at_once_count_once(owners: Database, clock: Clock) -> None:
    meter = metered(owners, clock)
    with meter.busy("etienne"):
        clock.advance(10)
        with meter.busy("etienne"):
            clock.advance(20)
        clock.advance(10)
    # Another owner's sandbox is counted apart.
    spend(meter, clock, "lou", 5)
    assert (meter.used("etienne"), meter.used("lou")) == (40, 5)
    # A call that runs counts as it goes.
    with meter.busy("etienne"):
        clock.advance(15)
        assert meter.used("etienne") == 55


def test_an_owner_past_their_budget_is_refused_until_the_first(
    owners: Database, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    meter = metered(owners, clock)
    spend(meter, clock, "etienne", 599)
    assert meter.refused("etienne") is None
    with meter.busy("etienne"):
        clock.advance(1)
        with caplog.at_level(logging.WARNING):
            refused, again = meter.refused("etienne"), meter.refused("etienne")
    assert refused is not None and again is not None and not refused.server
    assert (str(refused), refused.told(owner=False)) == (OWNER_WORDS, OTHERS_WORDS)
    assert refused.retry_after == wait(clock)
    assert meter.refused("lou") is None
    # Logged once an hour, as a warning: an owner's budget is no alarm.
    (logged,) = [one for one in caplog.records if one.name == "cliquet_server.sandbox_time"]
    assert logged.levelno == logging.WARNING
    assert logged.getMessage().startswith("etienne's apps used their 10 minutes")
    # The 1st, in UTC, counts from nothing.
    clock.now = RESET
    assert meter.refused("etienne") is None
    assert meter.used("etienne") == 0


def test_past_the_server_s_ceiling_every_sandbox_is_refused(
    owners: Database, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    meter = metered(owners, clock, per_owner=None, per_server=10)
    spend(meter, clock, "etienne", 300)
    with meter.busy("lou"):
        clock.advance(299)
        assert meter.refused("etienne") is None
        clock.advance(1)
        with caplog.at_level(logging.WARNING):
            refusals = [meter.refused(login) for login in ("etienne", "lou", "etienne")]
    for refused in refusals:
        assert refused is not None and refused.server
        assert refused.told(owner=True) == refused.told(owner=False) == SERVER_WORDS
    # One error an hour, in fixed words an operator searches for, never `unexpected:`.
    (logged,) = [one for one in caplog.records if one.levelno == logging.ERROR]
    assert logged.getMessage() == (
        "sandbox budget: the server used its 10 minutes of sandbox time for 2026-11, so every "
        "call into a sandbox is refused until 1 December 2026 at 00:00 UTC"
    )
    assert not unexpected(caplog)


def test_a_plus_owner_runs_past_the_free_budget_up_to_their_own(
    owners: Database, clock: Clock
) -> None:
    owners.grant("lou", Right.PLUS)
    meter = metered(owners, clock, per_plus_owner=30)
    assert (meter.budget("etienne"), meter.budget("lou")) == (10, 30)
    spend(meter, clock, "etienne", 600)
    spend(meter, clock, "lou", 600)
    assert meter.refused("etienne") is not None
    assert meter.refused("lou") is None
    spend(meter, clock, "lou", 1200)
    refused = meter.refused("lou")
    assert refused is not None and not refused.server
    assert "this month's sandbox time, 30 minutes," in str(refused)


def test_a_grant_or_a_withdrawal_of_plus_holds_at_the_next_write(
    owners: Database, clock: Clock
) -> None:
    meter = metered(owners, clock, per_plus_owner=30)
    spend(meter, clock, "lou", 600)
    owners.grant("lou", Right.PLUS)
    # Read again with the counts, not at each check.
    assert meter.refused("lou") is not None
    meter.write()
    assert meter.refused("lou") is None
    owners.withdraw("lou", Right.PLUS)
    meter.write()
    assert meter.refused("lou") is not None


def test_the_server_s_ceiling_refuses_a_plus_owner(owners: Database, clock: Clock) -> None:
    owners.grant("lou", Right.PLUS)
    meter = metered(owners, clock, per_plus_owner=30, per_server=10)
    spend(meter, clock, "lou", 600)
    refused = meter.refused("lou")
    assert refused is not None and refused.server


def test_without_a_plus_budget_a_plus_owner_has_the_free_one(
    owners: Database, clock: Clock
) -> None:
    owners.grant("lou", Right.PLUS)
    meter = metered(owners, clock)
    assert not meter.is_plus("lou")
    assert meter.budget("lou") == 10
    spend(meter, clock, "lou", 600)
    assert meter.refused("lou") is not None


def test_the_logins_with_a_right_leave_out_the_others(owners: Database) -> None:
    assert owners.logins_with(Right.PLUS) == frozenset()
    owners.grant("lou", Right.PLUS)
    owners.grant("etienne", Right.BUILD)
    assert owners.logins_with(Right.PLUS) == {"lou"}


def test_a_call_across_the_first_is_split_between_the_months(owners: Database) -> None:
    clock = Clock(datetime(2026, 11, 30, 23, 59, 30, tzinfo=UTC))
    meter = metered(owners, clock, per_owner=1)
    spend(meter, clock, "etienne", 50)
    # 30 seconds in November, 20 in December, whose minute is still left.
    assert meter.used("etienne") == 20
    assert meter.refused("etienne") is None
    meter.write()
    assert owners.sandbox_time("2026-11") == {"etienne": 30}
    assert owners.sandbox_time("2026-12") == {"etienne": 20}


def test_the_counts_are_written_and_read_back(owners: Database, clock: Clock) -> None:
    meter = metered(owners, clock)
    with meter.busy("etienne"):
        clock.advance(90.5)
        # A long call is written as it goes, in whole seconds: the rest waits.
        meter.write()
        assert owners.sandbox_time("2026-11") == {"etienne": 90}
        clock.advance(30)
    meter.write()
    assert owners.sandbox_time("2026-11") == {"etienne": 120}
    # Half a second waits for more.
    meter.write()
    assert owners.sandbox_time("2026-11") == {"etienne": 120}
    # As the server starts again.
    assert metered(owners, clock).used("etienne") == 120


def test_a_failed_write_keeps_the_time(
    owners: Database,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    meter = metered(owners, clock)
    spend(meter, clock, "etienne", 60)

    def refused(seconds: Any) -> None:
        raise OperationalError("insert", {}, Exception("disk I/O error"))

    with monkeypatch.context() as patched, caplog.at_level(logging.WARNING):
        patched.setattr(owners, "add_sandbox_time", refused)
        meter.write()
    assert "the sandbox time couldn't be written" in caplog.text
    assert owners.sandbox_time("2026-11") == {}
    spend(meter, clock, "etienne", 15)
    meter.write()
    assert owners.sandbox_time("2026-11") == {"etienne": 75}


def test_a_failed_read_of_the_plus_owners_keeps_the_time(
    owners: Database, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    meter = metered(owners, clock, per_plus_owner=30)
    spend(meter, clock, "etienne", 60.5)

    def broken(right: Right) -> frozenset[str]:
        raise ValueError("'admin' is not a valid Right")

    with monkeypatch.context() as patched:
        patched.setattr(owners, "logins_with", broken)
        with pytest.raises(ValueError):
            meter.write()
    assert owners.sandbox_time("2026-11") == {"etienne": 60}
    # The half second not written yet waits for the next write.
    spend(meter, clock, "etienne", 0.5)
    meter.write()
    assert owners.sandbox_time("2026-11") == {"etienne": 61}


def test_this_month_and_the_one_before_are_kept(owners: Database, clock: Clock) -> None:
    owners.add_sandbox_time(
        {("etienne", "2026-09"): 60, ("etienne", "2026-10"): 60, ("lou", "2026-11"): 60}
    )
    # A deleted account's time stays, so the whole server's never drops.
    owners.close_account("lou", NOW)
    meter = metered(owners, clock, per_owner=None, per_server=1)
    assert owners.sandbox_time("2026-09") == {}
    assert owners.sandbox_time("2026-10") == {"etienne": 60}
    refused = meter.refused("etienne")
    assert refused is not None and refused.server


def test_the_history_and_the_transactions_never_reach_the_sandbox() -> None:
    outside = [
        "app_history",
        "app_ask_for_file",
        "app_begin_transaction",
        "app_transaction_calls",
        "app_commit_transaction",
        "app_rollback_transaction",
        "ui://cliquet/screens",
    ]
    assert not any(runs_app_code(name) for name in outside)
    assert all(runs_app_code(name) for name in ("stock", "app_revert", "app_changes_since"))


def test_the_budgets_are_settings(tmp_path: Path) -> None:
    base = {"CLIQUET_DATA_DIR": str(tmp_path)}
    defaults = Settings.from_env(base)
    assert (defaults.sandbox_minutes_per_owner_month, defaults.sandbox_minutes_per_month) == (
        None,
        None,
    )
    given = Settings.from_env({**base, "CLIQUET_SANDBOX_MINUTES_PER_MONTH": "6000"})
    assert given.sandbox_minutes_per_month == 6000
    with pytest.raises(SettingsError, match="CLIQUET_SANDBOX_MINUTES_PER_OWNER_MONTH"):
        Settings.from_env({**base, "CLIQUET_SANDBOX_MINUTES_PER_OWNER_MONTH": "0"})
    free, plus = (
        "CLIQUET_SANDBOX_MINUTES_PER_OWNER_MONTH",
        "CLIQUET_SANDBOX_MINUTES_PER_PLUS_OWNER_MONTH",
    )
    both = Settings.from_env({**base, free: "10", plus: "30"})
    assert both.sandbox_minutes_per_plus_owner_month == 30
    # A Plus budget never caps Plus owners more than free ones.
    with pytest.raises(SettingsError, match=f"{plus} needs {free}"):
        Settings.from_env({**base, plus: "30"})
    with pytest.raises(SettingsError, match=f"{plus} \\(5\\) is below {free} \\(10\\)"):
        Settings.from_env({**base, free: "10", plus: "5"})


def budgeted(settings: Settings, **env: str) -> Settings:
    return Settings.from_env(
        {"CLIQUET_DATA_DIR": str(settings.data_dir), "CLIQUET_DEV_LOGIN": "etienne", **env}
    )


def served(settings: Settings, executor: LocalExecutor, clock: Clock, **env: str) -> Starlette:
    """The server, with the budgets `env` sets, and etienne/shop and lou/cafe."""
    server = create_app(budgeted(settings, **env), executor=executor, clock=clock)
    apps = server.state.apps
    apps.import_folder(APPS / "stock", "etienne", "shop", email="etienne@example.com")
    apps.import_folder(APPS / "stock", "lou", "cafe")
    apps.page_wait = apps.start_wait
    return server


@pytest.fixture
def capped(settings: Settings, executor: LocalExecutor, clock: Clock) -> Starlette:
    """The server, each owner's sandbox at 10 minutes a month."""
    return served(settings, executor, clock, CLIQUET_SANDBOX_MINUTES_PER_OWNER_MONTH="10")


def meter_of(server: Starlette) -> SandboxTime:
    meter = server.state.apps.meter
    assert meter is not None
    return meter


def test_past_an_owner_s_budget_their_calls_are_refused(capped: Starlette, clock: Clock) -> None:
    a_shop_member(capped, "staff")
    spend(meter_of(capped), clock, "etienne", 600)
    mine = call(capped, "etienne", READ, STOCK)
    theirs = call(capped, "sam", READ, {**STOCK, "role": "staff"})
    shown = call(capped, "sam", SHOW, {"app": "etienne/shop", "view": "till", "role": "staff"})
    assert [text(one) for one in (mine, theirs, shown)] == [OWNER_WORDS, *[OTHERS_WORDS] * 2]
    assert mine.is_error and theirs.is_error and shown.is_error
    # The history needs no sandbox.
    assert not call(capped, "etienne", READ, {**STOCK, "tool": "app_history"}).is_error
    built, guide = tools(capped, ("run_tests", {"app": "shop"}), ("read_guide", {}))
    assert built.is_error and text(built) == OWNER_WORDS
    assert not guide.is_error
    with signed_in(capped) as client:
        screen = client.post("/apps/etienne/shop/owner/rpc", json=SCREEN_CALL)
    assert screen.status_code == 429
    assert screen.headers["Retry-After"] == str(wait(clock))
    assert screen.json() == {"error": OWNER_WORDS}
    # Another owner's apps still run.
    assert not call(capped, "lou", READ, {**STOCK, "app": "lou/cafe"}).is_error


def test_past_an_owner_s_budget_a_screen_gets_a_429_and_an_agent_a_tool_error(
    capped: Starlette, clock: Clock
) -> None:
    sam = a_shop_member(capped, "staff")
    token = app_token(capped, "staff", person=sam)
    spend(meter_of(capped), clock, "etienne", 600)

    async def calls(address: str) -> list[types.CallToolResult]:
        async with app_client(address, "/apps/etienne/shop/mcp", token) as client:
            return [
                await client.call_tool("stock", {}),
                await client.call_tool("stock", {}),
                await client.call_tool("app_history", {}),
            ]

    with running(capped) as address:
        first, second, history = anyio.run(calls, address)
    # The server stopped, as at a deploy: the screen's call would start the app again.
    with signed_in(capped) as client:
        refused = client.post("/apps/etienne/shop/owner/rpc", json=SCREEN_CALL)
    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == str(wait(clock))
    assert refused.json() == {"error": OWNER_WORDS}
    assert first.is_error and second.is_error and not history.is_error
    assert text(first) == OTHERS_WORDS
    # Its owner sees it among the app's errors, once an hour.
    (kept,) = capped.state.apps.database.errors("etienne", "shop", clock.now)
    assert (
        kept.text
        == f"sam with the token 'shop as staff 0' was refused at the app's address: {OWNER_WORDS}"
    )
    assert kept.tool == "stock"


def test_past_an_owner_s_budget_the_console_neither_restores_nor_builds(
    capped: Starlette, clock: Clock
) -> None:
    apps = capped.state.apps
    first = apps.repo("etienne", "shop").publishes()[0].commit
    code = apps.repo("etienne", "shop").code()
    apps.publish("etienne", "shop", {**code, "app.py": code["app.py"] + b"\n"}, note="Again")
    spend(meter_of(capped), clock, "etienne", 600)
    with signed_in(capped) as client:
        restored = client.post(
            f"/manage/etienne/shop/versions/{first}/restore",
            data={"csrf": csrf_for(capped, client)},
            follow_redirects=False,
        )
        preview = client.get("/build/shop/preview", follow_redirects=False)
    assert "Not restored: Your apps have used this month" in restored.text
    assert apps.repo("etienne", "shop").publishes()[0].note == "Again"
    assert preview.status_code == 429
    assert preview.headers["Retry-After"] == str(wait(clock))
    assert "Your apps have used this month" in preview.text


def test_past_an_owner_s_budget_their_apps_don_t_start(
    capped: Starlette, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    apps = capped.state.apps
    apps.forget("etienne", "shop")
    spend(meter_of(capped), clock, "etienne", 600)
    with caplog.at_level(logging.WARNING):
        refused = call(capped, "etienne", READ, STOCK)
        (overview,) = tools(capped, ("read_app_overview", {"app": "shop"}))
    assert text(refused) == OWNER_WORDS
    assert OWNER_WORDS in text(overview)
    # Neither kept as a failed start, nor logged as a fault or under an id.
    assert apps.failed("etienne", "shop") is None
    assert apps.current("etienne", "shop") is None
    assert apps.database.errors("etienne", "shop", clock.now) == []
    assert not unexpected(caplog)
    assert not [one for one in caplog.records if one.getMessage().startswith("Error e-")]


def test_a_call_that_began_before_the_budget_ran_out_finishes(
    capped: Starlette, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    meter = meter_of(capped)
    spend(meter, clock, "etienne", 599)
    run = SandboxClient.run

    def slow(self: SandboxClient, *args: Any) -> dict[str, Any]:
        # The budget runs out while the sandbox works.
        clock.advance(30)
        return run(self, *args)

    monkeypatch.setattr(SandboxClient, "run", slow)
    done = call(capped, "etienne", WRITE, {**STOCK, "tool": "record_sale", "arguments": SALE})
    assert not done.is_error
    assert meter.used("etienne") >= 629
    assert text(call(capped, "etienne", READ, STOCK)) == OWNER_WORDS


def test_past_the_server_s_ceiling_every_owner_is_refused(
    settings: Settings, executor: LocalExecutor, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    server = served(settings, executor, clock, CLIQUET_SANDBOX_MINUTES_PER_MONTH="10")
    spend(meter_of(server), clock, "lou", 600)
    with caplog.at_level(logging.WARNING):
        mine = call(server, "etienne", READ, STOCK)
        (built,) = tools(server, ("check_app", {"app": "shop"}))
    assert text(mine) == text(built) == SERVER_WORDS
    assert [one.getMessage()[:16] for one in caplog.records if one.levelno == logging.ERROR] == [
        "sandbox budget: "
    ]
    assert not unexpected(caplog)


def test_the_warm_up_counts_nothing_and_leaves_an_owner_past_their_budget(
    capped: Starlette, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    apps = capped.state.apps
    for login, name in (("etienne", "shop"), ("lou", "cafe")):
        apps.forget(login, name)
        apps.database.note_apps_used(login, NOW - timedelta(days=1))
    meter = meter_of(capped)
    spend(meter, clock, "etienne", 600)
    asked: list[str] = []
    sandbox_of = apps.executor.sandbox
    monkeypatch.setattr(
        apps.executor, "sandbox", lambda login: asked.append(login) or sandbox_of(login)
    )
    # Time passes at every reading of the clock, so whatever is counted counts.
    clock.tick = 1.0
    for owner in apps.warm_up():
        owner.result(timeout=60)
    assert apps.current("lou", "cafe") is not None
    assert apps.current("etienne", "shop") is None
    assert "lou" in asked and "etienne" not in asked
    assert meter.used("lou") == 0


def test_the_counts_are_written_as_the_server_stops_and_read_as_it_starts(
    settings: Settings, executor: LocalExecutor, clock: Clock
) -> None:
    env = {"CLIQUET_SANDBOX_MINUTES_PER_OWNER_MONTH": "10"}
    first = served(settings, executor, clock, **env)
    with TestClient(first):
        spend(meter_of(first), clock, "etienne", 600)
    again = create_app(budgeted(settings, **env), executor=executor, clock=clock)
    assert meter_of(again).used("etienne") == 600
    assert text(call(again, "etienne", READ, STOCK)) == OWNER_WORDS


def test_without_a_budget_nothing_is_counted_or_refused(server: Starlette) -> None:
    assert server.state.apps.meter is None
    assert not any(call(server, "etienne", READ, STOCK).is_error for _ in range(3))
    with signed_in(server) as client:
        page = client.get("/account").text
    assert "Sandbox time" not in page
    assert server.state.database.sandbox_time("2026-11") == {}


def test_the_account_page_shows_the_month_s_sandbox_time(capped: Starlette, clock: Clock) -> None:
    spend(meter_of(capped), clock, "etienne", 150)
    with signed_in(capped) as client:
        page = client.get("/account").text
    assert "Sandbox time" in page
    assert "<strong>2 of 10 minutes</strong>" in page
    assert "It resets on 1 December 2026 at 00:00 UTC." in page
    assert "Plus" not in page


def test_the_account_page_shows_a_plus_owner_their_own_budget(
    settings: Settings, executor: LocalExecutor, clock: Clock
) -> None:
    server = served(
        settings,
        executor,
        clock,
        CLIQUET_SANDBOX_MINUTES_PER_OWNER_MONTH="10",
        CLIQUET_SANDBOX_MINUTES_PER_PLUS_OWNER_MONTH="30",
    )
    server.state.database.grant("etienne", Right.PLUS)
    meter_of(server).write()
    with signed_in(server) as client:
        page = client.get("/account").text
    assert "<strong>0 of 30 minutes</strong> of sandbox time this month, your Plus budget:" in page


@pytest.mark.slow
def test_every_call_into_a_sandbox_is_counted(
    capped: Starlette, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Whatever reaches the sandbox, on any surface, runs while its owner's is counted busy.
    meter = meter_of(capped)
    uncounted: list[str] = []
    send = SandboxClient._send

    def counted(self: SandboxClient, method: str, path: str, *args: Any) -> Any:
        owner = re.match(r"/apps/([^/]+)/", path)
        if not (owner.group(1) in meter._busy if owner else meter._busy):
            uncounted.append(f"{method} {path}")
        return send(self, method, path, *args)

    monkeypatch.setattr(SandboxClient, "_send", counted)
    # Each stop forgets the started apps: the warm-up at the next start, which counts nothing,
    # is tested apart.
    monkeypatch.setattr(capped.state.apps, "warm_up", list)
    clock.tick = 0.01
    sam = a_shop_member(capped, "staff")
    token = app_token(capped, "staff", person=sam)
    capped.state.apps.forget("etienne", "shop")
    # A person's start, then each of the person's tools.
    assert not call(capped, "etienne", READ, STOCK).is_error
    assert not call(
        capped, "etienne", WRITE, {**STOCK, "tool": "record_sale", "arguments": SALE}
    ).is_error
    assert not call(
        capped, "sam", SHOW, {"app": "etienne/shop", "view": "till", "role": "staff"}
    ).is_error
    with signed_in(capped) as client:
        assert client.post("/apps/etienne/shop/owner/rpc", json=SCREEN_CALL).status_code == 200

    async def agent(address: str) -> types.CallToolResult:
        async with app_client(address, "/apps/etienne/shop/mcp", token) as client:
            return await client.call_tool("stock", {})

    with running(capped) as address:
        assert not anyio.run(agent, address).is_error
    answers = tools(
        capped,
        ("check_app", {"app": "shop"}),
        ("run_tests", {"app": "shop", "only": "sale"}),
        ("start_preview", {"app": "shop"}),
        ("try_call", {"app": "shop", "tool": "stock", "role": "owner"}),
        ("show_preview", {"app": "shop", "view": "till", "role": "owner"}),
    )
    assert not any(answer.is_error for answer in answers), [text(one) for one in answers]
    with_accent(capped, "#123456")
    assert uncounted == []
    assert meter.used("etienne") > 0
