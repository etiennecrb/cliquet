"""An app starts in the background: a request waits for it a little, then says it is starting."""

import threading
import time
import traceback
from concurrent.futures import Future
from datetime import datetime, timedelta

import anyio
import httpx
import pytest
from fastmcp import Client as BuilderClient
from fastmcp.exceptions import ToolError
from sqlalchemy.exc import OperationalError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

import cliquet_server.apps as apps_module
from cliquet.screenshot import Browser, find_browser
from cliquet_server.apps import Apps, AppStarting, Live, SandboxStarting, UnknownApp
from cliquet_server.db import SandboxInfo
from cliquet_server.executor import BuildProblems, SandboxError
from cliquet_server.me import DESCRIBE, LIST
from cliquet_server.pages import starting
from cliquet_server.settings import Settings

from .server_helpers import (
    NOW,
    Watched,
    a_shop_member,
    app_token,
    blocked_build,
    call,
    example_code,
    failing_build,
    me_server,
    member,
    needs_browser,
    one_unexpected,
    published_before_roles_were_kept,
    rpc_refusal,
    signed_in,
    text,
    unexpected,
    wait_for,
)


def started_now(apps: Apps) -> Live:
    """The shop if it is started, or `AppStarting` at once."""
    return anyio.run(lambda: apps.started("etienne", "shop", wait=0))


def started_now_or_starting(apps: Apps) -> str:
    """Whether the shop is started, or starting, now."""
    try:
        started_now(apps)
    except AppStarting:
        return "starting"
    return "started"


def test_a_request_waits_for_a_quick_start(shop, apps: Apps) -> None:
    apps.forget("etienne", "shop")

    live = anyio.run(lambda: apps.started("etienne", "shop"))

    assert live.runtime.manifest["name"] == "shop"
    assert started_now(apps) is live


def test_a_request_doesn_t_wait_for_a_slow_start(shop, apps: Apps, monkeypatch) -> None:
    apps.forget("etienne", "shop")

    with blocked_build(apps, "shop", monkeypatch) as blocked:
        with pytest.raises(AppStarting):
            started_now(apps)
        assert blocked.building.wait(timeout=30)
        # The start goes on: asking again joins it.
        with pytest.raises(AppStarting):
            started_now(apps)
        blocked.release()
        live = apps.live("etienne", "shop")

    assert started_now(apps) is live
    assert blocked.builds == 1


def test_a_page_waits_for_a_start_only_a_moment(shop, apps: Apps, monkeypatch) -> None:
    # A person sees that the app is starting at once, rather than a blank page for 20 s.
    apps.forget("etienne", "shop")
    apps.page_wait = 0.1

    with blocked_build(apps, "shop", monkeypatch) as blocked:
        with apps.page(), pytest.raises(AppStarting):
            anyio.run(lambda: apps.started("etienne", "shop"))
        assert blocked.building.wait(timeout=30)
        blocked.release()
        live = apps.live("etienne", "shop")

    with apps.page():
        assert anyio.run(lambda: apps.started("etienne", "shop")) is live


def test_a_web_manifest_waits_for_a_start_as_a_call_does(server: Starlette, monkeypatch) -> None:
    # A phone fetches it as it adds the page to its home screen, and doesn't ask again.
    apps: Apps = server.state.apps
    apps.start_wait, apps.page_wait = 0.5, 0
    apps.forget("etienne", "shop")

    with blocked_build(apps, "shop", monkeypatch), signed_in(server) as client:
        began = time.monotonic()
        manifest = client.get("/apps/etienne/shop/owner/manifest.webmanifest")
        waited = time.monotonic() - began

    assert manifest.status_code == 503 and waited >= apps.start_wait


def test_the_starting_page_s_probe_takes_no_notice(server: Starlette) -> None:
    # Its probe of whether the page is ready is thrown away: the reload shows the notice.
    database = server.state.apps.database
    etienne = database.person_by_login("etienne")
    assert etienne is not None
    server.state.apps.notices.tell(etienne.id, "A notice for etienne.")

    with signed_in(server) as client:
        probed = client.get("/manage/etienne/shop", headers={"Cliquet-Probe": "starting"})
        shown = client.get("/manage/etienne/shop")

    assert probed.status_code == shown.status_code == 200
    assert "A notice for etienne." not in probed.text
    assert "A notice for etienne." in shown.text


def test_a_failed_start_is_tried_again(shop, apps: Apps, monkeypatch) -> None:
    apps.forget("etienne", "shop")
    apps.failed_for = 0

    with failing_build(apps, "shop", monkeypatch), pytest.raises(SandboxError, match="away"):
        apps.live("etienne", "shop")

    assert apps.live("etienne", "shop").runtime.manifest["name"] == "shop"


def test_a_publish_during_a_start_is_what_is_served(shop, apps: Apps, monkeypatch) -> None:
    apps.forget("etienne", "shop")
    code = example_code()
    code["app.py"] += b"\n# A new version.\n"
    published: list[Live] = []

    with blocked_build(apps, "shop", monkeypatch) as blocked:
        with pytest.raises(AppStarting):
            started_now(apps)
        assert blocked.building.wait(timeout=30)
        publisher = threading.Thread(
            target=lambda: published.append(apps.publish("etienne", "shop", code, note="New"))
        )
        publisher.start()
        blocked.release()
        publisher.join(timeout=60)

    assert not publisher.is_alive()
    assert apps.live("etienne", "shop") is published[0]
    assert published[0].version != shop.version


def test_a_forget_during_a_start_keeps_nothing(shop, apps: Apps, monkeypatch) -> None:
    apps.forget("etienne", "shop")

    with blocked_build(apps, "shop", monkeypatch) as blocked:
        start = apps.start("etienne", "shop")
        assert blocked.building.wait(timeout=30)
        forgetting = threading.Thread(target=apps.forget, args=("etienne", "shop"))
        forgetting.start()
        blocked.release()
        start.result(timeout=60)
        forgetting.join(timeout=60)

    assert not forgetting.is_alive()
    assert ("etienne", "shop") not in apps._cache


def test_a_start_that_ends_after_close_keeps_nothing(shop, apps: Apps, monkeypatch) -> None:
    other = Apps(apps.settings, apps.database, apps.executor, clock=lambda: NOW)

    with blocked_build(apps, "shop", monkeypatch) as blocked:
        start = other.start("etienne", "shop")
        assert blocked.building.wait(timeout=30)
        other.close()
        blocked.release()
        with pytest.raises(SandboxError, match="stopped"):
            start.result(timeout=60)

    assert other._cache == {}


def test_every_door_says_the_app_is_starting(server: Starlette, address: str, monkeypatch) -> None:
    apps: Apps = server.state.apps
    apps.start_wait = apps.page_wait = 0
    database = apps.database
    sam = a_shop_member(server, "staff")
    owner_token = app_token(server, "owner")
    # sam's token is for staff, and sam now holds only the role named owner.
    sam_token = app_token(server, "staff", person=sam)
    database.remove_member("etienne", "shop", sam.id)
    member(database, sam, "etienne", "shop", "owner")
    # Only a list of an app whose roles aren't kept yet starts it.
    published_before_roles_were_kept(database, "etienne", "shop")
    apps.forget("etienne", "shop")

    with blocked_build(apps, "shop", monkeypatch):
        endpoint = httpx.post(
            f"{address}/apps/etienne/shop/mcp", headers={"Authorization": f"Bearer {owner_token}"}
        )
        # A member without the role: whether the role exists isn't known yet either.
        no_role = httpx.post(
            f"{address}/apps/etienne/shop/mcp", headers={"Authorization": f"Bearer {sam_token}"}
        )
        with signed_in(server) as client:
            opened = client.get("/apps/etienne/shop", follow_redirects=False)
            page = client.get("/apps/etienne/shop/owner")
            manifest = client.get("/apps/etienne/shop/owner/manifest.webmanifest")
            rpc = client.post(
                "/apps/etienne/shop/owner/rpc",
                json={"method": "tools/list"},
                headers={"origin": "http://testserver"},
            )
            console = client.get("/manage/etienne/shop")
        listed = call(server, "etienne", LIST, {})
        describe_app = call(server, "etienne", DESCRIBE, {"app": "shop"})

    for answer in (endpoint, no_role, rpc, manifest):
        assert answer.headers["retry-after"] == "30"
    # At the app's address, a client shows the sentence of a JSON-RPC error.
    for answer in (endpoint, no_role):
        assert rpc_refusal(answer, 503) == "the app is starting: try again in a minute"
    for answer in (rpc, manifest):
        assert answer.status_code == 503
        assert answer.json() == {"error": "the app is starting: try again in a minute"}
    for answer in (opened, page, console):
        assert answer.status_code == 503
        assert "The app is starting" in answer.text
        # It asks for itself again until the app has started, then opens.
        assert "fetch(location.href" in answer.text
    (row,) = listed.structured_content["apps"]
    assert row["problem"] == "It isn't ready yet: list your apps again in a minute, or open it."
    assert describe_app.is_error
    assert text(describe_app) == "etienne/shop is starting: call again in a minute."


def warmed(apps: Apps, *, used: bool = True) -> None:
    """Warm the apps up, and wait until it is done: as after a day of use of every published
    app, unless not `used`."""
    if used:
        for info in apps.published():
            apps.database.note_apps_used(info.login, NOW - timedelta(days=1))
    for owner in apps.warm_up():
        owner.result(timeout=60)


def test_warm_up_starts_every_published_app(shop, apps: Apps, monkeypatch) -> None:
    apps.forget("etienne", "shop")
    asked: list[str] = []
    sandbox_of = apps.executor.sandbox
    monkeypatch.setattr(
        apps.executor, "sandbox", lambda login: asked.append(login) or sandbox_of(login)
    )
    # An owner with a sandbox and only a draft: their sandbox is brought up to date too.
    apps.database.save_sandbox(SandboxInfo("sam", "cliquet-sam", "http://sam", "t", "a" * 64, NOW))
    apps.database.note_apps_used("sam", NOW - timedelta(days=6))

    warmed(apps)

    assert started_now(apps).runtime.manifest["name"] == "shop"
    assert {"etienne", "sam"} <= set(asked)


def test_warm_up_brings_up_recent_owners_first_and_sweeps_everyone(apps: Apps, monkeypatch) -> None:
    # One owner at a time, so the order they are warmed in is the order they are taken in.
    monkeypatch.setattr(apps_module, "OWNERS_WARMED_AT_ONCE", 1)
    used = {
        "camille": NOW - timedelta(days=7, minutes=1),
        "dana": NOW - timedelta(days=6),
        "sam": NOW - timedelta(hours=1),
        "lou": None,
    }
    for login, when in used.items():
        apps.database.save_sandbox(SandboxInfo(login, login, "http://x", "t", "a", NOW))
        if when is not None:
            apps.database.note_apps_used(login, when)
    swept: list[set[str]] = []
    monkeypatch.setattr(apps.executor, "sweep", swept.append)
    taken: list[str] = []
    monkeypatch.setattr(apps, "_warm_owner", lambda login, names, closes: taken.append(login))

    warmed(apps, used=False)

    # The sweep keeps every owner's sandbox: one not warmed is set up on its first request.
    assert swept == [set(used)]
    assert taken == ["sam", "dana"]


def test_a_use_is_kept_once_every_ten_minutes(shop, apps: Apps, monkeypatch) -> None:
    apps.forget("etienne", "shop")
    clock = [1000.0]
    apps.monotonic = lambda: clock[0]
    # Neither the warm-up nor the start it makes is a use.
    warmed(apps)
    assert apps.database.apps_used_since(NOW) == []
    kept: list[str] = []
    note = apps.database.note_apps_used
    monkeypatch.setattr(
        apps.database, "note_apps_used", lambda login, when: kept.append(login) or note(login, when)
    )
    # An app's call is, and so is a builder's tool: the first one writes it.
    live = started_now(apps)
    assert live.runtime.check() is None
    assert kept == ["etienne"]
    with apps.sandbox("etienne"):
        pass
    live.runtime.check()
    clock[0] += 599
    live.runtime.check()
    assert kept == ["etienne"]
    clock[0] += 1
    with apps.sandbox("etienne"):
        pass
    assert kept == ["etienne", "etienne"]
    assert apps.database.apps_used_since(NOW) == ["etienne"]


def test_a_use_that_can_t_be_kept_fails_no_call(shop, apps: Apps, monkeypatch, caplog) -> None:
    def locked(login: str, when: datetime) -> None:
        raise OperationalError("update people", {}, Exception("database is locked"))

    monkeypatch.setattr(apps.database, "note_apps_used", locked)
    assert started_now(apps).runtime.check() is None
    assert "etienne's last use couldn't be kept" in caplog.text


def test_a_failed_warm_up_is_logged_and_tried_again(shop, apps: Apps, monkeypatch, caplog) -> None:
    apps.forget("etienne", "shop")

    with failing_build(apps, "shop", monkeypatch):
        warmed(apps)

    # The server's own failure, logged once as it happened: the warm-up adds nothing.
    record = one_unexpected(caplog)
    assert record.getMessage() == "unexpected: etienne/shop can't be started: the sandbox is away"
    assert "didn't start at boot" not in caplog.text
    apps.failed_for = 0
    assert apps.live("etienne", "shop").runtime.manifest["name"] == "shop"


def test_an_app_s_own_build_problems_at_boot_are_only_a_warning(
    shop, apps: Apps, monkeypatch, caplog
) -> None:
    apps.forget("etienne", "shop")
    problems = BuildProblems(["app.py: NameError: name 'pricing' is not defined"])

    with failing_build(apps, "shop", monkeypatch, problems):
        warmed(apps)

    # The app's own failure: its owner reads it, and the log only warns.
    assert not unexpected(caplog)
    assert "etienne/shop didn't start at boot: app.py: NameError" in caplog.text


def test_a_start_that_fails_on_a_request_is_logged_once(shop, apps: Apps, monkeypatch, caplog):
    apps.forget("etienne", "shop")

    with failing_build(apps, "shop", monkeypatch):
        for _ in range(3):
            with pytest.raises(SandboxError, match="the sandbox is away"):
                anyio.run(lambda: apps.started("etienne", "shop"))

    # The start failed once, and is kept for a while: the log has it once, as unexpected.
    record = one_unexpected(caplog)
    assert record.getMessage() == "unexpected: etienne/shop can't be started: the sandbox is away"
    caplog.clear()
    problems = BuildProblems(["app.py: NameError: name 'pricing' is not defined"])
    apps.failed_for = 0
    with failing_build(apps, "shop", monkeypatch, problems), pytest.raises(BuildProblems):
        anyio.run(lambda: apps.started("etienne", "shop"))
    assert not unexpected(caplog)


def test_the_server_warms_up_as_it_boots(server: Starlette, monkeypatch) -> None:
    apps: Apps = server.state.apps
    apps.forget("etienne", "shop")
    apps.database.note_apps_used("etienne", NOW - timedelta(days=1))

    with blocked_build(apps, "shop", monkeypatch) as blocked, TestClient(server) as client:
        assert blocked.building.wait(timeout=30)
        # The server answers while it warms up.
        assert client.get("/healthz").status_code == 200
        blocked.release()


def test_a_failed_start_is_kept_for_a_while(shop, apps: Apps, monkeypatch) -> None:
    apps.forget("etienne", "shop")
    builds: list[str] = []
    client = apps.executor.sandbox("etienne")
    real = type(client).put_version

    def counted(self, login: str, app: str, version: str, files: dict[str, bytes]):
        builds.append(app)
        return real(self, login, app, version, files)

    monkeypatch.setattr(type(client), "put_version", counted)
    with failing_build(apps, "shop", monkeypatch), pytest.raises(SandboxError):
        apps.live("etienne", "shop")

    # Within a while, a request is told why at once, and doesn't build the app again.
    for _ in range(3):
        with pytest.raises(SandboxError, match="the sandbox is away") as kept:
            started_now(apps)
        # The same error, raised afresh: its traceback doesn't grow with each request.
        assert len(traceback.extract_tb(kept.value.__traceback__)) < 10
    assert builds == []
    # After it, the next request starts the app again.
    apps.failed_for = 0
    assert started_now_or_starting(apps) in {"starting", "started"}
    assert apps.live("etienne", "shop").runtime.manifest["name"] == "shop"
    assert builds == ["shop"]


def test_the_builder_tries_a_failed_start_again_at_once(shop, apps: Apps, monkeypatch) -> None:
    apps.forget("etienne", "shop")
    with failing_build(apps, "shop", monkeypatch), pytest.raises(SandboxError):
        apps.live("etienne", "shop")

    # The builder's tools wait for a start of their own, rather than a request's failure.
    assert apps.live("etienne", "shop").runtime.manifest["name"] == "shop"


def builder_answer(apps: Apps, settings: Settings, tool: str, arguments: dict) -> str:
    """What the assistant reads from one of the builder's tools: its error, or "done"."""

    async def main() -> str:
        async with BuilderClient(me_server(apps)) as client:
            try:
                await client.call_tool(tool, arguments)
            except ToolError as error:
                return str(error)
            return "done"

    return anyio.run(main)


def test_a_builder_tool_doesn_t_wait_long_for_a_slow_start(
    shop, apps: Apps, settings: Settings, monkeypatch
) -> None:
    apps.forget("etienne", "shop")
    # Below what an MCP client waits for a call: here, not at all.
    apps.builder_wait = 0

    with blocked_build(apps, "shop", monkeypatch) as blocked:
        answer = builder_answer(apps, settings, "read_app_overview", {"app": "shop"})
        assert blocked.building.wait(timeout=30)
        # The start goes on: a call meanwhile joins it.
        with pytest.raises(AppStarting):
            apps.live("etienne", "shop")
        blocked.release()
        apps.builder_wait = 60
        again = builder_answer(apps, settings, "read_app_overview", {"app": "shop"})

    assert answer == (
        "The app is still starting, which can take a few minutes after a deploy. "
        "Call the same tool again in a minute: the start goes on meanwhile."
    )
    assert again == "done"
    assert blocked.builds == 1


def slow_sandbox(apps: Apps, monkeypatch) -> tuple[list[str], threading.Event]:
    """Make the owner's sandbox start slowly, as a new Sprite does, until the event is set.
    It gives each start that had to wait."""
    sandbox_of = apps.executor.sandbox
    ready = threading.Event()
    waited: list[str] = []

    def slow(login: str):
        if not ready.is_set():
            waited.append(login)
            assert ready.wait(timeout=60), "the test never let the sandbox start"
        return sandbox_of(login)

    monkeypatch.setattr(apps.executor, "sandbox", slow)
    return waited, ready


def test_a_builder_tool_doesn_t_wait_long_for_a_slow_sandbox(
    shop, apps: Apps, settings: Settings, monkeypatch
) -> None:
    apps.builder_wait = 0
    waited, ready = slow_sandbox(apps, monkeypatch)
    try:
        answer = builder_answer(apps, settings, "run_tests", {"app": "shop"})
        # The sandbox's start goes on: a call meanwhile joins it.
        with pytest.raises(SandboxStarting), apps.sandbox("etienne"):
            pass
    finally:
        ready.set()
    apps.builder_wait = 60
    again = builder_answer(apps, settings, "run_tests", {"app": "shop"})

    assert answer == (
        "Your sandbox is still starting, which can take a few minutes after a deploy. "
        "Call the same tool again in a minute: the start goes on meanwhile."
    )
    assert again == "done"
    assert waited == ["etienne"]


def test_one_builder_call_waits_for_its_starts_within_one_deadline(
    shop, apps: Apps, settings: Settings, monkeypatch
) -> None:
    apps.forget("etienne", "shop")
    # The call's clock: the owner's sandbox takes almost all of the call's 45 s on it.
    ticks = [0.0]
    apps.monotonic = lambda: ticks[0]
    sandbox_of = apps.executor.sandbox
    sandbox_waited: list[str] = []

    def slow(login: str):
        if not sandbox_waited:
            sandbox_waited.append(login)
            ticks[0] += apps.builder_wait - 0.2
        return sandbox_of(login)

    monkeypatch.setattr(apps.executor, "sandbox", slow)
    # The live app's start is slow too: its build waits until the test lets it go.
    client = sandbox_of("etienne")
    real = type(client).put_version
    starting = threading.Event()
    released = threading.Event()
    starts: list[str] = []

    def put_version(self, login: str, app: str, version: str, files: dict[str, bytes]):
        if threading.current_thread().name == "start etienne/shop":
            starts.append(version)
            starting.set()
            assert released.wait(timeout=60), "the test never let the app start"
        return real(self, login, app, version, files)

    monkeypatch.setattr(type(client), "put_version", put_version)
    try:
        began = time.monotonic()
        answer = builder_answer(apps, settings, "check_app", {"app": "shop"})
        took = time.monotonic() - began
        # The app's start goes on meanwhile.
        assert starting.wait(timeout=30)
    finally:
        released.set()
    again = builder_answer(apps, settings, "check_app", {"app": "shop"})

    assert sandbox_waited == ["etienne"]
    assert answer == (
        "The app is still starting, which can take a few minutes after a deploy. "
        "Call the same tool again in a minute: the start goes on meanwhile."
    )
    # The wait for the app had what the sandbox left of the call's deadline, not 45 s more.
    assert took < apps.builder_wait / 2
    assert again == "done"
    assert len(starts) == 1


def test_a_sandbox_that_is_up_isn_t_starting_past_the_deadline(shop, apps: Apps) -> None:
    ticks = [0.0]
    apps.monotonic = lambda: ticks[0]
    with apps.builder_call():
        # A build, say, took the whole call's deadline and more.
        ticks[0] += apps.builder_wait + 10
        # Asking a sandbox that is up takes a moment, in a thread: never too long, each time.
        clients = set()
        for _ in range(5):
            with apps.sandbox("etienne") as client:
                clients.add(client)

    assert clients == {apps.executor.sandbox("etienne")}


def test_a_delete_doesn_t_wait_long_for_a_slow_sandbox(
    shop, apps: Apps, settings: Settings, monkeypatch
) -> None:
    apps.builder_wait = 0
    _, ready = slow_sandbox(apps, monkeypatch)
    try:
        answer = builder_answer(apps, settings, "delete_app", {"app": "shop", "confirm": "shop"})
    finally:
        ready.set()
    # The sandbox's start ends before the test does.
    apps.builder_wait = 60
    with apps.sandbox("etienne"):
        pass

    assert answer.startswith("Your sandbox is still starting")
    # Nothing was deleted, and the app is still served.
    assert apps.database.app("etienne", "shop") is not None
    assert apps.current("etienne", "shop") is not None


def test_the_draft_preview_runs_its_last_build_while_its_sandbox_starts(
    server: Starlette, monkeypatch
) -> None:
    apps: Apps = server.state.apps
    server.state.builder.start_preview("etienne", "shop")
    server.state.builder.write_file("etienne", "shop", "instructions.md", "Changed.\n")
    apps.builder_wait = 0
    _, ready = slow_sandbox(apps, monkeypatch)
    try:
        with signed_in(server) as client:
            page = client.get("/build/shop/preview/owner")
    finally:
        ready.set()
    apps.builder_wait = 60
    with apps.sandbox("etienne"):
        pass

    assert page.status_code == 200
    assert "Your sandbox is still starting" in page.text


def test_the_builder_tries_a_failed_sandbox_again_at_once(shop, apps: Apps, monkeypatch) -> None:
    sandbox_of = apps.executor.sandbox
    failures = ["can't reach Sprites"]

    def flaky(login: str):
        if failures:
            raise SandboxError(failures.pop())
        return sandbox_of(login)

    monkeypatch.setattr(apps.executor, "sandbox", flaky)
    with pytest.raises(SandboxError, match="can't reach Sprites"), apps.sandbox("etienne"):
        pass

    with apps.sandbox("etienne") as client:
        assert client is sandbox_of("etienne")


def test_a_list_shows_why_an_app_didn_t_start(server: Starlette, monkeypatch) -> None:
    apps: Apps = server.state.apps
    # Only a list of an app whose roles aren't kept yet starts it.
    published_before_roles_were_kept(apps.database, "etienne", "shop")
    apps.forget("etienne", "shop")

    with failing_build(apps, "shop", monkeypatch) as failed:
        with pytest.raises(SandboxError):
            apps.live("etienne", "shop")
        listed = call(server, "etienne", LIST, {})

    (row,) = listed.structured_content["apps"]
    assert row["problem"] == "It can't be started: the sandbox is away"
    # The failure is kept: listing doesn't build the app again.
    assert len(failed) == 1


def test_a_list_says_a_start_failed_without_starting_the_app(
    server: Starlette, monkeypatch
) -> None:
    apps: Apps = server.state.apps
    apps.forget("etienne", "shop")

    with failing_build(apps, "shop", monkeypatch) as failed:
        with pytest.raises(SandboxError):
            apps.live("etienne", "shop")
        listed = call(server, "etienne", LIST, {})

    # The roles are kept, and the failure the server already holds is said with them.
    (row,) = listed.structured_content["apps"]
    assert row["roles"] == ["owner", "staff"]
    assert row["problem"] == "It can't be started: the sandbox is away"
    assert len(failed) == 1
    # Once the failure isn't kept any more, the list says nothing of it, and starts nothing.
    apps.failed_for = 0
    with failing_build(apps, "shop", monkeypatch) as again:
        (row,) = call(server, "etienne", LIST, {}).structured_content["apps"]
    assert "problem" not in row and again == []


def test_what_says_an_app_is_starting() -> None:
    def answer(method: str, path: str):
        scope = {"type": "http", "method": method, "path": path, "headers": [], "query_string": b""}
        return anyio.run(starting, Request(scope), AppStarting("starting"))

    call_ = answer("POST", "/apps/etienne/shop/owner/rpc")
    page = answer("GET", "/apps/etienne/rpc")
    form = answer("POST", "/manage/etienne/shop/share")

    assert call_.headers["content-type"] == "application/json"
    # An app called rpc has a page like any other, which opens itself once the app started.
    assert page.headers["content-type"].startswith("text/html")
    assert b"fetch(location.href" in bytes(page.body)
    assert b'"Cliquet-Probe": "starting"' in bytes(page.body)
    # A form sent while the app starts isn't sent again by itself.
    assert b"fetch(location.href" not in bytes(form.body) and "refresh" not in form.headers
    assert all(
        one.status_code == 503 and one.headers["retry-after"] == "30" for one in (call_, page, form)
    )


@pytest.mark.browser
@needs_browser
def test_the_starting_page_opens_the_app_once_it_started(
    server: Starlette, address: str, monkeypatch
) -> None:
    apps: Apps = server.state.apps
    apps.page_wait = 0
    sam = a_shop_member(server, "staff")
    cookie = server.state.web_auth.session_cookie(sam)
    path = find_browser()
    assert path is not None
    browser = Browser(path)
    try:
        browser.visit(f"{address}/favicon.svg", 390)
        browser.evaluate(f"document.cookie = 'session={cookie}; path=/'")
        apps.forget("etienne", "shop")
        with blocked_build(apps, "shop", monkeypatch) as blocked:
            browser.visit(f"{address}/apps/etienne/shop/staff", 390)
            assert browser.evaluate("document.title") == "The app is starting · cliquet.dev"
            assert blocked.building.wait(timeout=30)
            blocked.release()
            # The page asks again until the app has started, then opens the app's own page.
            wait_for(browser, "document.querySelector('.app-bar') !== null")
        assert browser.evaluate("location.pathname") == "/apps/etienne/shop/staff"
    finally:
        browser.close()


def test_a_forget_doesnt_wait_for_a_start_waiting_for_its_sandbox(
    shop, apps: Apps, monkeypatch
) -> None:
    apps.forget("etienne", "shop")
    sandbox_of = apps.executor.sandbox
    waiting = threading.Event()
    answered = threading.Event()

    def slow(login: str):
        # The owner's Sprite is set up again, which takes a minute.
        waiting.set()
        assert answered.wait(timeout=60)
        return sandbox_of(login)

    monkeypatch.setattr(apps.executor, "sandbox", slow)
    try:
        start = apps.start("etienne", "shop")
        assert waiting.wait(timeout=30)
        forgetting = threading.Thread(target=apps.forget, args=("etienne", "shop"))
        forgetting.start()
        forgetting.join(timeout=10)
        assert not forgetting.is_alive()
    finally:
        answered.set()
    start.result(timeout=60)


def test_a_start_during_a_delete_finds_no_app(shop, apps: Apps, monkeypatch) -> None:
    apps.forget("etienne", "shop")
    # Set once the start is done, or waits for the app's lock.
    moved = threading.Event()
    apps._locks[("etienne", "shop")] = Watched(moved)  # ty: ignore[invalid-assignment]
    client = apps.executor.sandbox("etienne")
    real = type(client).delete_app
    starts: list[Future[Live]] = []

    def delete_app(self, login: str, app: str) -> None:
        # A list or the warm-up starts the app while it is deleted.
        starts.append(apps.start(login, app))
        starts[0].add_done_callback(lambda _: moved.set())
        assert moved.wait(timeout=60)
        real(self, login, app)

    monkeypatch.setattr(type(client), "delete_app", delete_app)
    apps.delete("etienne", "shop")

    assert isinstance(starts[0].exception(timeout=60), UnknownApp)
    assert apps.current("etienne", "shop") is None


@pytest.mark.parametrize("tool", ["check_app", "publish_app"])
def test_a_builder_check_starts_the_live_app_while_it_builds(
    shop, apps: Apps, settings: Settings, monkeypatch, tool: str
) -> None:
    apps.forget("etienne", "shop")
    client = apps.executor.sandbox("etienne")
    real = type(client).put_version
    started = threading.Event()
    began_first: list[bool] = []

    def put_version(self, login: str, app: str, version: str, files: dict[str, bytes]):
        if threading.current_thread().name == "start etienne/shop":
            started.set()
        elif not began_first:
            # The draft's build: the live app's start is already on its way.
            began_first.append(started.wait(timeout=30))
        return real(self, login, app, version, files)

    monkeypatch.setattr(type(client), "put_version", put_version)
    arguments = {"app": "shop"} | ({"remark": "Again"} if tool == "publish_app" else {})
    builder_answer(apps, settings, tool, arguments)

    assert began_first == [True]


def test_a_sandbox_set_up_cut_off_by_a_close_is_not_unexpected(
    apps: Apps, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    other = Apps(apps.settings, apps.database, apps.executor, clock=lambda: NOW)

    def closed_meanwhile(login: str) -> None:
        other.close()
        raise SandboxError("the sandbox service stopped")

    monkeypatch.setattr(apps.executor, "sandbox", closed_meanwhile)
    with pytest.raises(SandboxError):
        other._owner_sandbox("etienne")
    assert unexpected(caplog) == []
