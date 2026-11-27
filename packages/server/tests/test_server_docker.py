import io
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from cliquet.sandbox import version_id
from cliquet.scaffold import sdk_code_hash
from cliquet_server.apps import Apps
from cliquet_server.db import Database, SandboxInfo
from cliquet_server.docker import (
    SETUP,
    SETUP_DONE,
    SUBPATH_API,
    DockerApi,
    DockerExecutor,
    parse_stdcopy,
    stdcopy_text,
    wait_for_socket,
)
from cliquet_server.executor import (
    ExecResult,
    SandboxAway,
    SandboxClient,
    SandboxError,
    SandboxFault,
    encoded,
    sdk_archive,
    tar_of,
)
from cliquet_server.settings import Settings

from .server_helpers import PROBE_APP, SDK, example_code, example_data, one_unexpected, sdk_copy

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
WANTED = sdk_code_hash(SDK)


def frame(kind: int, body: bytes) -> bytes:
    """One of Docker's stream frames."""
    return bytes([kind, 0, 0, 0]) + len(body).to_bytes(4, "big") + body


def test_parse_stdcopy_reads_the_frames() -> None:
    assert parse_stdcopy(frame(1, b"out\n") + frame(2, b"err\n")) == (b"out\n", b"err\n")
    assert parse_stdcopy(frame(1, b"a") + frame(1, b"b") + frame(2, b"c")) == (b"ab", b"c")
    # A stream that isn't framed, as a terminal's isn't, is all standard output.
    assert parse_stdcopy(b"plain text") == (b"plain text", b"")
    assert parse_stdcopy(b"") == (b"", b"")


def test_stdcopy_text_keeps_the_order() -> None:
    assert stdcopy_text(frame(2, b"one\n") + frame(1, b"two\n")) == "one\ntwo\n"
    assert stdcopy_text(b"plain text") == "plain text"


def test_the_archive_is_a_tar_of_the_files() -> None:
    content = tar_of({"sdk.tar.gz": b"the sdk"})
    with tarfile.open(fileobj=io.BytesIO(content), mode="r") as archive:
        assert archive.getnames() == ["sdk.tar.gz"]
        one = archive.extractfile("sdk.tar.gz")
        assert one is not None and one.read() == b"the sdk"


def test_the_api_sends_what_docker_expects() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/containers/create":
            return httpx.Response(201, json={"Id": "abc", "Warnings": []})
        if request.url.path == "/containers/abc/exec":
            return httpx.Response(201, json={"Id": "e1"})
        if request.url.path == "/exec/e1/start":
            return httpx.Response(200, content=frame(1, b"out\n") + frame(2, b"err\n"))
        if request.url.path == "/exec/e1/json":
            return httpx.Response(200, json={"ExitCode": 3, "Running": False})
        if request.url.path.endswith("/logs"):
            return httpx.Response(200, content=frame(1, b"line one\n") + frame(2, b"line two\n"))
        return httpx.Response(204)

    api = DockerApi("/var/run/docker.sock", transport=httpx.MockTransport(handler))
    made = api.create(
        "cliquet-etienne",
        image="cliquet-sandbox:latest",
        env={"CLIQUET_SANDBOX_TOKEN": "the-token"},
        labels={"cliquet.sdk": "the-sdk"},
        host_config={"NetworkMode": "none"},
    )
    assert made == "abc"
    assert seen[-1].method == "POST"
    assert seen[-1].url.path == "/containers/create"
    assert seen[-1].url.params["name"] == "cliquet-etienne"
    body = seen[-1].read()
    assert b'"Image":"cliquet-sandbox:latest"' in body
    assert b'"Env":["CLIQUET_SANDBOX_TOKEN=the-token"]' in body
    assert b'"Labels":{"cliquet.sdk":"the-sdk"}' in body
    assert b'"HostConfig":{"NetworkMode":"none"}' in body

    api.start("abc")
    assert seen[-1].method == "POST" and seen[-1].url.path == "/containers/abc/start"
    api.remove("abc")
    assert seen[-1].method == "DELETE"
    assert seen[-1].url.params["force"] == "1"
    assert seen[-1].url.params["v"] == "0"

    assert api.exec("abc", ["sh", "-c", "true"]) == ExecResult(3, "out\n", "err\n")
    started = next(request for request in seen if request.url.path == "/exec/e1/start")
    assert started.read() == b'{"Detach":false,"Tty":false}'
    assert api.logs("abc", tail=5) == "line one\nline two\n"
    assert seen[-1].url.params["tail"] == "5"
    api.close()


def test_a_container_that_mounts_a_subfolder_needs_engine_26() -> None:
    seen: list[httpx.Request] = []
    older = [False]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/volumes/create":
            return httpx.Response(201, json={"Name": "cliquet-etienne-root"})
        if older[0]:
            # What Docker Engine 25 answers.
            message = "client version 1.45 is too new. Maximum supported API version is 1.44"
            return httpx.Response(400, json={"message": message})
        return httpx.Response(201, json={"Id": "abc", "Warnings": []})

    api = DockerApi(transport=httpx.MockTransport(handler))
    labels = {"cliquet.sandbox": "1", "cliquet.owner": "etienne"}
    api.create_volume("cliquet-etienne-root", labels=labels)
    assert seen[-1].url.path == "/volumes/create"
    assert json.loads(seen[-1].read()) == {"Name": "cliquet-etienne-root", "Labels": labels}

    made = {"image": "cliquet-sandbox:latest", "env": {}, "labels": {}, "host_config": {}}
    assert api.create("cliquet-etienne", **made, subpath=True) == "abc"
    # A daemon older than Engine 26 refuses this version rather than mount the whole volume.
    assert seen[-1].url.path == f"/v{SUBPATH_API}/containers/create"
    older[0] = True
    with pytest.raises(SandboxFault, match="needs Docker Engine 26 or later"):
        api.create("cliquet-etienne", **made, subpath=True)
    api.close()


def test_a_container_that_is_not_there_is_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/missing/json"):
            return httpx.Response(404, json={"message": "No such container: missing"})
        return httpx.Response(500, json={"message": "it broke"})

    api = DockerApi(transport=httpx.MockTransport(handler))
    assert api.inspect("missing") is None
    with pytest.raises(SandboxError, match="it broke"):
        api.inspect("broken")
    api.close()


def test_a_daemon_that_cannot_be_reached_says_so() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no socket there")

    api = DockerApi(transport=httpx.MockTransport(handler))
    with pytest.raises(SandboxAway, match="can't reach Docker's daemon"):
        api.inspect("any")
    api.close()


def test_a_socket_the_server_may_not_use_names_its_group() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 13] Permission denied") from PermissionError(13, "no")

    api = DockerApi(transport=httpx.MockTransport(handler))
    with pytest.raises(SandboxAway, match="DOCKER_GID"):
        api.inspect("any")
    api.close()


def test_the_socket_is_waited_for() -> None:
    # A unix socket's path is short: a test's own folder is too long for one.
    folder = Path(tempfile.mkdtemp(prefix="cliquet-socket-"))
    try:
        path = folder / "sandbox.sock"
        with socket.socket(socket.AF_UNIX) as bound:
            bound.bind(str(path))
            wait_for_socket(path, seconds=1)
        with pytest.raises(SandboxFault, match="socket"):
            wait_for_socket(path, seconds=0)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


class FakeEngine:
    """Docker's API, faked: it records what the executor asks for and answers as Docker does."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.created: dict[str, Any] = {}
        # What `inspect` answers, or None for a container that isn't there.
        self.state: dict[str, Any] | None = None
        self.setup_code = 0
        self.setup_output = ""
        self.log = ""
        # What `containers` answers.
        self.found: list[str] = []
        # The volumes made, with their labels.
        self.volumes: dict[str, dict[str, str]] = {}

    def inspect(self, name: str) -> dict[str, Any] | None:
        self.calls.append(("inspect", name))
        return self.state

    def create(
        self,
        name: str,
        *,
        image: str,
        env: dict[str, str],
        labels: dict[str, str],
        host_config: dict[str, Any],
        subpath: bool = False,
    ) -> str:
        self.calls.append(("create", name))
        self.created = {
            "image": image,
            "env": dict(env),
            "labels": dict(labels),
            "host_config": host_config,
            "subpath": subpath,
        }
        return "the-container-id"

    def create_volume(self, name: str, *, labels: dict[str, str]) -> None:
        self.volumes[name] = dict(labels)

    def start(self, container: str) -> None:
        self.calls.append(("start", container))

    def stop(self, container: str, *, wait: int = 10) -> None:
        self.calls.append(("stop", container))

    def remove(self, container: str, *, force: bool = True) -> None:
        self.calls.append(("remove", container))

    def remove_volume(self, name: str) -> None:
        self.calls.append(("remove_volume", name))

    def containers(self, *, label: str) -> list[str]:
        self.calls.append(("containers", label))
        return list(self.found)

    def exec(self, container: str, command: list[str], *, timeout: float = 300) -> ExecResult:
        self.calls.append(("exec", command))
        return ExecResult(self.setup_code, self.setup_output, "")

    def logs(self, container: str, tail: int = 20) -> str:
        self.calls.append(("logs", container))
        return self.log

    def close(self) -> None:
        self.calls.append(("close", ""))


@dataclass
class FakeWaits:
    """What the executor waits for after starting a container, faked: a real test sets `real`."""

    socket: Callable[..., None]
    health: Callable[..., None]
    real: bool = False
    failure: SandboxError | None = None
    sockets: list[Path] = field(default_factory=list)
    clients: list[SandboxClient] = field(default_factory=list)

    def wait_for_socket(self, path: Path, seconds: float = 60.0) -> None:
        self.sockets.append(path)
        if self.real:
            self.socket(path, seconds)

    def wait_until_healthy(self, client: SandboxClient, seconds: float = 120.0) -> None:
        self.clients.append(client)
        if self.failure is not None:
            raise self.failure
        if self.real:
            self.health(client, seconds)


@pytest.fixture(autouse=True)
def fake_waits(monkeypatch: pytest.MonkeyPatch) -> FakeWaits:
    """Fake the waits for a container's socket and its first answer: none of them in a test."""
    import cliquet_server.docker as docker

    fake = FakeWaits(docker.wait_for_socket, docker.wait_until_healthy)
    monkeypatch.setattr(docker, "wait_for_socket", fake.wait_for_socket)
    monkeypatch.setattr(docker, "wait_until_healthy", fake.wait_until_healthy)
    return fake


@pytest.fixture
def sockets() -> Iterator[Path]:
    """The folder one owner's socket is in: a short one, since a unix socket's path must be."""
    folder = Path(tempfile.mkdtemp(prefix="cliquet-sockets-"))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def executor_with(
    engine: FakeEngine, sockets: Path, database: Database, sdk_root: Path = SDK, **kwargs: Any
) -> DockerExecutor:
    """An executor over the fake engine, with the clock fixed at `NOW`."""
    return DockerExecutor(
        cast(DockerApi, engine),
        database,
        sockets,
        sockets_host=sockets,
        sdk_root=sdk_root,
        clock=lambda: NOW,
        **kwargs,
    )


def container_config(sockets: Path, owner: str = "etienne") -> dict[str, Any]:
    """The limits this version gives a container, as Docker's inspect answers them."""
    return {
        "NetworkMode": "none",
        "ReadonlyRootfs": True,
        "Tmpfs": {"/tmp": "rw,size=512m"},
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "Memory": 2048 * 2**20,
        "Binds": [
            f"cliquet-{owner}-root:/sandbox",
            f"cliquet-{owner}-venv:/app/.venv",
            f"{sockets / owner}:/sock",
        ],
    }


def running_container(
    sdk: str = WANTED,
    running: bool = True,
    sockets: Path | None = None,
    created: datetime = NOW,
) -> dict[str, Any]:
    """A container as Docker's inspect answers it."""
    return {
        "Created": created.isoformat(),
        "State": {"Running": running},
        "Config": {
            "User": "sandbox",
            "Labels": {
                "cliquet.sdk": sdk,
                "cliquet.sockets": str((sockets or Path("/sockets")) / "etienne"),
            },
        },
        "HostConfig": container_config(sockets or Path("/sockets")),
    }


def ready_sandbox(owner: str = "etienne", sockets: Path | None = None) -> SandboxInfo:
    """A sandbox row as this version of the server made it, with the container it names."""
    socket = (sockets or Path("/sockets")) / owner / "sandbox.sock"
    return SandboxInfo(owner, f"cliquet-{owner}", str(socket), "sandbox-token", WANTED, NOW)


def test_a_new_owner_gets_a_container(
    sockets: Path, database: Database, fake_waits: FakeWaits
) -> None:
    engine = FakeEngine()
    executor = executor_with(engine, sockets, database)
    client = executor.sandbox("etienne")
    assert [call[0] for call in engine.calls] == [
        "inspect",
        "remove",
        "create",
        "start",
        "exec",
    ]
    info = database.sandbox("etienne")
    assert info is not None
    assert info.name == "cliquet-etienne"
    assert info.url == str(sockets / "etienne" / "sandbox.sock")
    assert info.sdk == WANTED
    assert info.ready_at == NOW
    assert info.token != "sandbox-token"
    assert engine.created["image"] == "cliquet-sandbox:latest"
    assert engine.created["env"]["CLIQUET_SANDBOX_TOKEN"] == info.token
    assert len(engine.created["env"]["CLIQUET_SANDBOX_SETUP"]) == 16
    assert engine.created["labels"] == {
        "cliquet.sandbox": "1",
        "cliquet.owner": "etienne",
        "cliquet.sdk": WANTED,
        "cliquet.sockets": str(sockets / "etienne"),
    }
    assert engine.created["host_config"] == {
        "NetworkMode": "none",
        "AutoRemove": False,
        "ReadonlyRootfs": True,
        "Tmpfs": {"/tmp": "rw,size=512m"},
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges"],
        "Memory": 2048 * 2**20,
        "Binds": [
            "cliquet-etienne-root:/sandbox",
            "cliquet-etienne-venv:/app/.venv",
            f"{sockets / 'etienne'}:/sock",
        ],
    }
    assert engine.created["subpath"] is False
    # Its volumes are labelled, so an operator finds every one the server made.
    assert engine.volumes == {
        "cliquet-etienne-root": {"cliquet.sandbox": "1", "cliquet.owner": "etienne"},
        "cliquet-etienne-venv": {"cliquet.sandbox": "1", "cliquet.owner": "etienne"},
    }
    # The container writes its socket here as its own user, so the folder is open to every one.
    assert (sockets / "etienne").stat().st_mode & 0o777 == 0o777
    assert fake_waits.sockets == [sockets / "etienne" / "sandbox.sock"]
    # The wait probes with a client that can't wake: this thread holds the owner's lock.
    assert fake_waits.clients[0]._wake is None
    assert client._wake is not None
    assert str(client._client.base_url) == "http://sandbox"
    executor.close()


def in_the_volume(state: dict[str, Any], owner: str = "etienne") -> dict[str, Any]:
    """The container as Docker's inspect answers it once its sockets are in the volume."""
    host = state["HostConfig"]
    host["Binds"] = [bind for bind in host["Binds"] if not bind.endswith(":/sock")]
    host["Mounts"] = [
        {
            "Type": "volume",
            "Source": "cliquet_sockets",
            "Target": "/sock",
            "VolumeOptions": {"Subpath": owner},
        }
    ]
    state["Config"]["Labels"]["cliquet.sockets"] = f"volume:cliquet_sockets/{owner}"
    return state


def test_a_server_in_a_container_keeps_the_sockets_in_a_volume(
    sockets: Path, database: Database, fake_waits: FakeWaits
) -> None:
    engine = FakeEngine()
    executor = executor_with(engine, sockets, database, sockets_volume="cliquet_sockets")
    executor.sandbox("etienne")
    # The volume is mounted at `sockets` in the server's container, and the sandbox mounts only
    # its owner's subfolder: never another owner's socket.
    assert engine.created["host_config"]["Binds"] == [
        "cliquet-etienne-root:/sandbox",
        "cliquet-etienne-venv:/app/.venv",
    ]
    assert engine.created["host_config"]["Mounts"] == [
        {
            "Type": "volume",
            "Source": "cliquet_sockets",
            "Target": "/sock",
            "VolumeOptions": {"Subpath": "etienne"},
        }
    ]
    assert engine.created["subpath"] is True
    assert engine.created["labels"]["cliquet.sockets"] == "volume:cliquet_sockets/etienne"
    assert (sockets / "etienne" / "sdk.tar.gz").exists()
    assert fake_waits.sockets == [sockets / "etienne" / "sandbox.sock"]
    executor.close()


@pytest.mark.parametrize("made", ["in the volume", "with a bind", "with the whole volume"])
def test_a_container_without_its_owner_s_subfolder_is_made_again(
    sockets: Path, database: Database, fake_waits: FakeWaits, made: str
) -> None:
    database.save_sandbox(ready_sandbox(sockets=sockets))
    engine = FakeEngine()
    engine.state = running_container(sockets=sockets)
    if made != "with a bind":
        in_the_volume(engine.state)
    if made == "with the whole volume":
        # As an older daemon would have made it, or a server before the subfolder.
        del engine.state["HostConfig"]["Mounts"][0]["VolumeOptions"]
    executor = executor_with(engine, sockets, database, sockets_volume="cliquet_sockets")
    executor.sandbox("etienne")
    kept = engine.calls == [("inspect", "cliquet-etienne")]
    assert kept == (made == "in the volume")
    executor.close()


def test_the_sdk_is_put_in_the_folder_the_sandbox_shares(database: Database, sockets: Path) -> None:
    engine = FakeEngine()
    executor = executor_with(engine, sockets, database)
    executor.sandbox("etienne")
    # The container reads it there, since a read-only root takes no upload.
    assert (sockets / "etienne" / "sdk.tar.gz").read_bytes() == sdk_archive(SDK)
    assert ("exec", ["sh", "-c", SETUP]) in engine.calls
    # A new SDK leaves nothing of an earlier one behind.
    assert "rm -rf /sandbox/sdk" in SETUP
    executor.close()


def test_a_socket_path_too_long_to_connect_to_is_refused(
    tmp_path: Path, database: Database
) -> None:
    engine = FakeEngine()
    deep = tmp_path / ("a" * 60) / ("b" * 60)
    executor = executor_with(engine, deep, database)
    with pytest.raises(SandboxFault, match="shorter folder"):
        executor.sandbox("etienne")
    assert engine.calls == []
    executor.close()


def test_a_container_ready_for_this_sdk_is_used_as_it_is(
    sockets: Path, database: Database, fake_waits: FakeWaits
) -> None:
    database.save_sandbox(ready_sandbox(sockets=sockets))
    engine = FakeEngine()
    engine.state = running_container(sockets=sockets)
    executor = executor_with(engine, sockets, database)
    client = executor.sandbox("etienne")
    assert engine.calls == [("inspect", "cliquet-etienne")]
    assert client._client.headers["x-sandbox-token"] == "sandbox-token"
    assert fake_waits.sockets == [] and fake_waits.clients == []
    executor.close()


@pytest.mark.parametrize(
    ("changed", "kept"),
    [("src/cliquet/renderer/cliquet.css", True), ("src/cliquet/worker.py", False)],
)
def test_only_new_sdk_code_means_a_new_container(
    sockets: Path,
    database: Database,
    fake_waits: FakeWaits,
    tmp_path: Path,
    changed: str,
    kept: bool,
) -> None:
    # Set up for this SDK, which a deploy then changes.
    database.save_sandbox(ready_sandbox(sockets=sockets))
    engine = FakeEngine()
    engine.state = running_container(sockets=sockets)
    executor = executor_with(engine, sockets, database, sdk_root=sdk_copy(tmp_path, changed))
    executor.sandbox("etienne")
    assert (engine.calls == [("inspect", "cliquet-etienne")]) == kept
    assert (("create", "cliquet-etienne") in engine.calls) != kept
    executor.close()


def test_a_stopped_container_is_started_again(
    sockets: Path, database: Database, fake_waits: FakeWaits
) -> None:
    database.save_sandbox(ready_sandbox(sockets=sockets))
    engine = FakeEngine()
    engine.state = {
        "Created": NOW.isoformat(),
        "State": {"Running": False, "Status": "exited"},
        "Config": {
            "User": "sandbox",
            "Labels": {"cliquet.sdk": WANTED, "cliquet.sockets": str(sockets / "etienne")},
        },
        "HostConfig": container_config(sockets),
    }
    executor = executor_with(engine, sockets, database)
    client = executor.sandbox("etienne")
    assert [call[0] for call in engine.calls] == ["inspect", "start"]
    assert client._client.headers["x-sandbox-token"] == "sandbox-token"
    assert fake_waits.clients[0]._wake is None
    assert client._wake is not None
    executor.close()


def test_a_container_with_other_limits_is_made_again(
    sockets: Path, database: Database, fake_waits: FakeWaits
) -> None:
    database.save_sandbox(ready_sandbox(sockets=sockets))
    engine = FakeEngine()
    engine.state = running_container(sockets=sockets)
    engine.state["HostConfig"]["CapDrop"] = []  # as a container from before the limits had it
    executor = executor_with(engine, sockets, database)
    executor.sandbox("etienne")
    kinds = [call[0] for call in engine.calls]
    assert kinds == ["inspect", "remove", "create", "start", "exec"]
    executor.close()


def test_an_idle_container_is_stopped(
    sockets: Path, database: Database, fake_waits: FakeWaits
) -> None:
    now = [0.0]
    engine = FakeEngine()
    executor = executor_with(engine, sockets, database, idle_seconds=60, monotonic=lambda: now[0])
    executor.sandbox("etienne")
    executor.stop_idle()
    assert not any(call[0] == "stop" for call in engine.calls)

    now[0] = 61.0
    executor.stop_idle()
    assert ("stop", "cliquet-etienne") in engine.calls
    executor.close()


def test_the_sweep_removes_what_the_server_lost(sockets: Path, database: Database) -> None:
    engine = FakeEngine()
    engine.found = ["cliquet-etienne", "cliquet-gone", "another-gone", "cliquet-"]
    executor = executor_with(engine, sockets, database)
    executor.sweep({"etienne"})
    removed = [call for call in engine.calls if call[0] == "remove"]
    assert removed == [("remove", "cliquet-gone")]
    assert ("remove_volume", "cliquet-gone-root") in engine.calls
    assert ("remove_volume", "cliquet-gone-venv") in engine.calls
    executor.close()


def test_destroying_an_owner_s_sandbox_removes_its_container_and_volumes(
    sockets: Path, database: Database, fake_waits: FakeWaits
) -> None:
    engine = FakeEngine()
    executor = executor_with(engine, sockets, database)
    client = executor.sandbox("etienne")
    # A row named with another prefix names a container that goes too.
    old = ready_sandbox(sockets=sockets)
    database.save_sandbox(SandboxInfo("etienne", "other-etienne", old.url, "t", WANTED, NOW))
    engine.calls.clear()
    executor.destroy("etienne")
    assert engine.calls == [
        ("remove", "other-etienne"),
        ("remove", "cliquet-etienne"),
        ("remove_volume", "cliquet-etienne-root"),
        ("remove_volume", "cliquet-etienne-venv"),
    ]
    assert not (sockets / "etienne").exists()
    assert client._client.is_closed
    # One that was never there is fine, and nothing is made.
    engine.calls.clear()
    executor.destroy("sam")
    assert [call[0] for call in engine.calls] == ["remove", "remove_volume", "remove_volume"]
    executor.close()


@pytest.mark.parametrize("stopped", [False, True])
def test_a_container_set_up_for_a_deleted_account_is_removed(
    sockets: Path, database: Database, fake_waits: FakeWaits, stopped: bool
) -> None:
    database.create_app("etienne", "shop", NOW)
    engine = FakeEngine()
    if stopped:
        # One that was stopped is woken, then its record refused.
        database.save_sandbox(ready_sandbox(sockets=sockets))
        engine.state = running_container(sockets=sockets)
        engine.state["State"] = {"Running": False, "Status": "exited"}
    executor = executor_with(engine, sockets, database)
    real = database.save_sandbox

    def deleted_meanwhile(info: SandboxInfo) -> None:
        database.close_account("etienne", NOW)
        real(info)

    database.save_sandbox = deleted_meanwhile  # type: ignore[method-assign]
    with pytest.raises(SandboxFault, match="account was deleted"):
        executor.sandbox("etienne")
    assert engine.calls[-3:] == [
        ("remove", "cliquet-etienne"),
        ("remove_volume", "cliquet-etienne-root"),
        ("remove_volume", "cliquet-etienne-venv"),
    ]
    assert ("start", "cliquet-etienne" if stopped else "the-container-id") in engine.calls
    assert not (sockets / "etienne").exists()
    executor.close()


def test_a_sweep_that_cannot_reach_docker_is_a_warning(
    sockets: Path,
    database: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = FakeEngine()
    executor = executor_with(engine, sockets, database)

    def broken(owners: set[str]) -> None:
        raise SandboxAway("Docker isn't there")

    monkeypatch.setattr(executor, "sweep", broken)
    apps = Apps(settings, database, executor, clock=lambda: NOW)
    apps.warm_up()
    assert not any(record.levelname == "ERROR" for record in caplog.records)
    assert any("weren't swept" in record.getMessage() for record in caplog.records)
    apps.close()


def test_the_warm_up_sweeps_what_the_server_lost(
    sockets: Path, database: Database, settings: Settings
) -> None:
    engine = FakeEngine()
    engine.found = ["cliquet-gone"]
    apps = Apps(settings, database, executor_with(engine, sockets, database), clock=lambda: NOW)
    apps.warm_up()
    assert ("remove", "cliquet-gone") in engine.calls
    apps.close()


@pytest.mark.parametrize("there", [False, True])
def test_a_container_that_isn_t_serving_is_set_up_again(
    sockets: Path, database: Database, there: bool
) -> None:
    database.save_sandbox(ready_sandbox(sockets=sockets))
    engine = FakeEngine()
    engine.state = running_container(running=False, sockets=sockets) if there else None
    executor = executor_with(engine, sockets, database)
    executor.sandbox("etienne")
    kinds = [call[0] for call in engine.calls]
    assert kinds == ["inspect", "remove", "create", "start", "exec"]
    # The token the row holds is the one the container gets, so a client keeps working.
    assert engine.created["env"]["CLIQUET_SANDBOX_TOKEN"] == "sandbox-token"
    executor.close()


def test_a_row_named_with_another_prefix_is_made_again(
    sockets: Path, database: Database, fake_waits: FakeWaits
) -> None:
    old = SandboxInfo(
        "etienne",
        "old-etienne",
        str(sockets / "etienne" / "sandbox.sock"),
        "keep-me",
        WANTED,
        NOW,
    )
    database.save_sandbox(old)
    engine = FakeEngine()
    executor = executor_with(engine, sockets, database)
    executor.sandbox("etienne")
    assert ("remove", "old-etienne") in engine.calls
    assert ("remove", "cliquet-etienne") in engine.calls
    executor.close()


def test_a_container_that_runs_as_root_is_made_again(
    sockets: Path, database: Database, fake_waits: FakeWaits
) -> None:
    database.save_sandbox(ready_sandbox(sockets=sockets))
    engine = FakeEngine()
    engine.state = running_container(sockets=sockets)
    engine.state["Config"]["User"] = ""  # an image that dropped its USER
    executor = executor_with(engine, sockets, database)
    executor.sandbox("etienne")
    kinds = [call[0] for call in engine.calls]
    assert kinds == ["inspect", "remove", "create", "start", "exec"]
    executor.close()


def test_a_rewritten_bind_path_is_still_ours(
    sockets: Path, database: Database, fake_waits: FakeWaits
) -> None:
    database.save_sandbox(ready_sandbox(sockets=sockets))
    engine = FakeEngine()
    engine.state = running_container(sockets=sockets)
    # Docker Desktop rewrites the host side of a bind; the container is still this server's.
    engine.state["HostConfig"]["Binds"] = [
        "cliquet-etienne-root:/sandbox",
        "cliquet-etienne-venv:/app/.venv",
        f"/host_mnt/private{sockets / 'etienne'}:/sock",
    ]
    executor = executor_with(engine, sockets, database)
    executor.sandbox("etienne")
    assert engine.calls == [("inspect", "cliquet-etienne")]
    executor.close()


def test_a_container_made_after_the_row_is_made_again(
    sockets: Path, database: Database, fake_waits: FakeWaits
) -> None:
    database.save_sandbox(ready_sandbox(sockets=sockets))
    engine = FakeEngine()
    engine.state = running_container(sockets=sockets, created=NOW + timedelta(seconds=5))
    executor = executor_with(engine, sockets, database)
    executor.sandbox("etienne")
    kinds = [call[0] for call in engine.calls]
    assert kinds == ["inspect", "remove", "create", "start", "exec"]
    executor.close()


def test_a_container_that_cannot_come_back_is_made_anew(
    sockets: Path, database: Database, fake_waits: FakeWaits, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.save_sandbox(ready_sandbox(sockets=sockets))
    engine = FakeEngine()
    engine.state = {
        "Created": NOW.isoformat(),
        "State": {"Running": False, "Status": "exited"},
        "Config": {
            "User": "sandbox",
            "Labels": {"cliquet.sdk": WANTED, "cliquet.sockets": str(sockets / "etienne")},
        },
        "HostConfig": container_config(sockets),
    }
    executor = executor_with(engine, sockets, database)

    def broken(owner: str, info: Any, socket: Path) -> SandboxClient:
        raise SandboxError("it can't come back")

    monkeypatch.setattr(executor, "_wake", broken)
    executor.sandbox("etienne")
    kinds = [call[0] for call in engine.calls]
    assert kinds == ["inspect", "remove", "create", "start", "exec"]
    executor.close()


def test_a_busy_container_is_not_stopped(
    sockets: Path, database: Database, fake_waits: FakeWaits
) -> None:
    now = [0.0]
    engine = FakeEngine()
    executor = executor_with(engine, sockets, database, idle_seconds=60, monotonic=lambda: now[0])
    client = executor.sandbox("etienne")
    assert client._on_call is not None and client._on_done is not None
    now[0] = 61.0
    client._on_call()  # a call began
    executor.stop_idle()
    assert not any(call[0] == "stop" for call in engine.calls)
    client._on_done()
    # Idle counts from the end of the call, so it is not idle until another period has passed.
    now[0] = 122.0
    executor.stop_idle()
    assert ("stop", "cliquet-etienne") in engine.calls
    executor.close()


def test_the_start_waits_for_the_marker_the_setup_writes() -> None:
    start = (Path(__file__).parents[3] / "docker" / "sandbox-start.sh").read_text()
    assert SETUP_DONE in start
    assert "$CLIQUET_SANDBOX_SETUP" in start


def test_a_new_sdk_means_a_new_container(
    sockets: Path, database: Database, fake_waits: FakeWaits
) -> None:
    old = SandboxInfo(
        "etienne",
        "cliquet-etienne",
        str(sockets / "etienne" / "sandbox.sock"),
        "keep-me",
        "old",
        NOW,
    )
    database.save_sandbox(old)
    engine = FakeEngine()
    engine.state = running_container(sdk="old")
    executor = executor_with(engine, sockets, database)
    executor.sandbox("etienne")
    kinds = [call[0] for call in engine.calls]
    assert kinds == ["inspect", "remove", "create", "start", "exec"]
    info = database.sandbox("etienne")
    assert info is not None and info.sdk == WANTED and info.token == "keep-me"
    assert engine.created["labels"]["cliquet.sdk"] == WANTED
    executor.close()


def test_a_failed_setup_is_said_with_the_container_s_log(sockets: Path, database: Database) -> None:
    engine = FakeEngine()
    engine.setup_code = 1
    engine.setup_output = "it went boom"
    engine.log = "starting\nthen boom"
    executor = executor_with(engine, sockets, database)
    with pytest.raises(SandboxFault) as error:
        executor.sandbox("etienne")
    said = str(error.value)
    assert "setting up the sandbox failed: it went boom" in said
    assert said.endswith("Its log ends with: starting\nthen boom")
    assert database.sandbox("etienne") is None
    # A container that didn't set up would answer nothing for ever.
    assert ("remove", "the-container-id") in engine.calls
    executor.close()


def test_a_container_that_doesn_t_answer_says_why(
    sockets: Path, database: Database, fake_waits: FakeWaits
) -> None:
    engine = FakeEngine()
    engine.log = "the sandbox said nothing"
    fake_waits.failure = SandboxError("the sandbox didn't start: it didn't answer")
    executor = executor_with(engine, sockets, database)
    with pytest.raises(SandboxFault, match="the sandbox didn't start: it didn't answer"):
        executor.sandbox("etienne")
    assert database.sandbox("etienne") is None
    assert ("logs", "the-container-id") in engine.calls
    executor.close()


def test_a_failed_setup_is_logged_once_as_unexpected(
    sockets: Path,
    settings: Settings,
    database: Database,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = FakeEngine()
    engine.setup_code = 1
    engine.setup_output = "it went boom"
    apps = Apps(
        settings,
        database,
        executor_with(engine, sockets, database),
        clock=lambda: NOW,
    )
    with pytest.raises(SandboxError, match="boom"), apps.sandbox("etienne"):
        pass
    record = one_unexpected(caplog)
    assert record.name == "cliquet_server.apps"
    assert record.getMessage().startswith(
        "unexpected: etienne's sandbox didn't start: the sandbox container didn't start: "
        "setting up the sandbox failed: it went boom"
    )
    apps.close()


def test_close_removes_no_container(
    sockets: Path, database: Database, fake_waits: FakeWaits
) -> None:
    engine = FakeEngine()
    executor = executor_with(engine, sockets, database)
    executor.sandbox("etienne")
    before = list(engine.calls)
    executor.close()
    assert engine.calls[len(before) :] == [("close", "")]
    assert not any(call[0] == "remove" for call in engine.calls[len(before) :])
    assert database.sandbox("etienne") is not None


def _image_is_there() -> bool:
    """Whether this machine has the sandbox image, which the real-Docker test needs."""
    try:
        found = subprocess.run(
            ["docker", "image", "inspect", "cliquet-sandbox:latest"], capture_output=True
        )
    except OSError:
        return False  # no docker command here: the real test can't run
    return found.returncode == 0


def _sockets_reach_the_host() -> bool:
    """Whether the host can reach a unix socket a container left in a bind mount.

    Docker Desktop on macOS runs its daemon in a Linux VM, which passes the socket's file on
    but no connection to it: there the server can't reach a sandbox at all.
    """
    return sys.platform != "darwin"


real_docker = pytest.mark.skipif(
    not (os.environ.get("CLIQUET_TEST_DOCKER") and _image_is_there() and _sockets_reach_the_host()),
    reason=(
        "needs a real Docker whose sockets reach the host, and the sandbox image: "
        "on Linux, build it, then set CLIQUET_TEST_DOCKER=1"
    ),
)
# Each test with it makes a container of its own, and removes it even when it fails.


@real_docker
@pytest.mark.sandbox
@pytest.mark.slow
def test_a_real_container_runs_an_app(
    sockets: Path, database: Database, fake_waits: FakeWaits
) -> None:
    # This test needs the sandbox's own answers, not the fakes.
    fake_waits.real = True
    prefix = f"cliquet-test-{secrets.token_hex(4)}-"
    name = f"{prefix}etienne"
    # A unix socket's path is short: a test's own folder is too long for one.
    sockets = Path(tempfile.mkdtemp(prefix="cliquet-sockets-"))
    api = DockerApi()
    executor = DockerExecutor(
        api, database, sockets, sockets_host=sockets, prefix=prefix, sdk_root=SDK
    )
    try:
        client = executor.sandbox("etienne")
        code = example_code()
        version = version_id(code)
        built = client.put_version("etienne", "shop", version, code)
        assert built.manifest["name"] == "shop"
        files = encoded(example_data())
        request = {
            "op": "query",
            "caller": {"id": "t", "role": "owner"},
            "now": NOW.isoformat(),
            "name": "stock",
            "arguments": {},
            "files": files,
        }
        assert len(client.run("etienne", "shop", version, request)["result"]) == 5

        # Pictures, drawn by the Chromium in the image, without its own sandbox.
        shots = client.pictures("etienne", "shop", version, "staff", example_data(), ["till"])
        assert [shot.view for shot in shots] == ["till", "till"]
        assert shots[0].height and shots[0].height > 0

        # The container has no network at all: no DNS, no site, and no route to the server.
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
    finally:
        try:
            subprocess.run(["docker", "rm", "--force", name], capture_output=True)
            subprocess.run(
                ["docker", "volume", "rm", "--force", f"{prefix}etienne-root"],
                capture_output=True,
            )
        finally:
            executor.close()
            shutil.rmtree(sockets, ignore_errors=True)
