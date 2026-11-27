"""The Docker executor: one sandbox container per owner, on this machine's Docker."""

import contextlib
import json as json_module
import logging
import os
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from cliquet.errors import report
from cliquet.store import write_atomically
from cliquet.wheels import Wheels, default_cache
from cliquet_server.db import Database, SandboxInfo
from cliquet_server.executor import (
    ExecResult,
    SandboxAway,
    SandboxClient,
    SandboxError,
    SandboxFault,
    SdkArchive,
    owned,
    save_or_destroy,
    wait_until_healthy,
)
from cliquet_server.settings import (
    DEFAULT_PREFIX,
    DEFAULT_SANDBOX_IDLE_SECONDS,
    DEFAULT_SANDBOX_IMAGE,
    DEFAULT_SANDBOX_MEMORY_MB,
)

_log = logging.getLogger(__name__)

# The file the setup command writes last, named for this setup: the image's sandbox waits for it
# before it starts, so it never runs a line of the image's own copy or of an earlier SDK.
SETUP_DONE = "/sandbox/ready-"

# The setup command, run in a new container: the server's own SDK, from the archive it put in the
# sandbox's own folder, into the container's volume, with the dependencies the image holds. It
# reaches no package index.
SETUP = (
    "rm -rf /sandbox/sdk && mkdir -p /sandbox/sdk && "
    "tar -xzf /sock/sdk.tar.gz -C /sandbox/sdk && "
    "uv pip install --quiet --offline --no-deps --force-reinstall -e /sandbox/sdk && "
    f'touch "{SETUP_DONE}$CLIQUET_SANDBOX_SETUP"'
)

# How long a call to Docker waits, unless it says otherwise: the setup runs longer.
API_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
SETUP_TIMEOUT = 300.0
# How long the container has to open its socket, and then to answer as this SDK's sandbox.
SOCKET_WAIT = 60.0
# The longest socket path Cliquet takes: a unix socket's own is 104 bytes on macOS, with the
# byte that ends it.
MAX_SOCKET_PATH = 100
# How many owners' containers are set up at once: each is a create, an install and a wait.
SETUPS_AT_ONCE = 4
# The API of Docker Engine 26, the first to mount one subfolder of a volume. An older daemon
# ignores the subfolder and mounts the whole volume, with every owner's socket, so a container
# that needs one is made through this version, which an older daemon refuses.
SUBPATH_API = "1.45"


def parse_stdcopy(stream: bytes) -> tuple[bytes, bytes]:
    """What Docker's framed stream carries: its standard output and its standard error."""
    frames = _frames(stream)
    stdout = b"".join(body for kind, body in frames if kind != 2)
    stderr = b"".join(body for kind, body in frames if kind == 2)
    return stdout, stderr


def stdcopy_text(stream: bytes) -> str:
    """Docker's framed stream as text, in the order it was printed."""
    return b"".join(body for _, body in _frames(stream)).decode(errors="replace")


def _frames(stream: bytes) -> list[tuple[int, bytes]]:
    """Docker's frames: each one's kind (1 standard output, 2 standard error) and its bytes.

    A stream that isn't framed, as a terminal's isn't, is all standard output.
    """
    if len(stream) < 8 or stream[0] not in (0, 1, 2) or stream[1:4] != b"\0\0\0":
        return [(1, stream)] if stream else []
    frames = []
    position = 0
    while position + 8 <= len(stream):
        kind = stream[position]
        size = int.from_bytes(stream[position + 4 : position + 8], "big")
        frames.append((kind, stream[position + 8 : position + 8 + size]))
        position += 8 + size
    return frames


class DockerApi:
    """The few calls of Docker's Engine API that the server needs.

    It speaks to the daemon over its unix socket, as an HTTP client: the server never runs the
    `docker` command, and needs no library for it.
    """

    def __init__(
        self,
        socket: str = "/var/run/docker.sock",
        *,
        base_url: str = "http://docker",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            transport=transport or httpx.HTTPTransport(uds=socket),
            timeout=API_TIMEOUT,
        )

    def inspect(self, name: str) -> dict[str, Any] | None:
        """The container, or None when it doesn't exist."""
        path = f"/containers/{name}/json"
        response = self._request("GET", path)
        if response.status_code == 404:
            return None
        return self._json(response, "GET", path)

    def create(
        self,
        name: str,
        *,
        image: str,
        env: Mapping[str, str],
        labels: Mapping[str, str],
        host_config: Mapping[str, Any],
        subpath: bool = False,
    ) -> str:
        """Make the container, and return its id."""
        # Through Engine 26's API when it mounts a volume's subfolder: an older daemon then
        # refuses it, rather than mount the whole volume.
        body = {
            "Image": image,
            "Env": [f"{key}={value}" for key, value in env.items()],
            "Labels": dict(labels),
            "HostConfig": dict(host_config),
        }
        path = f"/v{SUBPATH_API}/containers/create" if subpath else "/containers/create"
        response = self._request("POST", path, params={"name": name}, json=body)
        if subpath and response.status_code == 400 and "too new" in _said(response):
            raise SandboxFault(
                "keeping the sockets in a volume needs Docker Engine 26 or later, and this "
                f"daemon is older: upgrade Docker. It said: {_said(response)}"
            )
        return str(self._json(response, "POST", path)["Id"])

    def create_volume(self, name: str, *, labels: Mapping[str, str]) -> None:
        """Make the volume with these labels, or find it there, as it is."""
        body = {"Name": name, "Labels": dict(labels)}
        self._answer(self._request("POST", "/volumes/create", json=body), "POST", "/volumes/create")

    def start(self, container: str) -> None:
        """Start the container, or find it already running."""
        path = f"/containers/{container}/start"
        response = self._request("POST", path)
        if response.status_code != 304:
            self._answer(response, "POST", path)

    def stop(self, container: str, *, wait: int = 10) -> None:
        """Stop the container, or find it already stopped."""
        path = f"/containers/{container}/stop"
        response = self._request("POST", path, params={"t": str(wait)}, timeout=wait + 30)
        if response.status_code != 304:
            self._answer(response, "POST", path)

    def remove(self, container: str, *, force: bool = True) -> None:
        """Remove the container. A missing one is fine, and its volumes are never removed."""
        path = f"/containers/{container}"
        response = self._request("DELETE", path, params={"force": "1" if force else "0", "v": "0"})
        if response.status_code != 404:
            self._answer(response, "DELETE", path)

    def remove_volume(self, name: str) -> None:
        """Remove the volume. A missing one is fine."""
        path = f"/volumes/{name}"
        response = self._request("DELETE", path)
        if response.status_code != 404:
            self._answer(response, "DELETE", path)

    def containers(self, *, label: str) -> list[str]:
        """The names of the containers with this label, running or not."""
        filters = json_module.dumps({"label": [label]})
        answer = self._json(
            self._request("GET", "/containers/json", params={"all": "1", "filters": filters}),
            "GET",
            "/containers/json",
        )
        return [one["Names"][0].lstrip("/") for one in answer if one.get("Names")]

    def exec(
        self, container: str, command: list[str], *, timeout: float = SETUP_TIMEOUT
    ) -> ExecResult:
        """Run a command in the container, and return what it printed and its exit code."""
        made = self._json(
            self._request(
                "POST",
                f"/containers/{container}/exec",
                json={"Cmd": command, "AttachStdout": True, "AttachStderr": True},
            ),
            "POST",
            f"/containers/{container}/exec",
        )
        one = str(made["Id"])
        path = f"/exec/{one}/start"
        response = self._request(
            "POST", path, json={"Detach": False, "Tty": False}, timeout=timeout
        )
        self._answer(response, "POST", path)
        stdout, stderr = parse_stdcopy(response.content)
        answer = self._json(self._request("GET", f"/exec/{one}/json"), "GET", f"/exec/{one}/json")
        code = answer.get("ExitCode")
        return ExecResult(
            code if isinstance(code, int) else -1,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )

    def logs(self, container: str, tail: int = 20) -> str:
        """The last lines the container printed."""
        path = f"/containers/{container}/logs"
        params = {"stdout": "1", "stderr": "1", "tail": str(tail)}
        response = self._request("GET", path, params=params)
        self._answer(response, "GET", path)
        return stdcopy_text(response.content)

    def close(self) -> None:
        """Close the HTTP connection."""
        self._client.close()

    def _request(
        self, method: str, path: str, *, timeout: httpx.Timeout | float = API_TIMEOUT, **kwargs: Any
    ) -> httpx.Response:
        try:
            return self._client.request(method, path, timeout=timeout, **kwargs)
        except httpx.TimeoutException:
            seconds = timeout.read if isinstance(timeout, httpx.Timeout) else timeout
            raise SandboxAway(f"Docker took more than {seconds:g} seconds") from None
        except httpx.TransportError as error:
            if _denied(error):
                raise SandboxAway(
                    "the server may not use Docker's socket: give its user the socket's group, "
                    "with DOCKER_GID in docker/.env when it runs with Docker Compose"
                ) from error
            error_id = report(error, f"can't reach Docker: {error}")
            raise SandboxAway(
                f"can't reach Docker's daemon. Error {error_id} has the details."
            ) from error

    def _answer(self, response: httpx.Response, method: str, path: str) -> None:
        if 200 <= response.status_code < 300:
            return
        said = _said(response)
        raise SandboxError(f"Docker answered {response.status_code} to {method} {path}: {said}")

    def _json(self, response: httpx.Response, method: str, path: str) -> Any:
        self._answer(response, method, path)
        try:
            return response.json()
        except ValueError as error:
            raise SandboxError(f"Docker answered {method} {path} with no JSON: {error}") from error


def _denied(error: BaseException) -> bool:
    """Whether a failed connection was refused for the socket's permissions."""
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, PermissionError):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


def socket_path_problem(path: Path) -> str | None:
    """Why the server can't connect to the sandbox's socket path, or None when it fits."""
    length = len(os.fsencode(path))
    if length <= MAX_SOCKET_PATH:
        return None
    return (
        f"the sandbox's socket path takes {length} bytes, and a unix socket's at most "
        f"{MAX_SOCKET_PATH}: set CLIQUET_SANDBOX_SOCKETS to a shorter folder."
    )


def wait_for_socket(path: Path, seconds: float = SOCKET_WAIT) -> None:
    """Wait until the sandbox has opened its socket, for at most `seconds`."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.is_socket():
            return
        time.sleep(0.05)
    raise SandboxFault("the sandbox didn't open its socket")


class DockerExecutor:
    """One sandbox container per owner, on this machine's Docker."""

    def __init__(
        self,
        api: DockerApi,
        database: Database,
        sockets: Path,
        *,
        sockets_host: Path,
        sockets_volume: str | None = None,
        image: str = DEFAULT_SANDBOX_IMAGE,
        prefix: str = DEFAULT_PREFIX,
        idle_seconds: int = DEFAULT_SANDBOX_IDLE_SECONDS,
        memory_mb: int = DEFAULT_SANDBOX_MEMORY_MB,
        sdk_root: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        wheels: Wheels | None = None,
    ) -> None:
        self._api = api
        self._database = database
        self._sockets = Path(sockets)
        self._sockets_host = sockets_host
        # The volume the server's container keeps the sockets in, mounted at `sockets` there:
        # each sandbox mounts only its owner's subfolder of it.
        self._sockets_volume = sockets_volume
        self._image = image
        self._prefix = prefix
        self._clock = clock or (lambda: datetime.now(UTC))
        self._wheels = wheels or Wheels(default_cache(), sdk=sdk_root)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._clients: dict[str, SandboxClient] = {}
        self._sdk = SdkArchive(sdk_root)
        # How many owners' containers are set up at once, since each takes a while.
        self._setups = threading.Semaphore(SETUPS_AT_ONCE)
        self._memory = memory_mb * 2**20
        # A scratch folder for Chromium, the previews and uv's cache, which the root lacks.
        self._tmpfs = max(64, memory_mb // 4)
        # When each owner's sandbox last took a call, to stop the ones left idle.
        self._idle = idle_seconds
        self._monotonic = monotonic or time.monotonic
        self._used: dict[str, float] = {}
        self._calls: dict[str, int] = {}
        self._stopping = threading.Event()
        self._sweeper = self._start_sweeper()

    def sandbox(self, owner: str) -> SandboxClient:
        """The client for this owner's container, started or set up again if it isn't there."""
        with self._lock_for(owner):
            return self._set_up(owner)

    def close(self) -> None:
        """Close the clients and stop the sweeper. Never removes a container."""
        self._stopping.set()
        if self._sweeper is not None:
            self._sweeper.join(timeout=5)
        with self._locks_guard:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.close()
        self._api.close()

    def stop_idle(self) -> None:
        """Stop the containers no call has used for `idle_seconds`, so a small host can rest."""
        if self._idle <= 0:
            return
        now = self._monotonic()
        with self._locks_guard:
            owners = list(self._clients)
        for owner in owners:
            last = self._used.get(owner)
            if self._calls.get(owner, 0) or last is None or now - last < self._idle:
                continue
            name = self._name(owner)
            try:
                self._api.stop(name)
            except SandboxError as error:
                _log.warning("sandbox %s: not stopped after %s s idle: %s", name, self._idle, error)
                continue
            self._used.pop(owner, None)
            _log.info("sandbox %s: stopped after %s s idle", name, self._idle)

    def sweep(self, owners: set[str]) -> None:
        """Remove the containers and volumes of owners the server no longer has."""
        for name in self._api.containers(label="cliquet.sandbox=1"):
            if not name.startswith(self._prefix):
                continue  # another server's container
            owner = name[len(self._prefix) :]
            if not owner or owner in owners:
                continue
            try:
                self._remove(owner)
            except SandboxError as error:
                _log.warning("sandbox %s: not swept at boot: %s", name, error)

    def _start_sweeper(self) -> threading.Thread | None:
        """Start the thread that stops idle containers, unless they are never stopped."""
        if self._idle <= 0:
            return None
        thread = threading.Thread(target=self._sweep_loop, name="sandbox-idle", daemon=True)
        thread.start()
        return thread

    def _sweep_loop(self) -> None:
        """Stop idle containers, once a minute or once per idle period, whichever is shorter."""
        while not self._stopping.wait(min(float(self._idle), 60.0)):
            try:
                self.stop_idle()
            except BaseException:
                _log.exception("unexpected: the idle sandboxes weren't stopped")

    def _set_up(self, owner: str) -> SandboxClient:
        """The owner's client, from its container ready for this SDK or made anew. Expects the
        owner's lock, so that one container is made once."""
        archive, wanted = self._sdk.get()
        info = self._database.sandbox(owner)
        name = self._name(owner)
        socket = self._socket(owner)
        problem = socket_path_problem(socket)
        if problem is not None:
            raise SandboxFault(problem)
        container: str | None = None
        try:
            found = self._api.inspect(name)
            if self._ready(info, found, wanted, name, owner):
                assert info is not None
                client = self._clients.get(owner) or self._client(socket, info.token, owner)
                self._clients[owner] = client
                self._note_used(owner)
                return client
            if self._stopped(info, found, wanted, name, owner):
                assert info is not None
                try:
                    return self._wake(owner, info, socket)
                except SandboxError:
                    pass  # the container can't come back: it is made anew below
            token = info.token if info is not None and info.token else secrets.token_hex(24)
            # This setup's marker: a container made again must wait for its own setup.
            setup = secrets.token_hex(8)
            # The container writes its socket here as its own user, and reads the SDK from it:
            # the folder and the socket must be open to every user, since the two may differ.
            socket.parent.mkdir(parents=True, exist_ok=True)
            socket.parent.chmod(0o777)
            # Not `write_bytes`: a path app code can write must not be followed when it is a link.
            write_atomically(socket.parent / "sdk.tar.gz", archive)
            # A socket left by the container before: the new sandbox can't bind it.
            socket.unlink(missing_ok=True)
            if info is not None and info.name != name:
                # A container named for another prefix, as on Sprites: it serves nobody now.
                self._api.remove(info.name)
            with self._setups:
                self._api.remove(name)
                # Labelled, so an operator finds every volume the server made.
                for volume in (self._volume(owner), self._venv(owner)):
                    self._api.create_volume(
                        volume, labels={"cliquet.sandbox": "1", "cliquet.owner": owner}
                    )
                container = self._api.create(
                    name,
                    image=self._image,
                    env={"CLIQUET_SANDBOX_TOKEN": token, "CLIQUET_SANDBOX_SETUP": setup},
                    labels={
                        "cliquet.sandbox": "1",
                        "cliquet.owner": owner,
                        "cliquet.sdk": wanted,
                        # The daemon may rewrite a bind path, so the folder is named here.
                        "cliquet.sockets": self._sockets_label(owner),
                    },
                    host_config=self._host_config(owner),
                    subpath=self._sockets_volume is not None,
                )
                self._api.start(container)
                made = self._api.exec(container, ["sh", "-c", SETUP])
                if made.code != 0:
                    raise SandboxFault(
                        f"setting up the sandbox failed: {(made.stdout + made.stderr)[-2000:]}"
                    )
                wait_for_socket(socket)
                # A probe with no wake: this thread holds the owner's lock, and a wake takes it.
                probe = self._client(socket, token, owner, wake=False)
                try:
                    wait_until_healthy(probe)
                finally:
                    probe.close()
                client = self._client(socket, token, owner)
        except SandboxError as error:
            failure = self._failure(container, error)
            if container is not None:
                # A container that didn't set up would answer nothing for ever: remove it.
                with contextlib.suppress(SandboxError):
                    self._api.remove(container)
            raise failure from error
        made = SandboxInfo(owner, name, str(socket), token, wanted, self._clock())
        save_or_destroy(self._database, made, client, lambda: self._destroy(owner))
        self._clients[owner] = client
        self._note_used(owner)
        return client

    def _wake(self, owner: str, info: SandboxInfo, socket: Path) -> SandboxClient:
        """Start a container that was set up and stopped, and wait for its service."""
        started = time.monotonic()
        self._api.start(info.name)
        # As in `_set_up`: the probe must not wake while this thread holds the owner's lock.
        probe = self._client(socket, info.token, owner, wake=False)
        try:
            wait_until_healthy(probe)
        finally:
            probe.close()
        client = self._clients.get(owner) or self._client(socket, info.token, owner)
        woken = SandboxInfo(owner, info.name, str(socket), info.token, info.sdk, self._clock())
        save_or_destroy(self._database, woken, client, lambda: self._destroy(owner))
        self._clients[owner] = client
        self._note_used(owner)
        _log.info("sandbox %s: awake in %.1f s", info.name, time.monotonic() - started)
        return client

    def destroy(self, owner: str) -> None:
        """Remove the owner's container, the one its record names with another prefix, both
        its volumes and its socket's folder, and forget its client."""
        with self._lock_for(owned(owner)):
            self._destroy(owner)

    def _destroy(self, owner: str) -> None:
        """What `destroy` does. Expects the owner's lock."""
        with self._locks_guard:
            client = self._clients.pop(owner, None)
        if client is not None:
            client.close()
        self._used.pop(owner, None)
        info = self._database.sandbox(owner)
        if info is not None and info.name != self._name(owner):
            self._api.remove(info.name)
        self._remove(owner)
        shutil.rmtree(self._socket(owner).parent, ignore_errors=True)

    def _remove(self, owner: str) -> None:
        """Remove the owner's container and both its volumes."""
        self._api.remove(self._name(owner))
        self._api.remove_volume(self._volume(owner))
        self._api.remove_volume(self._venv(owner))

    def _note_used(self, owner: str) -> None:
        self._used[owner] = self._monotonic()

    def _entered(self, owner: str) -> None:
        """A call began: the container may not be stopped until it ends."""
        self._calls[owner] = self._calls.get(owner, 0) + 1
        self._note_used(owner)

    def _left(self, owner: str) -> None:
        """A call ended: the container may rest once it has been idle long enough."""
        self._calls[owner] = max(0, self._calls.get(owner, 0) - 1)
        self._note_used(owner)

    def _ready(
        self,
        info: SandboxInfo | None,
        found: dict[str, Any] | None,
        wanted: str,
        name: str,
        owner: str,
    ) -> bool:
        """Whether the container that is there serves this SDK, so that it is used as it is."""
        if info is None or info.ready_at is None or info.sdk != wanted or info.name != name:
            return False
        if found is None or (found.get("State") or {}).get("Running") is not True:
            return False
        if not self._created_before(found, info.ready_at):
            return False
        labels = (found.get("Config") or {}).get("Labels")
        return (
            isinstance(labels, dict)
            and labels.get("cliquet.sdk") == wanted
            and self._configured(found, owner)
        )

    def _stopped(
        self,
        info: SandboxInfo | None,
        found: dict[str, Any] | None,
        wanted: str,
        name: str,
        owner: str,
    ) -> bool:
        """Whether the container that is there was set up for this SDK and is stopped."""
        if info is None or info.sdk != wanted or info.name != name or not info.token:
            return False
        if found is None:
            return False
        state = found.get("State") or {}
        if not self._created_before(found, info.ready_at):
            return False
        labels = (found.get("Config") or {}).get("Labels")
        return (
            state.get("Status") == "exited"
            and isinstance(labels, dict)
            and labels.get("cliquet.sdk") == wanted
            and self._configured(found, owner)
        )

    def _created_before(self, found: dict[str, Any], ready_at: datetime | None) -> bool:
        """Whether the container was made before the row was written, so the row describes it."""
        if ready_at is None:
            return False
        if ready_at.tzinfo is None:
            ready_at = ready_at.replace(tzinfo=UTC)
        created = found.get("Created")
        if not isinstance(created, str):
            return False
        try:
            made = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            return False
        return made <= ready_at + timedelta(seconds=1)

    def _configured(self, found: dict[str, Any], owner: str) -> bool:
        """Whether the container has this version's limits, so an older one is made again."""
        host = found.get("HostConfig") or {}
        config = found.get("Config") or {}
        labels = config.get("Labels") or {}
        user = str(config.get("User") or "")
        security = host.get("SecurityOpt") or []
        wanted = self._host_config(owner)
        # A bind's source may be rewritten by the daemon, as Docker Desktop does: the target and
        # the two volumes' names are what can be compared, and the folder is in a label.
        mounted: dict[str, str] = {}
        for bind in host.get("Binds") or []:
            source, _, target = str(bind).rpartition(":")
            mounted[target] = source
        return (
            # An image that dropped its USER would run app code as root: refuse it.
            user not in ("", "root", "0")
            and not user.startswith("0:")
            and labels.get("cliquet.sockets") == self._sockets_label(owner)
            and mounted.get("/sandbox") == self._volume(owner)
            and mounted.get("/app/.venv") == self._venv(owner)
            and self._sockets_mounted(host, mounted, owner)
            and host.get("NetworkMode") == wanted["NetworkMode"]
            and host.get("ReadonlyRootfs") is True
            and (host.get("Tmpfs") or {}).get("/tmp") == wanted["Tmpfs"]["/tmp"]
            and set(host.get("CapDrop") or []) == set(wanted["CapDrop"])
            and ("no-new-privileges" in security or "no-new-privileges:true" in security)
            and host.get("Memory") == wanted["Memory"]
        )

    def _sockets_mounted(self, host: dict[str, Any], mounted: dict[str, str], owner: str) -> bool:
        """Whether the container mounts its owner's sockets' folder as this server would."""
        if self._sockets_volume is None:
            return "/sock" in mounted
        # Only the owner's subfolder: a container that mounts the whole volume is made again.
        return "/sock" not in mounted and any(
            one.get("Type") == "volume"
            and one.get("Source") == self._sockets_volume
            and one.get("Target") == "/sock"
            and (one.get("VolumeOptions") or {}).get("Subpath") == owner
            for one in host.get("Mounts") or []
        )

    def _host_config(self, owner: str) -> dict[str, Any]:
        """How the container runs: no network, no way up, a read-only root, and its own folders."""
        config: dict[str, Any] = {
            "NetworkMode": "none",
            "AutoRemove": False,
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": f"rw,size={self._tmpfs}m"},
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Memory": self._memory,
            "Binds": [f"{self._volume(owner)}:/sandbox", f"{self._venv(owner)}:/app/.venv"],
        }
        if self._sockets_volume is None:
            config["Binds"].append(f"{self._sockets_host / owner}:/sock")
        else:
            config["Mounts"] = [
                {
                    "Type": "volume",
                    "Source": self._sockets_volume,
                    "Target": "/sock",
                    "VolumeOptions": {"Subpath": owner},
                }
            ]
        return config

    def _sockets_label(self, owner: str) -> str:
        """Where this owner's sockets' folder is, as the daemon names it: a path or a volume's."""
        if self._sockets_volume is None:
            return str(self._sockets_host / owner)
        return f"volume:{self._sockets_volume}/{owner}"

    def _failure(self, container: str | None, error: SandboxError) -> SandboxError:
        """Why a container wasn't set up, in Cliquet's words, with what it printed."""
        said = str(error).strip()
        if not said.endswith((".", "!", "?")):
            said = f"{said}."
        tail = self._tail(container) if container is not None else ""
        # A daemon that can't be reached is worth another call; a setup that failed is not.
        fault = SandboxAway if isinstance(error, SandboxAway) else SandboxFault
        return fault(
            f"the sandbox container didn't start: {said} Its log ends with: {tail}"
            if tail
            else f"the sandbox container didn't start: {said}"
        )

    def _tail(self, container: str) -> str:
        """The last lines the container printed: nothing when it has no log to read."""
        try:
            text = self._api.logs(container, tail=20)
        except SandboxError:
            return ""
        return "\n".join(text.splitlines()[-20:])

    def _client(self, socket: Path, token: str, owner: str, *, wake: bool = True) -> SandboxClient:
        """A client for this owner's sandbox: the probe of a setup passes `wake=False`."""
        return SandboxClient(
            "http://sandbox",
            token,
            socket=str(socket),
            wheels=self._wheels,
            on_call=lambda: self._entered(owner),
            on_done=lambda: self._left(owner),
            wake=(lambda: self._wake_client(owner)) if wake else None,
        )

    def _wake_client(self, owner: str) -> None:
        """Start the owner's container again if it was stopped, for a client's next retry."""
        self.sandbox(owner)

    def _name(self, owner: str) -> str:
        """The name of this owner's container."""
        return f"{self._prefix}{owner}"

    def _volume(self, owner: str) -> str:
        """The name of the volume this owner's app versions are kept in."""
        return f"{self._prefix}{owner}-root"

    def _venv(self, owner: str) -> str:
        """The name of the volume the sandbox's own Python environment is kept in."""
        return f"{self._prefix}{owner}-venv"

    def _socket(self, owner: str) -> Path:
        """This owner's socket, which only its sandbox can be reached on."""
        return self._sockets / owner / "sandbox.sock"

    def _lock_for(self, owner: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(owner, threading.Lock())


def _said(response: httpx.Response) -> str:
    """What Docker said of a failed call: its message, cut to the first 300 characters."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    message = body.get("message") if isinstance(body, dict) else None
    return (message if isinstance(message, str) else response.text)[:300]
