"""The server's way to a sandbox: a client for it, and a local sandbox to talk to."""

import base64
import contextlib
import gzip
import io
import json as json_module
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

import cliquet
from cliquet.errors import SafeError, report
from cliquet.files import LOCK, declared_problem
from cliquet.limits import MAX_ANSWER, TEST_OUTPUT
from cliquet.scaffold import local_sdk, sdk_code_hash, sdk_files
from cliquet.screenshot import Theme
from cliquet.wheels import Locked, WheelError, Wheels, WheelUnavailable, default_cache
from cliquet_server.db import LOGIN, AccountDeleted, Database, SandboxInfo
from cliquet_server.settings import Settings

_BUILD_TIMEOUT = 600
# How many versions the server locks at once, with uv: the part of a build that runs on the
# server's own machine, for every caller. A build waiting for a sandbox takes no turn.
_LOCKS_AT_ONCE = threading.Semaphore(4)
_RUN_TIMEOUT = 40
_TEST_TIMEOUT = 330
_PICTURE_TIMEOUT = 200
_SCRIPT_TIMEOUT = 140
_TIMEOUT = 30

# What the server takes from a sandbox at most, besides a whole answer (`MAX_ANSWER`) and
# test output (`TEST_OUTPUT`): a lock file, pictures, and error messages.
MAX_LOCK = 1024 * 1024
MAX_PICTURES = 50
MAX_PICTURE = 8 * 1024 * 1024
MAX_ERROR = 2_000
# The most calls a script's answer may list: the builder makes its actions again. A step
# may make several, as a click that records a sale and then refreshes three lists.
MAX_SCRIPT_CALLS = 1000

# The variables of the server's environment the local sandbox gets, besides its token: what it
# and uv need to find the SDK, a browser, uv's cache and Python, and to reach the package index
# through a proxy. Nothing else, so no secret of the server reaches app code. The sandbox finds the
# browser with CLIQUET_CHROME and PLAYWRIGHT_BROWSERS_PATH and hands its path to the preview, as
# app code gets none of the sandbox's variables.
_SANDBOX_VARIABLES = (
    "PATH",
    "HOME",
    "CLIQUET_SDK_PATH",
    "CLIQUET_CHROME",
    "CLIQUET_CHROME_NO_SANDBOX",
    "PLAYWRIGHT_BROWSERS_PATH",
    "UV_CACHE_DIR",
    "UV_PYTHON",
    "UV_PYTHON_INSTALL_DIR",
    "UV_PYTHON_BIN_DIR",
    "UV_PYTHON_PREFERENCE",
    "UV_PYTHON_DOWNLOADS",
    "UV_OFFLINE",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "UV_NATIVE_TLS",
    "UV_SYSTEM_CERTS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


class SandboxError(Exception):
    """The sandbox failed, or could not be reached.

    Its text may quote what the sandbox answered or printed, which can come from the app's
    code: only the app's owner reads it. `SandboxFault` is the exception.

    `error_id` is the id an app's failed start is kept under for its owner, once it is.
    """

    error_id: str | None = None


class SandboxFault(SandboxError, SafeError):
    """The sandbox failed, said in Cliquet's own fixed words, such as "the sandbox can't be
    reached": every caller reads it."""


class SandboxAway(SandboxFault):
    """The sandbox can't be reached, or took too long: a call made again may work."""

    passes = True


class CaptureFailed(SandboxError):
    """A view was laid out, but the browser couldn't take its picture."""


class UnknownVersion(SandboxError):
    """The sandbox doesn't have this version. Put it again."""


class MissingWheels(SandboxError):
    """The sandbox lacks some of the wheels a version needs. Send them, then put it again."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        super().__init__(f"the sandbox lacks {len(names)} wheels")


class BuildProblems(SandboxError):
    """The version can't be built. `problems` says why, one sentence each.

    They may quote the app's code, as an error it raised on import: only the owner reads them.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("\n".join(problems))


@dataclass(frozen=True)
class ExecResult:
    """What a command run in a sandbox printed, and the code it exited with."""

    code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class Built:
    """A version the sandbox has built: its manifest and its lock file."""

    manifest: dict[str, Any]
    lock: bytes


@dataclass(frozen=True)
class SuiteRun:
    """What running an app's tests gave."""

    passed: bool
    output: str


@dataclass(frozen=True)
class Picture:
    """One picture of a view, taken at one width, and how tall the view's screen is, as the
    sandbox says, when it says."""

    view: str
    width: int
    png: bytes
    height: int | None = None

    @property
    def shown(self) -> int:
        """How tall the picture is, in pixels: 0 if it isn't a PNG."""
        return png_height(self.png)


@dataclass(frozen=True)
class Scripted:
    """What a script on a screen gave: a picture, the calls the screen made, and any error.

    Each call has its `tool`, its `arguments`, its `error` (None when it was done) and its
    `result`.
    """

    png: bytes
    calls: list[dict[str, Any]]
    error: str | None
    # How tall the view's screen is, as the sandbox says, when it says.
    height: int | None = None

    @property
    def shown(self) -> int:
        """How tall the picture is, in pixels: 0 if it isn't a PNG."""
        return png_height(self.png)


def png_height(png: bytes) -> int:
    """A PNG's height in pixels, from its header: 0 if it isn't a PNG."""
    if png[:8] != b"\x89PNG\r\n\x1a\n" or len(png) < 24:
        return 0
    return int.from_bytes(png[20:24])


class SandboxClient:
    """Speaks a sandbox's HTTP API, over a URL or a unix socket."""

    def __init__(
        self,
        url: str,
        token: str | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        wheels: Wheels | None = None,
        socket: str | None = None,
        on_call: Callable[[], None] | None = None,
        on_done: Callable[[], None] | None = None,
        wake: Callable[[], None] | None = None,
        lost: Callable[[], None] | None = None,
    ) -> None:
        sent = dict(headers or {})
        if token:
            sent["X-Sandbox-Token"] = token
        if socket is not None:
            # A container over a unix socket: the sandbox is at no address at all.
            url = "http://sandbox"
            transport = transport or httpx.HTTPTransport(uds=socket)
        self._client = httpx.Client(base_url=url, headers=sent, transport=transport)
        self._transport = transport
        self._socket = socket
        self._wheels = wheels
        self._platform: tuple[str, str] | None = None
        # A sandbox someone else may stop between calls: the caller says each one happened, and
        # asks for a wake when the sandbox stops answering.
        self._on_call = on_call
        self._on_done = on_done
        self._wake = wake
        # Told when a call can't reach the sandbox, or gets an answer that isn't the sandbox's,
        # so that whoever made it can look for it: it may be gone. It must return at once.
        self._lost = lost

    def health(self, timeout: float = _TIMEOUT) -> dict[str, Any]:
        """The sandbox's health answer, waiting `timeout` seconds for it."""
        return self._call("GET", "/health", timeout=timeout)

    def put_version(self, login: str, app: str, version: str, files: dict[str, bytes]) -> Built:
        """Build a version, or answer from its last build.

        With wheels, the server locks the version and sends the sandbox the wheels it lacks, and
        the lock is the server's. Without, the sandbox builds it as it can.
        """
        path = f"/apps/{login}/{app}/versions/{version}"
        if self._wheels is None:
            answer = self._call("PUT", path, json={"files": encoded(files)}, timeout=_BUILD_TIMEOUT)
            return _built(answer, None)
        locked = self._locked(files)
        body = {
            "files": encoded({**files, LOCK: locked.lock}),
            "wheels": [{"name": wheel.name, "sha256": wheel.sha256} for wheel in locked.wheels],
        }
        try:
            answer = self._call("PUT", path, json=body, timeout=_BUILD_TIMEOUT)
        except MissingWheels as missing:
            self._send_wheels(locked, missing.names)
            answer = self._call("PUT", path, json=body, timeout=_BUILD_TIMEOUT)
        return _built(answer, locked.lock)

    def _locked(self, files: dict[str, bytes]) -> Locked:
        """The version's lock and wheels, for the sandbox's own platform."""
        assert self._wheels is not None
        if self._platform is None:
            health = self.health()
            python, platform = health.get("python"), health.get("platform")
            if not isinstance(python, str) or not isinstance(platform, str):
                raise SandboxFault(
                    "the sandbox doesn't say its platform: it is too old for this server"
                )
            self._platform = (python, platform)
        try:
            with _LOCKS_AT_ONCE:
                return self._wheels.lock(files, *self._platform)
        except WheelError as error:
            raise BuildProblems([str(error)]) from error

    def _send_wheels(self, locked: Locked, names: list[str]) -> None:
        assert self._wheels is not None
        by_name = {wheel.name: wheel for wheel in locked.wheels}
        for name in names:
            wheel = by_name.get(name)
            if wheel is None:
                raise SandboxError(
                    f"the sandbox asked for {name!r}, which the version doesn't need"
                )
            try:
                content = self._wheels.path(wheel).read_bytes()
            except WheelUnavailable as error:
                # PyPI didn't answer: not something the app's author can fix.
                raise SandboxError(f"{error}. Try again in a moment.") from error
            except WheelError as error:
                raise BuildProblems([str(error)]) from error
            self._call(
                "PUT",
                f"/wheels/{name}",
                params={"sha256": wheel.sha256},
                content=content,
                timeout=_BUILD_TIMEOUT,
            )

    def run(self, login: str, app: str, version: str, request: dict[str, Any]) -> dict[str, Any]:
        """Send one worker request to a built version, and return the worker's answer."""
        return self._call(
            "POST",
            f"/apps/{login}/{app}/versions/{version}/run",
            json=request,
            timeout=_RUN_TIMEOUT,
        )

    def test(self, login: str, app: str, version: str, only: str | None = None) -> SuiteRun:
        """Run a built version's tests in the sandbox."""
        answer = self._call(
            "POST",
            f"/apps/{login}/{app}/versions/{version}/test",
            json={"only": only},
            timeout=_TEST_TIMEOUT,
        )
        return SuiteRun(bool(answer["passed"]), str(answer["output"])[-TEST_OUTPUT:])

    def pictures(
        self,
        login: str,
        app: str,
        version: str,
        role: str | None,
        files: dict[str, bytes],
        views: list[str] | None = None,
        theme: Theme = "light",
    ) -> list[Picture]:
        """Ask the sandbox for pictures of a built version's views, drawn in `theme`."""
        answer = self._call(
            "POST",
            f"/apps/{login}/{app}/versions/{version}/pictures",
            json={"role": role, "files": encoded(files), "views": views, "theme": theme},
            timeout=_PICTURE_TIMEOUT,
        )
        pictures = answer["pictures"]
        if len(pictures) > MAX_PICTURES:
            raise SandboxFault(f"the sandbox answered more than {MAX_PICTURES} pictures")
        return [
            Picture(
                view=str(picture["view"]),
                width=int(picture["width"]),
                png=decoded(picture["png"], MAX_PICTURE, "a picture"),
                height=_height(picture.get("height")),
            )
            for picture in pictures
        ]

    def script(
        self,
        login: str,
        app: str,
        version: str,
        role: str | None,
        files: dict[str, bytes],
        view: str,
        steps: list[dict[str, Any]],
        width: int = 768,
        theme: Theme = "light",
    ) -> Scripted:
        """Run a script on a built version's view in the sandbox, drawn in `theme`."""
        answer = self._call(
            "POST",
            f"/apps/{login}/{app}/versions/{version}/script",
            json={
                "role": role,
                "files": encoded(files),
                "view": view,
                "width": width,
                "theme": theme,
                "steps": steps,
            },
            timeout=_SCRIPT_TIMEOUT,
        )
        error = answer.get("error")
        return Scripted(
            png=decoded(answer["png"], MAX_PICTURE, "a picture"),
            calls=_script_calls(answer.get("calls")),
            error=None if error is None else cut(str(error)),
            height=_height(answer.get("height")),
        )

    def delete_app(self, login: str, app: str) -> None:
        """Remove the app and its workers from the sandbox."""
        self._call("DELETE", f"/apps/{login}/{app}", timeout=_TIMEOUT)

    def close(self) -> None:
        """Close the HTTP connection."""
        self._client.close()

    def point_at(self, url: str, token: str) -> None:
        """Send every later call to the sandbox at `url`, with `token`, as when its owner's
        Sprite was set up again: whoever holds this client follows it. Only over a URL."""
        if self._socket is not None:
            raise ValueError("a client over a socket can't be pointed at another sandbox")
        headers = httpx.Headers(self._client.headers)
        headers["X-Sandbox-Token"] = token  # in place of the old one, whatever its case
        # The old client isn't closed: a call may still be under way on it, and it goes with
        # its last user.
        self._client = httpx.Client(base_url=url, headers=headers, transport=self._transport)
        self._platform = None

    def _call(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        json: Any = None,
        params: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> Any:
        if self._on_call is not None:
            self._on_call()
        try:
            delay = 0.2
            for attempt in range(3):
                try:
                    status, body = self._send(method, path, json, timeout, params, content)
                except httpx.TimeoutException:
                    raise SandboxAway(f"the sandbox took more than {timeout:g} seconds") from None
                except httpx.TransportError as error:
                    if attempt < 2 and self._wake is not None:
                        # The sandbox may have been stopped: the retry says whether it is back.
                        with contextlib.suppress(SandboxError):
                            self._wake()
                    if attempt == 2:
                        if self._lost is not None:
                            self._lost()
                        # What httpx says is outside text: the log has it, under the id.
                        error_id = report(error, f"the sandbox can't be reached: {error}")
                        raise SandboxAway(
                            f"the sandbox can't be reached. Error {error_id} has the details."
                        ) from error
                    time.sleep(delay)
                    delay = 1.0
                    continue
                try:
                    return _answer(status, body)
                except UnknownVersion, MissingWheels, BuildProblems, CaptureFailed:
                    raise  # the sandbox's own answers: it is there
                except SandboxError:
                    if self._lost is not None:
                        self._lost()
                    raise
            raise AssertionError("unreachable")
        finally:
            if self._on_done is not None:
                self._on_done()

    def _send(
        self,
        method: str,
        path: str,
        json: Any,
        timeout: float,
        params: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> tuple[int, bytes]:
        """Send one request, and read its answer up to `MAX_ANSWER` bytes."""
        headers: dict[str, str] = {}
        if json is not None:
            # The JSON goes gzipped, as the sandbox's answers come: a version's files are its bulk.
            raw = json_module.dumps(json, separators=(",", ":"), allow_nan=False).encode()
            content = gzip.compress(raw, compresslevel=6, mtime=0)
            headers = {"content-type": "application/json", "content-encoding": "gzip"}
        # One client for the whole call, though `point_at` may change it meanwhile.
        client = self._client
        request = client.build_request(
            method, path, params=params, content=content, headers=headers, timeout=timeout
        )
        response = client.send(request, stream=True)
        try:
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_ANSWER:
                    raise SandboxFault(f"the sandbox answered more than {MAX_ANSWER // 2**20} MB")
            return response.status_code, bytes(body)
        finally:
            response.close()


def wait_until_healthy(client: SandboxClient, seconds: float = 120) -> None:
    """Wait until the sandbox answers as this SDK's, for at most `seconds`."""
    deadline = time.monotonic() + seconds
    last = "it didn't answer"
    while time.monotonic() < deadline:
        try:
            health = client.health()
            if health.get("sdk") == cliquet.__version__:
                return
            last = f"the sandbox has SDK {health.get('sdk')}, not {cliquet.__version__}"
        except SandboxError as error:
            last = str(error)
        time.sleep(0.5)
    raise SandboxError(f"the sandbox didn't start: {last}")


class Executor(Protocol):
    """Where sandboxes come from."""

    def sandbox(self, owner: str) -> SandboxClient:
        """The client for this owner's sandbox, started or woken if needed."""
        ...

    def sweep(self, owners: set[str]) -> None:
        """Remove what this executor left for owners the server no longer has."""
        ...

    def destroy(self, owner: str) -> None:
        """Destroy this owner's sandbox and all it keeps, and forget its client, under the
        owner's lock: a setup that runs waits, then its own record is refused. It never sets
        a sandbox up, or wakes one, and a sandbox that isn't there is fine."""
        ...

    def close(self) -> None:
        """Stop what the executor runs."""
        ...


class LocalExecutor:
    """One sandbox for every owner, run as a child process on this machine."""

    def __init__(self, root: Path, wheels: Wheels | None = None) -> None:
        self.root = Path(root).resolve()
        self.wheels = wheels or Wheels(default_cache())
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._client: SandboxClient | None = None

    def sandbox(self, owner: str) -> SandboxClient:
        """The one client, starting the sandbox if it isn't running."""
        with self._lock:
            if (
                self._client is not None
                and self._process is not None
                and self._process.poll() is None
            ):
                return self._client
            self._stop()
            self.root.mkdir(parents=True, exist_ok=True)
            port = _free_port()
            token = secrets.token_hex(16)
            log = (self.root / "sandbox.log").open("ab")
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "cliquet.cli",
                        "sandbox",
                        "--port",
                        str(port),
                        "--root",
                        str(self.root),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=sandbox_environment(token),
                )
            finally:
                log.close()
            client = SandboxClient(f"http://127.0.0.1:{port}", token, wheels=self.wheels)
            deadline = time.monotonic() + 20
            last = "it didn't answer"
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    client.health()
                    self._process, self._client = process, client
                    return client
                except SandboxError as error:
                    last = str(error)
                time.sleep(0.1)
            _kill(process)
            client.close()
            raise SandboxError(f"the local sandbox didn't start: {self._tail() or last}")

    def sweep(self, owners: set[str]) -> None:
        """Nothing: one sandbox serves every owner."""

    def destroy(self, owner: str) -> None:
        """Remove the owner's apps from the one sandbox, which serves every owner: their
        workers stop, and their builds go. A sandbox that isn't running isn't started."""
        folder = self.root / "apps" / owned(owner)
        with self._lock:
            client, process = self._client, self._process
            if client is not None and process is not None and process.poll() is None:
                for app in sorted(folder.iterdir()) if folder.is_dir() else []:
                    # The sandbox stops their workers, then removes their folder.
                    with contextlib.suppress(SandboxError):
                        client.delete_app(owner, app.name)
            shutil.rmtree(folder, ignore_errors=True)

    def close(self) -> None:
        """Stop the sandbox and close its client. Safe to call twice."""
        with self._lock:
            self._stop()

    def _stop(self) -> None:
        if self._process is not None:
            _kill(self._process)
            self._process = None
        if self._client is not None:
            self._client.close()
            self._client = None

    def _tail(self) -> str:
        try:
            text = (self.root / "sandbox.log").read_text(errors="replace")
        except FileNotFoundError:
            return ""
        return "\n".join(text.splitlines()[-20:])


def owned(owner: str) -> str:
    """An owner's login, checked before it names a sandbox or a folder: a login is never a
    path. Raises `ValueError`."""
    if not LOGIN.fullmatch(owner):
        raise ValueError(f"{owner!r} is not a login")
    return owner


# Why a sandbox set up while its owner's account was deleted is gone.
DELETED = "the owner's account was deleted, so the sandbox set up for it was destroyed"


def save_or_destroy(
    database: Database, info: SandboxInfo, client: SandboxClient, destroy: Callable[[], None]
) -> None:
    """Keep the record of the owner's sandbox just set up or woken, or, when the account was
    deleted meanwhile, close its client, `destroy` what was set up and raise `SandboxFault`.
    Expects the owner's lock."""
    try:
        database.save_sandbox(info)
    except AccountDeleted as error:
        client.close()
        destroy()
        raise SandboxFault(DELETED) from error


def sandbox_environment(token: str) -> dict[str, str]:
    """The local sandbox's environment: its token, and only what it needs of the server's."""
    kept = {name: os.environ[name] for name in _SANDBOX_VARIABLES if name in os.environ}
    return {**kept, "CLIQUET_SANDBOX_TOKEN": token}


def make_executor(settings: Settings, database: Database) -> Executor:
    """The executor the settings ask for."""
    match settings.executor:
        case "local":
            return LocalExecutor(
                settings.data_dir / "sandbox", Wheels(settings.data_dir / "wheels")
            )
        case "sprites":
            from cliquet_server.sprites import SpritesApi, SpritesExecutor

            assert settings.sprites_token is not None
            token = settings.sprites_token.get_secret_value()
            return SpritesExecutor(
                SpritesApi(token),
                database,
                token,
                prefix=settings.sprites_prefix,
                wheels=Wheels(settings.data_dir / "wheels"),
            )
        case "docker":
            from cliquet_server.docker import DockerApi, DockerExecutor

            return DockerExecutor(
                DockerApi(settings.docker_socket),
                database,
                settings.sandbox_sockets,
                sockets_host=settings.sandbox_sockets_host,
                sockets_volume=settings.sandbox_sockets_volume,
                image=settings.sandbox_image,
                prefix=settings.sandbox_prefix,
                idle_seconds=settings.sandbox_idle_seconds,
                memory_mb=settings.sandbox_memory_mb,
                wheels=Wheels(settings.data_dir / "wheels"),
            )


def _answer(status: int, content: bytes) -> Any:
    try:
        body = json_module.loads(content)
    except ValueError:
        body = None
    if status == 200:
        return body
    if status == 404 and isinstance(body, dict) and body.get("error") == "unknown version":
        raise UnknownVersion("unknown version")
    if status == 409 and isinstance(body, dict) and isinstance(body.get("missing"), list):
        raise MissingWheels([str(name) for name in body["missing"]])
    if status == 422 and isinstance(body, dict):
        problems = body.get("problems")
        if isinstance(problems, list):
            raise BuildProblems([cut(str(problem)) for problem in problems])
        error = body.get("error")
        if isinstance(error, str) and body.get("capture_failed") is True:
            raise CaptureFailed(cut(error))
        if isinstance(error, str):
            raise BuildProblems([cut(error)])
    if status == 401:
        raise SandboxFault("the sandbox refused the token")
    text = content[:300].decode(errors="replace")
    raise SandboxError(f"the sandbox answered {status}: {text}")


def _built(answer: Any, lock: bytes | None) -> Built:
    """Check a built version's answer. `lock` is the server's own, when it locked the version."""
    manifest = answer["manifest"]
    declared = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(declared, list):
        raise BuildProblems(["the sandbox answered a manifest without a list of files"])
    problems = [_file_problem(name) for name in declared]
    problems = [problem for problem in problems if problem is not None]
    if problems:
        raise BuildProblems(problems)
    if lock is None:
        lock = decoded(answer["lock"], MAX_LOCK, "the lock file", BuildProblems)
    return Built(manifest, lock)


def _script_calls(calls: Any) -> list[dict[str, Any]]:
    """The calls a script's answer lists, each a tool's name, its arguments as an object, and
    its error or its result. At most `MAX_SCRIPT_CALLS`."""
    if calls is None:
        return []
    if not isinstance(calls, list):
        raise SandboxFault("the sandbox answered a script whose calls aren't a list")
    if len(calls) > MAX_SCRIPT_CALLS:
        raise SandboxFault(f"the sandbox answered more than {MAX_SCRIPT_CALLS} calls")
    checked = []
    for call in calls:
        if not (
            isinstance(call, dict)
            and isinstance(call.get("tool"), str)
            and isinstance(call.get("arguments"), dict)
            and isinstance(call.get("error"), str | None)
        ):
            raise SandboxFault(
                "the sandbox answered a call without a tool's name, its arguments as an "
                "object, and its error as text or null"
            )
        error = call["error"]
        checked.append(
            {
                "tool": call["tool"],
                "arguments": call["arguments"],
                "error": None if error is None else cut(error),
                "result": call.get("result"),
            }
        )
    return checked


def _file_problem(name: Any) -> str | None:
    if not isinstance(name, str):
        return f"the manifest's files must be names or patterns, not {name!r}"
    return declared_problem(name)


def cut(message: str) -> str:
    """An error message from a sandbox, such as a build's, cut to its last `MAX_ERROR`
    characters: the end of a tool's output says what failed."""
    return message[-MAX_ERROR:]


def decoded(text: Any, limit: int, what: str, error: type[Exception] = SandboxError) -> bytes:
    """Base64 from a sandbox, refused with `error` when it holds more than `limit` bytes."""
    if not isinstance(text, str) or len(text) > limit * 4 // 3 + 4:
        raise _limit_error(error, what, limit)
    content = base64.b64decode(text)
    if len(content) > limit:
        raise _limit_error(error, what, limit)
    return content


def _height(value: Any) -> int | None:
    """A screen's height from a sandbox, or None when it isn't a sensible number of pixels."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 10_000_000:
        return value
    return None


def _limit_error(error: type[Exception], what: str, limit: int) -> Exception:
    message = f"the sandbox answered more than {limit // 2**20} MB for {what}"
    return BuildProblems([message]) if error is BuildProblems else error(message)


def tar_of(files: Mapping[str, bytes]) -> bytes:
    """A tar of these files by name, with fixed metadata, as Docker's archive call takes."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in sorted(files):
            info = tarfile.TarInfo(name)
            info.size = len(files[name])
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(files[name]))
    return buffer.getvalue()


def sdk_archive(root: Path) -> bytes:
    """The SDK's source as a `.tar.gz`: what a sandbox needs to install it."""
    return gzip.compress(tar_of(sdk_files(root)), mtime=0)


class SdkArchive:
    """The SDK's archive and the hash of its code, built once: the same for every owner.

    A sandbox is set up again only when the hash changes, so a change to the SDK's other
    files, such as the renderer's, leaves every sandbox as it is.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = root
        self._built: tuple[bytes, str] | None = None
        self._guard = threading.Lock()

    def get(self) -> tuple[bytes, str]:
        """The archive of the SDK in `root`, or in this server's checkout, and its hash."""
        with self._guard:
            if self._built is None:
                root = self._root or local_sdk()
                if root is None:
                    raise SandboxFault(
                        "the server must run from a checkout of Cliquet to set up a sandbox"
                    )
                self._built = (sdk_archive(root), sdk_code_hash(root))
            return self._built


def encoded(files: dict[str, bytes]) -> dict[str, str]:
    """A map of files as base64, as a sandbox takes them."""
    return {name: base64.b64encode(content).decode() for name, content in files.items()}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _kill(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
