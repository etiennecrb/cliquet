"""The Sprites executor: one Fly Sprite per owner, with the sandbox as its service."""

import logging
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from cliquet.errors import report
from cliquet.wheels import Wheels, default_cache
from cliquet_server.db import Database, SandboxInfo
from cliquet_server.executor import (
    ExecResult,
    SandboxAway,
    SandboxClient,
    SandboxError,
    SdkArchive,
    owned,
    save_or_destroy,
    wait_until_healthy,
)
from cliquet_server.limiter import Once

_log = logging.getLogger(__name__)

START_SANDBOX = """\
#!/bin/bash
export CLIQUET_SANDBOX_TOKEN="$(cat /home/sprite/sandbox-token)"
export CLIQUET_SDK_PATH=/home/sprite/cliquet-sdk
export PATH="$HOME/.local/bin:$PATH"
exec /home/sprite/sandbox-venv/bin/python -m cliquet.cli sandbox \\
  --host 0.0.0.0 --port 8080 --root /home/sprite/sandbox
"""

SETUP = """\
set -e
chmod 600 /home/sprite/sandbox-token
chmod 755 /home/sprite/start-sandbox.sh
rm -rf /home/sprite/cliquet-sdk
mkdir -p /home/sprite/cliquet-sdk /home/sprite/sandbox
tar -xzf /home/sprite/cliquet-sdk.tar.gz -C /home/sprite/cliquet-sdk
uv python install 3.14
[ -x /home/sprite/sandbox-venv/bin/python ] || uv venv --python 3.14 /home/sprite/sandbox-venv
uv pip install --python /home/sprite/sandbox-venv/bin/python -e /home/sprite/cliquet-sdk
uvx --python 3.14 playwright install --with-deps chromium
"""

# The Sprite's policies. Measured on 2026-11-13: these are the
# hosts setup and builds ask for. A denied name gets `REFUSED` from the Sprite's own resolver.
SETUP_HOSTS = (
    "pypi.org",
    "files.pythonhosted.org",
    "releases.astral.sh",
    "archive.ubuntu.com",
    "security.ubuntu.com",
    "cdn.playwright.dev",
    "playwright.download.prss.microsoft.com",
    "storage.googleapis.com",
)
_DENY_THE_REST = {"action": "deny", "domain": "*"}
SETUP_POLICY: dict[str, Any] = {
    "rules": [*({"action": "allow", "domain": host} for host in SETUP_HOSTS), _DENY_THE_REST]
}
"""The network while a new Sprite is set up, before any app code has run there."""
BUILD_POLICY: dict[str, Any] = {"rules": [_DENY_THE_REST]}
"""The network from then on: none. The server sends the wheels each version needs.
Never send an empty list of rules: Sprites reads it as no policy, an open network."""
RESOURCES_POLICY: dict[str, Any] = {"memory": {"limit_mb": 8192, "autoscale": False}}
"""Fly's default memory limit, written down so that it is checked. Reaching it restarts the
Sprite, with every app of its owner."""
POLICY_KINDS = ("network", "resources")


def parse_exec(stream: bytes) -> ExecResult:
    """Read the answer of an `exec` call: framed standard output, standard error and exit code."""
    stdout, stderr = bytearray(), bytearray()
    current: bytearray | None = None
    code = -1
    position = 0
    while position < len(stream):
        byte = stream[position]
        if byte == 0x01:
            current = stdout
        elif byte == 0x02:
            current = stderr
        elif byte == 0x03:
            if position + 1 < len(stream):
                code = stream[position + 1]
            break
        elif current is not None:
            current.append(byte)
        position += 1
    return ExecResult(code, stdout.decode(errors="replace"), stderr.decode(errors="replace"))


# How long a call to the Sprites API waits, unless it says otherwise. A new Sprite can take
# more than httpx's default of 5 s to answer its first calls, while its container starts.
API_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# How long after an owner's last use of its sandbox the next call may pay for a Sprite waking:
# a Sprite goes cold about 30 s after its last command.
COLD_AFTER = 30.0
# How long the probe of a waking sandbox waits for it to answer. It has its own, shorter than a
# call's: the caller waits behind it, and its own call wakes the Sprite when the probe gives up.
WAKE_TIMEOUT = 10.0
# How long the Sprite's state, which only the wake line reads, may take: a slow control plane
# must not hold the caller's own call behind it.
STATE_TIMEOUT = 1.0


class SpritesApi:
    """The few calls of the Sprites API that the server needs."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.sprites.dev",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=API_TIMEOUT,
        )

    def get(self, name: str, timeout: httpx.Timeout | float = API_TIMEOUT) -> dict[str, Any] | None:
        """The Sprite, or None when it doesn't exist."""
        response = self._request("GET", f"/v1/sprites/{name}", timeout=timeout)
        if response.status_code == 404:
            return None
        return self._json(response, "GET", f"/v1/sprites/{name}")

    def create(self, name: str) -> dict[str, Any]:
        """Make a Sprite, and return it."""
        response = self._request("POST", "/v1/sprites", json={"name": name})
        return self._json(response, "POST", "/v1/sprites")

    def destroy(self, name: str) -> None:
        """Destroy the Sprite. A missing one is fine."""
        response = self._request("DELETE", f"/v1/sprites/{name}")
        if response.status_code != 404:
            self._answer(response, "DELETE", f"/v1/sprites/{name}")

    def exec(
        self,
        name: str,
        command: list[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float = 900,
    ) -> ExecResult:
        """Run a command in the Sprite, and return its output and code."""
        params: list[tuple[str, str]] = [("cmd", part) for part in command]
        for key, value in (env or {}).items():
            params.append(("env", f"{key}={value}"))
        path = f"/v1/sprites/{name}/exec"
        response = self._request("POST", path, params=params, timeout=timeout)
        self._answer(response, "POST", path)
        return parse_exec(response.content)

    def write(self, name: str, path: str, content: bytes) -> None:
        """Write a file in the Sprite."""
        target = f"/v1/sprites/{name}/fs/write"
        response = self._request("PUT", target, params={"path": path}, content=content)
        self._answer(response, "PUT", target)

    def set_policy(self, name: str, kind: str, body: dict[str, Any]) -> None:
        """Set one of the Sprite's policies: `network` or `resources`."""
        if kind not in POLICY_KINDS:
            raise ValueError(f"no such policy: {kind}. The policies are network and resources")
        path = f"/v1/sprites/{name}/policy/{kind}"
        response = self._request("POST", path, json=body)
        self._answer(response, "POST", path)

    def policy(self, name: str, kind: str) -> dict[str, Any]:
        """One of the Sprite's policies, as it reads back."""
        if kind not in POLICY_KINDS:
            raise ValueError(f"no such policy: {kind}. The policies are network and resources")
        path = f"/v1/sprites/{name}/policy/{kind}"
        return self._json(self._request("GET", path), "GET", path)

    def close(self) -> None:
        """Close the HTTP connection."""
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.request(method, path, **kwargs)
        except httpx.TransportError as error:
            error_id = report(error, f"can't reach Sprites: {error}")
            raise SandboxAway(f"can't reach Sprites. Error {error_id} has the details.") from error

    def _answer(self, response: httpx.Response, method: str, path: str) -> None:
        if 200 <= response.status_code < 300:
            return
        message = response.text[:300]
        raise SandboxError(f"Sprites answered {response.status_code} to {method} {path}: {message}")

    def _json(self, response: httpx.Response, method: str, path: str) -> Any:
        self._answer(response, method, path)
        return response.json()


def _wake_told(health: dict[str, Any], failure: SandboxError | None, awake: float) -> str:
    """What the wake line says of the wait: the probe's answer, or why it got none."""
    if failure is not None:
        return f"no answer in {awake:.1f} s: {failure}. The caller's own call wakes it."
    # A sandbox may answer anything, and only the SDK's sandbox says this of its worker.
    worker = health.get("worker")
    if not isinstance(worker, bool):
        return f"awake in {awake:.1f} s, and it didn't say whether its worker ran"
    running = "worker was running" if worker else "worker started"
    return f"awake in {awake:.1f} s, {running}"


# How many owners' Sprites are set up at once.
SETUPS_AT_ONCE = 4


class SpritesExecutor:
    """One Fly Sprite per owner, with the sandbox as its service."""

    def __init__(
        self,
        api: SpritesApi,
        database: Database,
        token: str,
        *,
        prefix: str = "cliquet-",
        sdk_root: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        wheels: Wheels | None = None,
        wakes: Once | None = None,
        checks: Once | None = None,
    ) -> None:
        self._api = api
        self._database = database
        self._token = token
        self._prefix = prefix
        self._clock = clock or (lambda: datetime.now(UTC))
        self._wheels = wheels or Wheels(default_cache(), sdk=sdk_root)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._clients: dict[str, SandboxClient] = {}
        # When each owner's sandbox was last woken: one wake line, at most, per `COLD_AFTER`.
        self._wakes = wakes or Once(COLD_AFTER)
        # When each owner's Sprite was last looked for after a call that couldn't reach it.
        self._checks = checks or Once(COLD_AFTER)
        self._sdk = SdkArchive(sdk_root)
        # How many owners' Sprites are set up at once, a minute or so each: after a deploy that
        # changes the SDK's code, every owner's is.
        self._setups = threading.Semaphore(SETUPS_AT_ONCE)

    def sandbox(self, owner: str) -> SandboxClient:
        """The client for this owner's Sprite, set up if needed: anew when Sprites no longer
        has it, as after it was destroyed by hand or the token moved to another
        organization. It stays the same client, so the apps started with it follow."""
        with self._lock_for(owner):
            client = self._clients.get(owner) or self._set_up(owner)
        # Outside the lock: a probe waits for a waking Sprite, and every other caller of this
        # owner's sandbox has nothing to wait for.
        if not self._note_wake(owner, client):
            self._set_up_if_gone(owner)
        return client

    def _set_up_if_gone(self, owner: str) -> None:
        """Set the owner's Sprite up again if Sprites has no such Sprite, asked again under the
        owner's lock, so that it is set up once. Nothing for an owner with no client, such as
        one whose account was deleted."""
        name = self._name(owner)
        with self._lock_for(owner):
            if owner not in self._clients or self._sprite_state(name) is not None:
                return
            _log.warning("sandbox %s: Sprites no longer has it, setting it up again", name)
            self._database.delete_sandbox(owner)
            self._set_up(owner)

    def _check_soon(self, owner: str) -> None:
        """A call couldn't reach the owner's Sprite, or got an answer that isn't the
        sandbox's: look for it in the background, at most once per `COLD_AFTER`."""
        if not self._checks.first(owner):
            return
        thread = threading.Thread(
            target=self._look_for, args=(owner,), name=f"sprite check {owner}", daemon=True
        )
        try:
            thread.start()
        except RuntimeError as error:
            _log.warning("sandbox %s: can't be looked for now: %s", self._name(owner), error)

    def _look_for(self, owner: str) -> None:
        """Set the owner's Sprite up again if it is gone, logging a failure as unexpected."""
        try:
            self._set_up_if_gone(owner)
        except Exception:
            _log.exception("unexpected: sandbox %s wasn't set up again", self._name(owner))

    def _set_up(self, owner: str) -> SandboxClient:
        """The owner's client, from its Sprite set up for this SDK or set up anew. Expects the
        owner's lock, so that one Sprite is set up once."""
        archive, wanted = self._sdk.get()
        info = self._database.sandbox(owner)
        name = self._name(owner)
        if (
            info is not None
            and info.ready_at is not None
            and info.sdk == wanted
            and info.name == name
        ):
            client = self._client(info.url, info.token, owner)
            self._clients[owner] = client
            return client
        with self._setups:
            # A Sprite is set up only while new: app code may have changed anything on an old
            # one, so it never gets the setup hosts back.
            started = time.monotonic()

            def step(what: str) -> None:
                _log.info("sandbox %s: %s after %.1f s", name, what, time.monotonic() - started)

            if info is not None and info.name != name:
                # Its Sprite was named with another prefix: remove it, or it would keep
                # running for nothing.
                self._api.destroy(info.name)
            self._api.destroy(name)
            sprite = self._api.create(name)
            step("made")
            url = sprite["url"]
            sandbox_token = info.token if info is not None else secrets.token_hex(24)
            self._set_policies(name, {"network": SETUP_POLICY, "resources": RESOURCES_POLICY})
            self._wait_for_exec(name)
            step("policies set")
            self._write_files(name, archive, sandbox_token)
            setup = self._api.exec(name, ["bash", "-lc", SETUP])
            step("set up")
            if setup.code != 0:
                raise SandboxError(
                    f"setting up the sandbox failed: {(setup.stdout + setup.stderr)[-2000:]}"
                )
            created = self._api.exec(
                name,
                [
                    "sprite-env",
                    "services",
                    "create",
                    "cliquet-sandbox",
                    "--cmd",
                    "/bin/bash",
                    "--args",
                    "-l,/home/sprite/start-sandbox.sh",
                    "--http-port",
                    "8080",
                    "--no-stream",
                ],
            )
            if created.code != 0:
                raise SandboxError(
                    f"creating the sandbox service failed: "
                    f"{(created.stdout + created.stderr)[-2000:]}"
                )
            step("service created")
            client = self._client(url, sandbox_token)
            wait_until_healthy(client)
            step("sandbox answered")
            # No version is put before the network is down to the build hosts.
            self._set_policies(name, {"network": BUILD_POLICY})
            step("network cut")
            # The account may have been deleted during the setup: what it made goes too.
            made = SandboxInfo(owner, name, url, sandbox_token, wanted, self._clock())
            save_or_destroy(self._database, made, client, lambda: self._api.destroy(name))
            client.close()
            kept = self._clients.get(owner)
            if kept is None:
                self._clients[owner] = kept = self._client(url, sandbox_token, owner)
            else:
                # The apps started with it speak to the new Sprite from now on.
                kept.point_at(url, sandbox_token)
            # A Sprite just set up was just used: its next call logs no wake line.
            self._wakes.first(owner)
            return kept

    def sweep(self, owners: set[str]) -> None:
        """Nothing: a Sprite is set up again by name, and Fly keeps no other record."""

    def destroy(self, owner: str) -> None:
        """Destroy the owner's Sprite, and the one its record names with another prefix, and
        forget its client."""
        name = self._name(owned(owner))
        with self._lock_for(owner):
            client = self._clients.pop(owner, None)
            if client is not None:
                client.close()
            info = self._database.sandbox(owner)
            if info is not None and info.name != name:
                self._api.destroy(info.name)
            self._api.destroy(name)

    def close(self) -> None:
        """Close the clients and the API. Never destroys a Sprite."""
        with self._locks_guard:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.close()
        self._api.close()

    def _note_wake(self, owner: str, client: SandboxClient) -> bool:
        """Log one line when this owner's sandbox may have woken: its state, and the wait.
        False, with no line, when Sprites says it has no such Sprite."""
        if not self._wakes.first(owner):
            return True
        began = time.monotonic()
        name = self._name(owner)
        state = self._sprite_state(name)
        if state is None:
            return False
        try:
            health = client.health(timeout=WAKE_TIMEOUT)
            failure = None
        except SandboxError as error:
            # The caller's own call wakes the Sprite: a slow probe costs it the wait, no more.
            health, failure = {}, error
        if not isinstance(health, dict):
            # A sandbox may answer anything, and an answer that isn't one says nothing of it.
            health = {}
        _log.info(
            "sandbox %s: %s, %s", name, state, _wake_told(health, failure, time.monotonic() - began)
        )
        return True

    def _name(self, owner: str) -> str:
        """The name of this owner's Sprite."""
        return f"{self._prefix}{owner}"

    def _sprite_state(self, name: str) -> str | None:
        """The Sprite's state as Sprites says it, before a call, or None when Sprites says it
        has no such Sprite: a reading that never fails."""
        try:
            sprite = self._api.get(name, timeout=STATE_TIMEOUT)
        except SandboxError, ValueError:
            # Sprites may be away, or answer a page that isn't JSON: that says nothing of it.
            return "state unknown"
        if sprite is None:
            return None
        state = sprite.get("status") if isinstance(sprite, dict) else None
        return state if isinstance(state, str) else "state unknown"

    def _set_policies(self, name: str, policies: dict[str, dict[str, Any]]) -> None:
        """Set each policy, and check it reads back as set. A Sprite is never left open."""
        for kind, body in policies.items():
            self._api.set_policy(name, kind, body)
        for kind, body in policies.items():
            found = self._api.policy(name, kind)
            if found != body:
                raise SandboxError(
                    f"the Sprite's {kind} policy reads back as {str(found)[:300]}, "
                    "not as it was set. The sandbox was not set up: try again later."
                )

    def _wait_for_exec(self, name: str, seconds: float = 60) -> None:
        """Wait until the Sprite runs a command: a new policy may restart its container."""
        deadline = time.monotonic() + seconds
        last = "it didn't answer"
        while time.monotonic() < deadline:
            try:
                if self._api.exec(name, ["true"], timeout=30).code == 0:
                    return
            except SandboxError as error:
                last = str(error)
            time.sleep(0.5)
        raise SandboxError(f"the Sprite didn't come back after its policies were set: {last}")

    def _client(self, url: str, token: str, owner: str | None = None) -> SandboxClient:
        """A client of the Sprite at `url`: the owner's, which looks for its Sprite when a call
        can't reach it, or one for a setup alone."""
        # Safe only while the URL keeps `sprite` auth: the Sprites proxy checks this token and
        # removes it before the request reaches the container, as the real-Sprite test checks.
        # Under `public` auth the header would pass through, and app code could read a token
        # that works on every owner's Sprite.
        return SandboxClient(
            url,
            token,
            headers={"Authorization": f"Bearer {self._token}"},
            wheels=self._wheels,
            lost=None if owner is None else (lambda: self._check_soon(owner)),
        )

    def _write_files(self, name: str, archive: bytes, sandbox_token: str) -> None:
        self._api.write(name, "/home/sprite/cliquet-sdk.tar.gz", archive)
        self._api.write(name, "/home/sprite/sandbox-token", sandbox_token.encode())
        self._api.write(name, "/home/sprite/start-sandbox.sh", START_SANDBOX.encode())

    def _lock_for(self, owner: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(owner, threading.Lock())
