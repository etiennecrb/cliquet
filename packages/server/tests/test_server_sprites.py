import io
import logging
import os
import re
import secrets
import tarfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from cliquet.sandbox import version_id
from cliquet.scaffold import sdk_code_hash
from cliquet_server.apps import Apps, StartFault
from cliquet_server.db import Database, SandboxInfo
from cliquet_server.executor import (
    ExecResult,
    SandboxAway,
    SandboxClient,
    SandboxError,
    SandboxFault,
    sdk_archive,
)
from cliquet_server.limiter import Once
from cliquet_server.settings import Settings, SettingsError
from cliquet_server.sprites import (
    BUILD_POLICY,
    COLD_AFTER,
    RESOURCES_POLICY,
    SETUP_POLICY,
    WAKE_TIMEOUT,
    SpritesApi,
    SpritesExecutor,
    parse_exec,
)

from .server_helpers import PROBE_APP, SDK, example_code, example_data, one_unexpected, sdk_copy

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


def test_parse_exec() -> None:
    assert parse_exec(b"\x02err\n\x01out\n\x03\x03") == ExecResult(3, "out\n", "err\n")
    assert parse_exec(b"\x01a\x01b\x02c\x03\x00") == ExecResult(0, "ab", "c")
    assert parse_exec(b"\x01a") == ExecResult(-1, "a", "")


def test_the_api_sends_what_sprites_expects() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/exec"):
            return httpx.Response(200, content=b"\x01ok\n\x03\x00")
        return httpx.Response(200, json={"path": "/x", "size": 1, "mode": "644"})

    api = SpritesApi("the-token", transport=httpx.MockTransport(handler))
    result = api.exec("s", ["bash", "-lc", "echo hi"], env={"A": "1"})
    assert result == ExecResult(0, "ok\n", "")
    request = seen[-1]
    assert request.method == "POST"
    assert request.url.path == "/v1/sprites/s/exec"
    assert request.url.params.get_list("cmd") == ["bash", "-lc", "echo hi"]
    assert request.url.params.get_list("env") == ["A=1"]
    assert request.headers["authorization"] == "Bearer the-token"
    api.write("s", "/home/sprite/x", b"bytes")
    assert seen[-1].method == "PUT"
    assert seen[-1].url.path == "/v1/sprites/s/fs/write"
    assert seen[-1].url.params["path"] == "/home/sprite/x"
    assert seen[-1].content == b"bytes"
    api.set_policy("s", "network", {"rules": [{"action": "deny", "domain": "*"}]})
    assert seen[-1].method == "POST"
    assert seen[-1].url.path == "/v1/sprites/s/policy/network"
    assert seen[-1].read() == b'{"rules":[{"action":"deny","domain":"*"}]}'
    assert api.policy("s", "resources") == {"path": "/x", "size": 1, "mode": "644"}
    assert seen[-1].method == "GET"
    assert seen[-1].url.path == "/v1/sprites/s/policy/resources"
    with pytest.raises(ValueError, match="privileges"):
        api.set_policy("s", "privileges", {})
    api.close()


def test_a_new_sprite_has_time_to_answer() -> None:
    # A new Sprite answered its first calls after more than httpx's default of 5 s.
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/exec"):
            return httpx.Response(200, content=b"\x03\x00")
        return httpx.Response(200, json={"name": "s", "url": "https://s"})

    api = SpritesApi("the-token", transport=httpx.MockTransport(handler))
    api.create("s")
    api.set_policy("s", "network", {"rules": []})
    api.exec("s", ["true"], timeout=30)
    api.close()

    made, policy, ran = (request.extensions["timeout"] for request in seen)
    assert made["read"] == policy["read"] == 60
    assert made["connect"] == 10
    # A call that says how long it waits keeps it.
    assert ran["read"] == 30


def test_a_missing_sprite_is_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/missing"):
            return httpx.Response(404)
        return httpx.Response(500, content=b"it broke")

    api = SpritesApi("the-token", transport=httpx.MockTransport(handler))
    assert api.get("missing") is None
    with pytest.raises(SandboxError) as error:
        api.get("broken")
    assert "the-token" not in str(error.value)
    api.close()


def test_the_archive_is_the_sdk_alone() -> None:
    first = sdk_archive(SDK)
    assert sdk_archive(SDK) == first
    with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as archive:
        names = set(archive.getnames())
        pyproject = archive.extractfile("pyproject.toml")
        assert pyproject is not None
        text = pyproject.read().decode()
    assert {
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "src/cliquet/__init__.py",
        "src/cliquet/worker.py",
    } <= names
    assert "tool.uv.workspace" not in text
    assert not any(name.startswith(("server/", "tests/")) for name in names)
    assert not any("__pycache__" in name for name in names)


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.setup_code = 0
        self.setup_output = ""
        self.policies: dict[str, Any] = {}
        self.policies_of: dict[tuple[str, str], Any] = {}
        self.broken: str | None = None
        self.altered: str | None = None
        # The state `get` answers, and whether it fails: an unknown state either way.
        self.state: str | None = None
        self.get_fails = False
        # Whether Sprites has no such Sprite, as after it was destroyed by hand.
        self.missing = False
        # A control plane that answers a page, which Sprites' JSON reader refuses.
        self.get_answers = True

    def get(self, name: str, timeout: float = 60.0) -> dict[str, Any] | None:
        self.calls.append(("get", name))
        if self.get_fails:
            raise SandboxError(f"Sprites answered 500 to GET /v1/sprites/{name}")
        if not self.get_answers:
            raise ValueError("Sprites answered a page that isn't JSON")
        if self.missing:
            return None
        sprite = {"name": name, "url": "https://sprite.example"}
        return sprite if self.state is None else {**sprite, "status": self.state}

    def create(self, name: str) -> dict[str, Any]:
        self.calls.append(("create", name))
        return {"url": "https://sprite.example"}

    def destroy(self, name: str) -> None:
        self.calls.append(("destroy", name))

    def exec(self, name: str, command: list[str], **kwargs: Any) -> ExecResult:
        self.calls.append(("exec", command))
        if command[0] == "bash":
            return ExecResult(self.setup_code, self.setup_output, "")
        return ExecResult(0, "", "")

    def write(self, name: str, path: str, content: bytes) -> None:
        self.calls.append(("write", path))

    def set_policy(self, name: str, kind: str, body: dict[str, Any]) -> None:
        self.calls.append(("set_policy", (kind, body)))
        if kind == self.broken:
            raise SandboxError(f"Sprites answered 500 to POST /v1/sprites/{name}/policy/{kind}")
        self.policies[kind] = body
        self.policies_of[(name, kind)] = body

    def policy(self, name: str, kind: str) -> dict[str, Any]:
        self.calls.append(("policy", kind))
        if kind == self.altered:
            return {"rules": []}
        return self.policies_of.get((name, kind), {})

    def close(self) -> None:
        self.calls.append(("close", ""))


def executor_with(
    api: FakeApi, database: Database, sdk_root: Path = SDK, **kwargs: Any
) -> SpritesExecutor:
    made = SpritesExecutor(
        cast(SpritesApi, api),
        database,
        "the-sprites-token",
        sdk_root=sdk_root,
        clock=lambda: NOW,
        **kwargs,
    )
    return made


@pytest.fixture(autouse=True)
def no_health_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    import cliquet_server.sprites as sprites

    monkeypatch.setattr(sprites, "wait_until_healthy", lambda client: None)


class FakeHealth:
    """The sandbox's `/health` answer, faked: no test reaches a Sprite's URL."""

    def __init__(self, real: Callable[..., dict[str, Any]]) -> None:
        self._real = real
        self.answer: Any = {"ok": True, "worker": False}
        self.error: SandboxError | None = None
        self.timeouts: list[float | None] = []

    def __call__(self, client: SandboxClient, *args: Any, **kwargs: Any) -> Any:
        if self.answer is None:
            return self._real(client, *args, **kwargs)
        self.timeouts.append(kwargs.get("timeout"))
        if self.error is not None:
            raise self.error
        return dict(self.answer) if isinstance(self.answer, dict) else self.answer


@pytest.fixture(autouse=True)
def fake_health(monkeypatch: pytest.MonkeyPatch) -> FakeHealth:
    """Fake every sandbox's `/health`: a test that needs the real one clears `answer`."""
    fake = FakeHealth(SandboxClient.health)

    def health(client: SandboxClient, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return fake(client, *args, **kwargs)

    monkeypatch.setattr(SandboxClient, "health", health)
    return fake


def ready_sandbox(owner: str = "etienne") -> SandboxInfo:
    """A sandbox row as this version of the server set it up before, at its own SDK."""
    return SandboxInfo(
        owner,
        f"cliquet-{owner}",
        "https://sprite.example",
        "sandbox-token",
        sdk_code_hash(SDK),
        NOW,
    )


def test_a_new_owner_gets_a_sprite(database: Database) -> None:
    api = FakeApi()
    executor = executor_with(api, database)
    client = executor.sandbox("etienne")
    kinds = [call[0] for call in api.calls]
    assert kinds == [
        "destroy",
        "create",
        "set_policy",
        "set_policy",
        "policy",
        "policy",
        "exec",
        "write",
        "write",
        "write",
        "exec",
        "exec",
        "set_policy",
        "policy",
    ]
    info = database.sandbox("etienne")
    assert info is not None
    assert info.name == "cliquet-etienne"
    assert info.url == "https://sprite.example"
    assert info.ready_at == NOW
    assert info.sdk == sdk_code_hash(SDK)
    assert client._client.headers["authorization"] == "Bearer the-sprites-token"
    assert client._client.headers["x-sandbox-token"] == info.token
    executor.close()


def test_a_sprite_gets_its_policies_first(database: Database) -> None:
    api = FakeApi()
    executor = executor_with(api, database)
    executor.sandbox("etienne")
    first_use = next(i for i, call in enumerate(api.calls) if call[0] in ("write", "exec"))
    before = api.calls[:first_use]
    assert ("set_policy", ("network", SETUP_POLICY)) in before
    assert ("set_policy", ("resources", RESOURCES_POLICY)) in before
    assert ("policy", "network") in before and ("policy", "resources") in before
    last_exec = max(i for i, call in enumerate(api.calls) if call[0] == "exec")
    assert api.calls[last_exec + 1 :] == [
        ("set_policy", ("network", BUILD_POLICY)),
        ("policy", "network"),
    ]
    assert api.policies == {"network": BUILD_POLICY, "resources": RESOURCES_POLICY}
    assert BUILD_POLICY == {"rules": [{"action": "deny", "domain": "*"}]}
    assert SETUP_POLICY["rules"][-1] == {"action": "deny", "domain": "*"}
    executor.close()


def test_a_ready_sandbox_is_used_as_it_is(database: Database) -> None:
    database.save_sandbox(ready_sandbox())
    api = FakeApi()
    executor = executor_with(api, database)
    client = executor.sandbox("etienne")
    # The wake probe reads the Sprite's state, and nothing is made or destroyed for it.
    assert api.calls == [("get", "cliquet-etienne")]
    assert client._client.headers["authorization"] == "Bearer the-sprites-token"
    assert client._client.headers["x-sandbox-token"] == "sandbox-token"
    executor.close()


@pytest.mark.parametrize("answer", [["not", "an", "answer"], {"ok": True}])
def test_a_health_answer_that_is_not_one_does_not_fail_the_call(
    database: Database,
    caplog: pytest.LogCaptureFixture,
    fake_health: FakeHealth,
    answer: Any,
) -> None:
    database.save_sandbox(ready_sandbox())
    api = FakeApi()
    fake_health.answer = answer
    executor = executor_with(api, database)
    with caplog.at_level(logging.INFO, logger="cliquet_server.sprites"):
        client = executor.sandbox("etienne")
    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 1
    assert lines[0].endswith("didn't say whether its worker ran")
    assert client._client.headers["x-sandbox-token"] == "sandbox-token"
    executor.close()


@pytest.mark.parametrize(
    ("worker", "said"), [(False, "worker started"), (True, "worker was running")]
)
def test_a_sprite_that_may_be_cold_says_its_state_and_its_wake(
    database: Database,
    caplog: pytest.LogCaptureFixture,
    fake_health: FakeHealth,
    worker: bool,
    said: str,
) -> None:
    database.save_sandbox(ready_sandbox())
    api = FakeApi()
    api.state = "cold"
    fake_health.answer = {"ok": True, "worker": worker}
    executor = executor_with(api, database)
    with caplog.at_level(logging.INFO, logger="cliquet_server.sprites"):
        executor.sandbox("etienne")
    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 1
    assert re.fullmatch(rf"sandbox cliquet-etienne: cold, awake in \d+\.\d s, {said}", lines[0])
    assert ("get", "cliquet-etienne") in api.calls
    # The probe waits less than a call does: the caller waits behind it.
    assert fake_health.timeouts == [WAKE_TIMEOUT]
    executor.close()


def test_a_probe_follows_the_last_use_of_the_sandbox(
    database: Database, caplog: pytest.LogCaptureFixture, fake_health: FakeHealth
) -> None:
    database.save_sandbox(ready_sandbox())
    api = FakeApi()
    api.state = "cold"
    now = time.monotonic()
    executor = executor_with(api, database, wakes=Once(COLD_AFTER, clock=lambda: now))
    with caplog.at_level(logging.INFO, logger="cliquet_server.sprites"):
        executor.sandbox("etienne")
        # A call just after asks nothing: the owner is known warm.
        executor.sandbox("etienne")
    assert len(api.calls) == 1
    assert fake_health.timeouts == [WAKE_TIMEOUT]
    # Past the window, the next call looks again.
    now += COLD_AFTER + 1
    with caplog.at_level(logging.INFO, logger="cliquet_server.sprites"):
        executor.sandbox("etienne")
    assert len(api.calls) == 2
    assert fake_health.timeouts == [WAKE_TIMEOUT, WAKE_TIMEOUT]
    assert len(caplog.records) == 2
    executor.close()


def test_a_new_sprite_logs_no_wake_line(
    database: Database, caplog: pytest.LogCaptureFixture, fake_health: FakeHealth
) -> None:
    api = FakeApi()
    executor = executor_with(api, database)
    with caplog.at_level(logging.INFO, logger="cliquet_server.sprites"):
        executor.sandbox("etienne")
        executor.sandbox("etienne")
    # A Sprite just set up was just used, so nothing reads its state or asks it anything.
    assert not any(call[0] == "get" for call in api.calls)
    assert fake_health.timeouts == []
    assert not any("awake" in record.getMessage() for record in caplog.records)
    executor.close()


@pytest.mark.parametrize("state_read", ["failing", "unreadable"])
def test_an_unknown_sprite_state_is_said_and_the_call_goes_on(
    database: Database,
    caplog: pytest.LogCaptureFixture,
    fake_health: FakeHealth,
    state_read: str,
) -> None:
    database.save_sandbox(ready_sandbox())
    api = FakeApi()
    api.get_fails = True
    api.get_answers = state_read != "unreadable"
    executor = executor_with(api, database)
    with caplog.at_level(logging.INFO, logger="cliquet_server.sprites"):
        client = executor.sandbox("etienne")
    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 1
    assert lines[0].startswith("sandbox cliquet-etienne: state unknown, awake in ")
    assert client._client.headers["x-sandbox-token"] == "sandbox-token"
    executor.close()


def test_a_sprite_that_sprites_no_longer_has_is_set_up_again(
    database: Database, caplog: pytest.LogCaptureFixture, fake_health: FakeHealth
) -> None:
    # Destroyed by hand, or the token now names another organisation: Sprites answers 404.
    database.save_sandbox(ready_sandbox())
    api = FakeApi()
    api.missing = True
    executor = executor_with(api, database)
    with caplog.at_level(logging.INFO, logger="cliquet_server.sprites"):
        client = executor.sandbox("etienne")
    # The probe finds it gone, then the owner's lock asks again, so that it is set up once.
    assert api.calls[:4] == [
        ("get", "cliquet-etienne"),
        ("get", "cliquet-etienne"),
        ("destroy", "cliquet-etienne"),
        ("create", "cliquet-etienne"),
    ]
    info = database.sandbox("etienne")
    assert info is not None
    assert info.ready_at == NOW
    assert info.token != "sandbox-token"
    assert client._client.headers["x-sandbox-token"] == info.token
    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert warnings == ["sandbox cliquet-etienne: Sprites no longer has it, setting it up again"]
    # A missing Sprite has no wake to probe.
    assert fake_health.timeouts == []
    # The new Sprite is used as it is from then on.
    api.calls.clear()
    assert executor.sandbox("etienne") is client
    assert api.calls == []
    executor.close()


def test_a_sprite_destroyed_while_in_use_is_set_up_again_at_the_next_probe(
    database: Database,
) -> None:
    database.save_sandbox(ready_sandbox())
    api = FakeApi()
    api.state = "warm"
    now = time.monotonic()
    executor = executor_with(api, database, wakes=Once(COLD_AFTER, clock=lambda: now))
    first = executor.sandbox("etienne")
    api.missing = True
    # Within the probe's window, nothing is read: the client stays.
    assert executor.sandbox("etienne") is first
    assert ("create", "cliquet-etienne") not in api.calls
    now += COLD_AFTER + 1
    # The same client, which the owner's started apps hold, now speaks to the new Sprite.
    assert executor.sandbox("etienne") is first
    assert ("create", "cliquet-etienne") in api.calls
    info = database.sandbox("etienne")
    assert info is not None
    assert first._client.headers["x-sandbox-token"] == info.token != "sandbox-token"
    executor.close()


def checked(owner: str = "etienne") -> None:
    """Wait for the background look for the owner's Sprite to end, if one runs."""
    for thread in threading.enumerate():
        if thread.name == f"sprite check {owner}":
            thread.join(timeout=30)
            assert not thread.is_alive()


def test_a_call_that_can_t_reach_the_sprite_has_it_looked_for(database: Database) -> None:
    database.save_sandbox(ready_sandbox())
    api = FakeApi()
    api.state = "warm"
    executor = executor_with(api, database)
    client = executor.sandbox("etienne")
    api.missing = True
    # What a started app's call does when the Sprite doesn't answer as a sandbox.
    assert client._lost is not None
    client._lost()
    checked()
    assert ("create", "cliquet-etienne") in api.calls
    assert executor.sandbox("etienne") is client
    info = database.sandbox("etienne")
    assert info is not None
    assert client._client.headers["x-sandbox-token"] == info.token != "sandbox-token"
    # A call failing again soon after looks for nothing more.
    made = api.calls.count(("create", "cliquet-etienne"))
    client._lost()
    checked()
    assert api.calls.count(("create", "cliquet-etienne")) == made
    executor.close()


def test_a_sprite_that_is_there_is_kept_when_a_call_fails(database: Database) -> None:
    database.save_sandbox(ready_sandbox())
    api = FakeApi()
    api.state = "warm"
    executor = executor_with(api, database)
    client = executor.sandbox("etienne")
    assert client._lost is not None
    client._lost()
    checked()
    assert ("create", "cliquet-etienne") not in api.calls
    assert client._client.headers["x-sandbox-token"] == "sandbox-token"
    executor.close()


def test_a_deleted_account_s_sprite_is_not_looked_for(database: Database) -> None:
    database.save_sandbox(ready_sandbox())
    api = FakeApi()
    api.state = "warm"
    executor = executor_with(api, database)
    client = executor.sandbox("etienne")
    executor.destroy("etienne")
    api.missing = True
    assert client._lost is not None
    client._lost()
    checked()
    assert ("create", "cliquet-etienne") not in api.calls
    executor.close()


def test_a_probe_that_gets_no_answer_does_not_fail_the_call(
    database: Database, caplog: pytest.LogCaptureFixture, fake_health: FakeHealth
) -> None:
    database.save_sandbox(ready_sandbox())
    api = FakeApi()
    fake_health.error = SandboxAway("the sandbox took more than 10 seconds")
    executor = executor_with(api, database)
    with caplog.at_level(logging.INFO, logger="cliquet_server.sprites"):
        client = executor.sandbox("etienne")
    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 1
    assert lines[0].startswith("sandbox cliquet-etienne: state unknown, no answer in ")
    assert lines[0].endswith(
        "the sandbox took more than 10 seconds. The caller's own call wakes it."
    )
    assert client._client.headers["x-sandbox-token"] == "sandbox-token"
    executor.close()


def test_a_new_sdk_means_a_new_sprite(database: Database) -> None:
    old = SandboxInfo("etienne", "cliquet-etienne", "https://sprite.example", "keep-me", "old", NOW)
    database.save_sandbox(old)
    api = FakeApi()
    executor = executor_with(api, database)
    executor.sandbox("etienne")
    kinds = [call[0] for call in api.calls]
    assert kinds[:2] == ["destroy", "create"]
    assert ("set_policy", ("network", SETUP_POLICY)) in api.calls
    assert api.policies["network"] == BUILD_POLICY
    info = database.sandbox("etienne")
    assert info is not None and info.token == "keep-me" and info.ready_at == NOW
    executor.close()


@pytest.mark.parametrize(
    ("changed", "kept"),
    [
        ("src/cliquet/renderer/cliquet.css", True),
        ("src/cliquet/renderer/screen.js", True),
        ("src/cliquet/worker.py", False),
        ("pyproject.toml", False),
    ],
)
def test_only_new_sdk_code_means_a_new_sprite(
    database: Database, tmp_path: Path, changed: str, kept: bool
) -> None:
    # Set up for this SDK, which a deploy then changes.
    database.save_sandbox(ready_sandbox())
    api = FakeApi()
    executor = executor_with(api, database, sdk_root=sdk_copy(tmp_path, changed))
    executor.sandbox("etienne")
    assert (("create", "cliquet-etienne") not in api.calls) == kept
    executor.close()


def test_a_ready_sprite_named_with_another_prefix_is_set_up_again(database: Database) -> None:
    api = FakeApi()
    executor = executor_with(api, database)
    wanted = sdk_code_hash(SDK)
    ready = SandboxInfo("etienne", "other-etienne", "https://sprite.example", "t", wanted, NOW)
    database.save_sandbox(ready)
    executor.sandbox("etienne")
    assert api.calls[:2] == [("destroy", "other-etienne"), ("destroy", "cliquet-etienne")]
    executor.close()


def test_an_old_sprite_named_with_another_prefix_is_removed(database: Database) -> None:
    old = SandboxInfo("etienne", "other-etienne", "https://sprite.example", "keep-me", "old", NOW)
    database.save_sandbox(old)
    api = FakeApi()
    executor = executor_with(api, database)
    executor.sandbox("etienne")
    assert api.calls[:3] == [
        ("destroy", "other-etienne"),
        ("destroy", "cliquet-etienne"),
        ("create", "cliquet-etienne"),
    ]
    info = database.sandbox("etienne")
    assert info is not None and info.name == "cliquet-etienne"
    executor.close()


def test_the_archive_is_built_once_for_every_owner(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cliquet_server.executor as executor_module

    built: list[Path] = []

    def counted(root: Path) -> bytes:
        built.append(root)
        return sdk_archive(root)

    monkeypatch.setattr(executor_module, "sdk_archive", counted)
    ready = ready_sandbox("ready")
    database.save_sandbox(ready)
    api = FakeApi()
    executor = executor_with(api, database)
    owners = ["etienne", "camille", "ready"]
    with ThreadPoolExecutor(len(owners)) as pool:
        list(pool.map(executor.sandbox, owners))
    assert built == [SDK]
    assert ("create", "cliquet-etienne") in api.calls
    assert ("create", "cliquet-camille") in api.calls
    assert ("create", "cliquet-ready") not in api.calls
    executor.close()


@pytest.mark.parametrize("problem", ["broken", "altered"])
@pytest.mark.parametrize("kind", ["network", "resources"])
def test_a_policy_that_fails_leaves_no_sandbox(database: Database, problem: str, kind: str) -> None:
    api = FakeApi()
    setattr(api, problem, kind)
    executor = executor_with(api, database)
    with pytest.raises(SandboxError, match=kind):
        executor.sandbox("etienne")
    info = database.sandbox("etienne")
    assert info is None or info.ready_at is None
    assert not any(call[0] in ("write", "exec") for call in api.calls)
    executor.close()


def test_a_build_policy_that_fails_leaves_no_sandbox(database: Database) -> None:
    api = FakeApi()
    real = api.set_policy

    def set_policy(name: str, kind: str, body: dict[str, Any]) -> None:
        if body == BUILD_POLICY:
            raise SandboxError("Sprites answered 500 to POST /v1/sprites/x/policy/network")
        real(name, kind, body)

    api.set_policy = set_policy  # type: ignore[method-assign]
    executor = executor_with(api, database)
    with pytest.raises(SandboxError, match="network"):
        executor.sandbox("etienne")
    info = database.sandbox("etienne")
    assert info is None or info.ready_at is None
    executor.close()


def test_destroying_an_owner_s_sandbox_removes_its_sprites_and_sets_up_none(
    database: Database,
) -> None:
    # A Sprite named with another prefix goes too, as at a setup.
    old = ready_sandbox()
    database.save_sandbox(SandboxInfo("etienne", "other-etienne", old.url, "t", old.sdk, NOW))
    api = FakeApi()
    executor = executor_with(api, database)
    executor.destroy("etienne")
    assert api.calls == [("destroy", "other-etienne"), ("destroy", "cliquet-etienne")]
    # The client of one set up is closed and forgotten: the next call sets one up again.
    client = executor.sandbox("etienne")
    api.calls.clear()
    executor.destroy("etienne")
    assert api.calls == [("destroy", "cliquet-etienne")]
    assert client._client.is_closed
    assert executor.sandbox("etienne") is not client
    # With no record and no Sprite, it destroys by name, and makes nothing.
    api.calls.clear()
    executor.destroy("sam")
    assert api.calls == [("destroy", "cliquet-sam")]
    with pytest.raises(ValueError, match="not a login"):
        executor.destroy("../etienne")
    executor.close()


def test_a_sprite_set_up_for_a_deleted_account_is_destroyed(database: Database) -> None:
    api = FakeApi()
    executor = executor_with(api, database)
    real = database.save_sandbox

    def deleted_meanwhile(info: SandboxInfo) -> None:
        database.close_account("etienne", NOW)
        real(info)

    database.create_app("etienne", "shop", NOW)
    database.save_sandbox = deleted_meanwhile  # type: ignore[method-assign]
    with pytest.raises(SandboxFault, match="account was deleted"):
        executor.sandbox("etienne")
    assert api.calls[-1] == ("destroy", "cliquet-etienne")
    assert database.sandbox("etienne") is None
    executor.close()


def test_a_failed_setup_is_said(database: Database) -> None:
    api = FakeApi()
    api.setup_code = 1
    api.setup_output = "it went boom"
    executor = executor_with(api, database)
    with pytest.raises(SandboxError, match="boom"):
        executor.sandbox("etienne")
    assert database.sandbox("etienne") is None
    executor.close()


def test_a_failed_setup_is_logged_once_as_unexpected(
    settings: Settings, database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    api = FakeApi()
    api.setup_code = 1
    api.setup_output = "it went boom"
    apps = Apps(settings, database, executor_with(api, database), clock=lambda: NOW)
    with pytest.raises(SandboxError, match="boom"), apps.sandbox("etienne"):
        pass
    record = one_unexpected(caplog)
    assert record.name == "cliquet_server.apps"
    assert record.getMessage().startswith(
        "unexpected: etienne's sandbox didn't start: setting up the sandbox failed: it went boom"
    )
    apps.close()


def test_a_sprites_call_that_raises_is_logged_once_as_unexpected(
    settings: Settings,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = FakeApi()
    # Sprites answers a Sprite without its URL: not a `SandboxError`, but Cliquet's failure all
    # the same, told in Cliquet's own words.
    monkeypatch.setattr(api, "create", lambda name: {})
    apps = Apps(settings, database, executor_with(api, database), clock=lambda: NOW)
    with (
        pytest.raises(StartFault, match=r"the server failed to start it \(KeyError\)"),
        apps.sandbox("etienne"),
    ):
        pass
    record = one_unexpected(caplog, KeyError)
    assert record.getMessage() == "unexpected: etienne's sandbox didn't start: 'url'"
    apps.close()


def test_a_sandbox_that_doesn_t_start_at_boot_is_logged_once_as_unexpected(
    settings: Settings, database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    api = FakeApi()
    api.setup_code = 1
    api.setup_output = "it went boom"
    # An owner whose sandbox an older SDK set up: the warm-up sets it up again.
    database.save_sandbox(SandboxInfo("sam", "cliquet-sam", "http://sam", "t", "a" * 64, NOW))
    database.note_apps_used("sam", NOW)
    apps = Apps(settings, database, executor_with(api, database), clock=lambda: NOW)
    for owner in apps.warm_up():
        owner.result(timeout=60)
    record = one_unexpected(caplog)
    assert record.getMessage().startswith("unexpected: sam's sandbox didn't start: ")
    assert not [one for one in caplog.records if one.levelno == logging.WARNING]
    apps.close()


def test_sprites_needs_its_token() -> None:
    with pytest.raises(SettingsError, match="CLIQUET_SPRITES_TOKEN"):
        Settings.from_env({"CLIQUET_EXECUTOR": "sprites"})


real_sprite = pytest.mark.skipif(
    not (os.environ.get("CLIQUET_TEST_SPRITES") and os.environ.get("CLIQUET_SPRITES_TOKEN")),
    reason="needs a real Sprite: set CLIQUET_TEST_SPRITES=1 and CLIQUET_SPRITES_TOKEN",
)
"""Each test with it creates a `cliquet-test-…` Sprite, and destroys it even when it fails."""


@real_sprite
def test_a_real_sprite_runs_an_app(database: Database, fake_health: FakeHealth) -> None:
    import base64
    import sys

    # This test needs the sandbox's own answer, not the fake one.
    fake_health.answer = None

    token = os.environ["CLIQUET_SPRITES_TOKEN"]
    prefix = f"cliquet-test-{secrets.token_hex(4)}-"
    api = SpritesApi(token)
    executor = SpritesExecutor(api, database, token, prefix=prefix, sdk_root=SDK)
    name = f"{prefix}etienne"
    started = time.monotonic()
    steps = logging.getLogger("cliquet_server.sprites")
    shown = logging.StreamHandler(sys.stdout)
    steps.addHandler(shown)
    steps.setLevel(logging.INFO)
    try:
        client = executor.sandbox("etienne")
        print(f"setup took {time.monotonic() - started:.1f}s")
        code = example_code()
        version = version_id(code)
        built = client.put_version("etienne", "shop", version, code)
        assert built.manifest["name"] == "shop"
        files = {key: base64.b64encode(content).decode() for key, content in example_data().items()}
        request = {
            "op": "query",
            "caller": {"id": "t", "role": "owner"},
            "now": NOW.isoformat(),
            "name": "stock",
            "arguments": {},
            "files": files,
        }
        assert len(client.run("etienne", "shop", version, request)["result"]) == 5

        # The policies read back as set, and app code can't reach the network.
        assert api.policy(name, "network") == BUILD_POLICY
        assert api.policy(name, "resources") == RESOURCES_POLICY
        shots = client.pictures("etienne", "shop", version, "staff", example_data(), ["till"])
        assert [shot.view for shot in shots] == ["till", "till"]
        probe = {"app.py": PROBE_APP.encode(), "instructions.md": b"Tries the network.\n"}
        probe_version = version_id(probe)
        client.put_version("etienne", "probe", probe_version, probe)
        reached = client.run(
            "etienne",
            "probe",
            probe_version,
            {**request, "caller": {"id": "t", "role": None}, "name": "reach", "files": {}},
        )
        print(f"example.com from app code: {reached}")
        assert reached["result"]["dns"].startswith("gaierror")
        assert not reached["result"]["https"].startswith("answered")
        assert not reached["result"]["pypi"].startswith("answered")

        # Printed for whoever reads the run: how long does a run take on a Sprite that went cold?
        cold = False
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            sprite = api.get(name)
            if sprite is not None and sprite["status"] == "cold":
                cold = True
                break
            time.sleep(5)
        woken = time.monotonic()
        assert len(client.run("etienne", "shop", version, request)["result"]) == 5
        print(f"run after {'cold' if cold else 'waiting'}: {time.monotonic() - woken:.1f}s")

        # Each executor owns its API client: closing it must not close the one used here.
        again = SpritesExecutor(SpritesApi(token), database, token, prefix=prefix, sdk_root=SDK)
        assert again.sandbox("etienne").health()["ok"] is True
        again.close()
    finally:
        steps.removeHandler(shown)
        try:
            api.destroy(name)
        finally:
            executor.close()


# A service that writes down the names of the headers each request brings, never their values.
HEADER_LOGGER = """\
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Logger(BaseHTTPRequestHandler):
    def do_GET(self):
        with open("/home/sprite/headers.jsonl", "a") as log:
            log.write(json.dumps({"path": self.path, "names": sorted(self.headers.keys())}) + "\\n")
        self.send_response(200)
        self.end_headers()


HTTPServer(("0.0.0.0", 8080), Logger).serve_forever()
"""


@real_sprite
def test_a_real_sprite_url_keeps_the_token_from_the_container() -> None:
    import json

    token = os.environ["CLIQUET_SPRITES_TOKEN"]
    name = f"cliquet-test-{secrets.token_hex(4)}-headers"
    api = SpritesApi(token)
    try:
        sprite = api.create(name)
        assert sprite["url_settings"]["auth"] == "sprite"
        api.write(name, "/home/sprite/logger.py", HEADER_LOGGER.encode())
        created = api.exec(
            name,
            [
                "sprite-env",
                "services",
                "create",
                "logger",
                "--cmd",
                "python3",
                "--args",
                "/home/sprite/logger.py",
                "--http-port",
                "8080",
                "--no-stream",
            ],
        )
        assert created.code == 0, created.stdout + created.stderr
        listening = api.exec(
            name,
            [
                "bash",
                "-c",
                "for i in $(seq 60); do curl -sf -m 2 localhost:8080/up && exit 0; "
                "sleep 1; done; exit 1",
            ],
        )
        assert listening.code == 0, "the logger didn't start listening"

        # The server sends the Sprites token to a Sprite's URL, as SpritesExecutor does.
        answer = httpx.get(
            f"{sprite['url']}/from-the-server",
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        assert answer.status_code == 200, answer.text[:300]
        logged = api.exec(name, ["cat", "/home/sprite/headers.jsonl"])
        seen = [json.loads(line) for line in logged.stdout.splitlines()]
        arrived = next(entry for entry in seen if entry["path"] == "/from-the-server")
        print(f"headers in the container: {arrived['names']}")
        assert "authorization" not in {header.lower() for header in arrived["names"]}
    finally:
        try:
            api.destroy(name)
        finally:
            api.close()
