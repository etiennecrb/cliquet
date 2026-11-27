"""The sandbox: keeps versions of apps and runs their workers.

The server talks to this service over HTTP. It keeps app code and built environments under
one root, never app data: every call brings the files it needs. Each version gets one warm
worker process, started when first used.
"""

import base64
import binascii
import hashlib
import hmac
import itertools
import json
import math
import os
import queue
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import anyio
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

import cliquet
from cliquet import scaffold
from cliquet.cli import app_python
from cliquet.files import LOCK, MIGRATION, VERSION_KINDS, data_name_problem, kind_of, versioned
from cliquet.limits import (
    MAX_ANSWER,
    MAX_BODY,
    MAX_DATA,
    MAX_FILE_ARGUMENT,
    TEST_OUTPUT,
    read_body,
    size_text,
)
from cliquet.manifest import sdk_source_problem
from cliquet.scaffold import ScaffoldError, set_sdk_source
from cliquet.screenshot import THEMES, find_browser, no_sandbox_asked
from cliquet.store import StoreError, write_atomically, write_files
from cliquet.wheels import MAX_WHEEL, sandbox_platform, sha256_problem, wheel_name_problem

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_VERSION = re.compile(r"^[0-9a-f]{16}$")
_IDS = itertools.count(1)

# The limits on each process that runs app code. Its answer's is `MAX_ANSWER`, in `cliquet.limits`.
MEMORY_LIMIT = 1 << 30
"""Address space of a worker or `cliquet test`: 1 GB, where the platform can lower one.
Chromium gets none."""
CPU_SECONDS = 60
"""CPU time of one call to a worker."""
TEST_SECONDS = 300
"""Time, and CPU time, of `cliquet test`."""
FILE_SIZE_LIMIT = 64 << 20
"""Size of one file written: 64 MB, more than any data file the call is given."""
PRINTS_KEPT = 64 << 10
"""The last 64 KB of what a worker prints are kept."""
CAN_LIMIT_MEMORY = sys.platform != "darwin"
"""Whether this platform can lower a process's memory. macOS can't: `setrlimit` raises
ValueError for RLIMIT_AS, RLIMIT_DATA and RLIMIT_RSS alike, so a worker there runs without."""
SANDBOX_PATH = "/usr/local/bin:/usr/bin:/bin"
_TOO_LARGE = b"\0 the answer was too large"
# Runs `cliquet.cli` in the app's own Python without uv: the version was built already.
_IN_APP = (
    "import os, sys; os.environ['CLIQUET_IN_APP_ENV'] = '1'; "
    "from cliquet.cli import main; sys.exit(main(sys.argv[1:]))"
)


def version_id(files: dict[str, bytes]) -> str:
    """The id of a version: a hash of its code, 16 hex characters."""
    digest = hashlib.sha256()
    for name in sorted(name for name in files if versioned(name)):
        content = files[name]
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(str(len(content)).encode())
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()[:16]


def bundle_of(folder: Path) -> dict[str, bytes]:
    """The files of the app in `folder` that make a version, by path."""
    root = Path(folder)
    # A version's files are at the top of the folder or in `tests/`: never deeper, or in data.
    found = [path for top in (root, root / "tests") if top.is_dir() for path in top.iterdir()]
    names = (path.relative_to(root).as_posix() for path in found if path.is_file())
    return {
        name: (root / name).read_bytes() for name in sorted(names) if kind_of(name) in VERSION_KINDS
    }


class WorkerPool:
    """Warm worker processes, one per version, started when first used."""

    def __init__(
        self,
        limit: int = 6,
        idle_seconds: float = 600,
        timeout: float = 30,
        *,
        cpu_seconds: int = CPU_SECONDS,
    ) -> None:
        self.limit = limit
        self.idle_seconds = idle_seconds
        self.timeout = timeout
        self.cpu_seconds = cpu_seconds
        self._workers: dict[Path, _Warm] = {}
        self._lock = threading.Lock()

    def request(
        self, folder: Path, payload: dict[str, Any], python: str | None = None
    ) -> dict[str, Any]:
        """Ask the worker of `folder` one request, starting it if needed."""
        folder = Path(folder).resolve()
        self._expire()
        for _ in range(2):
            warm = self._worker(folder, python)
            answer = self._ask(warm, payload)
            if answer is not None:
                return answer
            self._stop(warm)
        return _failed("the worker stopped")

    def running(self) -> bool:
        """Whether a live worker is held: one past its idle time counts as stopped."""
        now = time.monotonic()
        with self._lock:
            # Judged outside the lock, which a worker starting holds for seconds.
            held = list(self._workers.values())
        return any(not self._gone_unused(warm, now) and _alive(warm.process) for warm in held)

    def stop(self, folder: Path) -> None:
        """Stop the worker of `folder`, if it has one."""
        folder = Path(folder).resolve()
        with self._lock:
            warm = self._workers.pop(folder, None)
        if warm is not None:
            warm.terminate()

    def close(self) -> None:
        """Stop every worker."""
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for warm in workers:
            warm.terminate()

    # The warm workers

    def _worker(self, folder: Path, python: str | None) -> _Warm:
        retired = None
        with self._lock:
            warm = self._workers.get(folder)
            if warm is not None and not _alive(warm.process):
                del self._workers[folder]
                warm = None
            elif warm is not None and python is not None and warm.python != python:
                # The version was built again: its calls go to the new environment now.
                retired = self._workers.pop(folder)
                warm = None
            if warm is None:
                victim = None
                if len(self._workers) >= self.limit:
                    victim = min(self._workers.values(), key=lambda w: w.used)
                    del self._workers[victim.folder]
                warm = _start(folder, python or app_python(folder / "app.py"))
                self._workers[folder] = warm
                if victim is not None:
                    victim.terminate()
        if retired is not None:
            threading.Thread(target=_retire, args=(retired,), daemon=True).start()
        return warm

    def _ask(self, warm: _Warm, payload: dict[str, Any]) -> dict[str, Any] | None:
        """One request to a warm worker; None means the worker stopped."""
        with warm.lock:
            warm.used = time.monotonic()
            if not _alive(warm.process):
                return None
            request_id = next(_IDS)
            _allow_cpu(warm.process.pid, self.cpu_seconds)
            try:
                assert warm.process.stdin is not None
                line = json.dumps({**payload, "id": request_id}) + "\n"
                warm.process.stdin.write(line.encode())
                warm.process.stdin.flush()
            except BrokenPipeError, OSError:
                return None
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                try:
                    answer_line = warm.lines.get(timeout=max(remaining, 0))
                except queue.Empty:
                    self._stop(warm)
                    return _failed(f"the call took more than {self.timeout:g} seconds")
                if answer_line is _TOO_LARGE:
                    self._stop(warm)
                    if payload.get("op") == "migrate":
                        # A migration can't be split as a call can, but a version may migrate
                        # part of the data and leave the rest to the next one.
                        return _failed(
                            "the migration's answer was too large: one answer carries back at "
                            f"most {size_text(MAX_DATA)} of changed data, and an app keeps no "
                            "more. If the app holds more, migrate part of the files in this "
                            "version, and the rest in the next one"
                        )
                    message = f"the answer was too large: more than {size_text(MAX_ANSWER)}."
                    if payload.get("op") in ("call", "apply"):
                        message += (
                            " Return less or split the call, and if a data file is the cause, "
                            f"bring it under {size_text(MAX_DATA)} in one change or remove it."
                        )
                    else:
                        message += " Return less, or split the call."
                    return _failed(message)
                if answer_line is None:
                    return self._stopped(warm)
                try:
                    answer = json.loads(answer_line)
                except json.JSONDecodeError, UnicodeDecodeError:
                    return None
                if not isinstance(answer, dict):
                    continue
                if answer.get("id") == request_id:
                    answer.pop("id", None)
                    return self._explained(warm, answer)
                if answer.get("id") is None and "error" in answer:
                    # A worker that can't load its app says so before reading requests.
                    return answer

    def _stopped(self, warm: _Warm) -> dict[str, Any]:
        """What to say when a worker stopped during a call. The call isn't tried again.

        It had the request, so it may have done part of it, and it would likely stop again.
        """
        code = _wait_unreaped(warm.process, 5)
        self._stop(warm)
        prints = warm.prints.tail()
        if code == -signal.SIGXCPU:
            return _failed(
                f"the call used more than {self.cpu_seconds} seconds of CPU, and was stopped"
            )
        if code is not None and code < 0:
            name = signal.Signals(-code).name if -code in signal.valid_signals() else str(-code)
            return _failed(
                f"the worker died of {name} during the call, often because it used too much "
                f"memory: {_memory_told()}. Its last prints: {prints}"
            )
        return _failed(
            f"the worker stopped during the call, with code {code}. Its last prints: {prints}"
        )

    def _explained(self, warm: _Warm, answer: dict[str, Any]) -> dict[str, Any]:
        """Say in plain words when a call failed on one of the limits."""
        error = answer.get("error")
        if not isinstance(error, dict) or error.get("kind") != "failed":
            return answer
        message = str(error.get("message", ""))
        if error.get("type") == "MemoryError":
            # The worker may be left in a bad state: the next call starts a new one.
            self._stop(warm)
            return _failed(
                f"the call used too much memory: {_memory_told()}. Work on less data at once."
            )
        if "[Errno 27]" in message:
            return _failed(
                f"the call wrote a file larger than {FILE_SIZE_LIMIT >> 20} MB, the limit"
            )
        return answer

    def _stop(self, warm: _Warm) -> None:
        with self._lock:
            if self._workers.get(warm.folder) is warm:
                del self._workers[warm.folder]
        warm.terminate()

    def _gone_unused(self, warm: _Warm, now: float) -> bool:
        """Whether a worker has gone unused for longer than its idle time."""
        return now - warm.used > self.idle_seconds

    def _expire(self) -> None:
        now = time.monotonic()
        with self._lock:
            idle = [w for w in self._workers.values() if self._gone_unused(w, now)]
        for warm in idle:
            if warm.lock.acquire(blocking=False):
                try:
                    self._stop(warm)
                finally:
                    warm.lock.release()


class _Tail:
    """The last bytes of a stream, never more than `size`."""

    def __init__(self, size: int) -> None:
        self.size = size
        self._data = bytearray()
        self._lock = threading.Lock()

    def add(self, chunk: bytes) -> None:
        """Keep `chunk`, dropping what is older than the last `size` bytes."""
        with self._lock:
            self._data += chunk
            if len(self._data) > self.size:
                del self._data[: len(self._data) - self.size]

    def kept(self) -> bytes:
        """The bytes kept."""
        with self._lock:
            return bytes(self._data)

    def tail(self, characters: int = 2000) -> str:
        """The last characters kept, as text."""
        return self.kept().decode(errors="replace")[-characters:]


@dataclass
class _Warm:
    folder: Path
    python: str
    process: subprocess.Popen[bytes]
    lines: queue.Queue[bytes | None]
    prints: _Tail
    tmp: Path
    lock: threading.Lock
    used: float

    def terminate(self) -> None:
        """Stop the worker and every process it started, and remove its temporary folder."""
        _terminate(self.process)
        shutil.rmtree(self.tmp, ignore_errors=True)


def sandbox_environment(tmp: Path) -> dict[str, str]:
    """The whole environment of a process that runs app code: nothing of the sandbox's."""
    return {"PATH": SANDBOX_PATH, "HOME": str(Path.home()), "LANG": "C.UTF-8", "TMPDIR": str(tmp)}


def _memory_told() -> str:
    """What an owner is told of a worker's memory, which this platform may not limit."""
    if not CAN_LIMIT_MEMORY:
        return "this platform sets no memory limit for a worker"
    return f"a worker may use {MEMORY_LIMIT >> 30} GB"


def _limits(memory: int | None, cpu: int | None) -> Callable[[], None]:
    """Set the limits of a new process, in the child, before it runs anything."""

    def apply() -> None:
        if memory is not None and CAN_LIMIT_MEMORY:
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        if cpu is not None:
            # A soft limit only: the sandbox raises it before each call of a warm worker.
            _, hard = resource.getrlimit(resource.RLIMIT_CPU)
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, hard))
        resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_SIZE_LIMIT, FILE_SIZE_LIMIT))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply


def _allow_cpu(pid: int, seconds: int) -> None:
    """Let a warm worker use `seconds` more of CPU, from now. Linux only."""
    if not hasattr(resource, "prlimit"):
        return
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
        used = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
        _, hard = resource.prlimit(pid, resource.RLIMIT_CPU)
        soft = math.ceil(used) + seconds
        if hard != resource.RLIM_INFINITY:
            soft = min(soft, hard)
        resource.prlimit(pid, resource.RLIMIT_CPU, (soft, hard))
    except OSError, ValueError, IndexError:
        pass


def _scratch() -> Path:
    """A new temporary folder for one process, under the system's, so that its path stays short.

    Chromium makes a socket in it, and a socket's path must stay under about 100 characters.
    """
    return Path(tempfile.mkdtemp(prefix="cliquet-run-"))


def _start(folder: Path, python: str) -> _Warm:
    tmp = _scratch()
    process = subprocess.Popen(
        [python, "-m", "cliquet.worker", str(folder)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=sandbox_environment(tmp),
        # Without prlimit (macOS), the limit can't be raised before each call: a warm worker
        # would die once its calls add up to a minute. Its time limit is all it gets there.
        preexec_fn=_limits(MEMORY_LIMIT, CPU_SECONDS if hasattr(resource, "prlimit") else None),
        start_new_session=True,
    )
    lines: queue.Queue[bytes | None] = queue.Queue()
    prints = _Tail(PRINTS_KEPT)
    assert process.stdout is not None and process.stderr is not None
    threading.Thread(target=_read_lines, args=(process.stdout, lines), daemon=True).start()
    threading.Thread(target=_read_tail, args=(process.stderr, prints), daemon=True).start()
    return _Warm(folder, python, process, lines, prints, tmp, threading.Lock(), time.monotonic())


def _retire(warm: _Warm) -> None:
    """Stop a worker once the call it is answering, if any, is done."""
    with warm.lock:
        warm.terminate()


def _read_lines(stream: Any, lines: queue.Queue[bytes | None]) -> None:
    """Hand on each line of a worker's answers, and stop at one larger than the limit."""
    buffer = bytearray()
    while chunk := stream.read1(1 << 16):
        buffer += chunk
        while (end := buffer.find(b"\n")) >= 0:
            if end > MAX_ANSWER:
                lines.put(_TOO_LARGE)
                return
            lines.put(bytes(buffer[: end + 1]))
            del buffer[: end + 1]
        if len(buffer) > MAX_ANSWER:
            lines.put(_TOO_LARGE)
            return
    lines.put(None)


def _read_tail(stream: Any, tail: _Tail) -> None:
    while chunk := stream.read1(1 << 16):
        tail.add(chunk)


def _wait_unreaped(process: subprocess.Popen[Any], timeout: float) -> int | None:
    """Wait until the process exits, without reaping it, and return its code, or None.

    Until it is reaped, its id can't go to another process, so its group can still be killed
    safely. Where `waitid` isn't there, this reaps, as `Popen.wait` does.
    """
    if process.returncode is not None:
        return process.returncode
    if not hasattr(os, "waitid"):
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
    deadline = time.monotonic() + timeout
    while True:
        try:
            found = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return process.returncode
        if found is not None:
            killed = found.si_code in (os.CLD_KILLED, os.CLD_DUMPED)
            return -found.si_status if killed else found.si_status
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def _alive(process: subprocess.Popen[Any]) -> bool:
    """Whether the process still runs. Unlike `Popen.poll`, this doesn't reap it."""
    return _wait_unreaped(process, 0) is None


def _terminate(process: subprocess.Popen[Any]) -> None:
    """Stop a process started in its own session, and every process of its group.

    The leader is reaped last, so that its id, the group's, can't be reused before the group
    is killed.
    """
    if process.returncode is None:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGTERM)
        _wait_unreaped(process, 5)
        # The group outlives its leader while a child of it runs: kill what is left.
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    process.wait()
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            with suppress(OSError):
                stream.close()


def _run_limited(
    command: list[str],
    cwd: Path,
    timeout: float,
    memory: int | None,
    keep: int,
    merged: bool = False,
) -> tuple[int | None, bytes, bytes]:
    """Run app code to the end, in its own group, with the limits and an empty environment.

    Keep the last `keep` bytes of each output, read as it comes. None as the code means the time
    ran out, and the whole group was killed.
    """
    tmp = _scratch()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merged else subprocess.PIPE,
        env=sandbox_environment(tmp),
        preexec_fn=_limits(memory, math.ceil(timeout)),
        start_new_session=True,
    )
    out, err = _Tail(keep), _Tail(keep)
    readers = [threading.Thread(target=_read_tail, args=(process.stdout, out), daemon=True)]
    if not merged:
        readers.append(threading.Thread(target=_read_tail, args=(process.stderr, err), daemon=True))
    for reader in readers:
        reader.start()
    try:
        code = _wait_unreaped(process, timeout)
        _terminate(process)
        for reader in readers:
            reader.join(timeout=5)
        return code, out.kept(), err.kept()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# The sandbox


def create_sandbox(
    root: Path,
    token: str | None = None,
    sdk_path: Path | None = None,
    pool: WorkerPool | None = None,
) -> Starlette:
    """The sandbox as an ASGI app, keeping everything under `root`."""
    root = Path(root).resolve()
    if sdk_path is None:
        sdk_path = scaffold.sdk_folder()
    if pool is None:
        pool = WorkerPool()
    built_for = _sdk_id(sdk_path)
    _forget_builds(root, built_for)
    pictures = threading.Semaphore(2)
    locks: dict[Path, threading.Lock] = {}
    locks_lock = threading.Lock()

    def folder_of(login: str, app_name: str, version: str) -> Path:
        return root / "apps" / login / app_name / version

    def lock_for(folder: Path) -> threading.Lock:
        with locks_lock:
            return locks.setdefault(folder, threading.Lock())

    wheels = _WheelStore(root / "wheels")
    python, platform = sandbox_platform()

    async def health(request: Request) -> JSONResponse:
        answer = {
            "ok": True,
            "sdk": cliquet.__version__,
            "python": python,
            "platform": platform,
            # In a thread: the pool's lock is held across a worker's start.
            "worker": await anyio.to_thread.run_sync(pool.running),
        }
        return JSONResponse(answer)

    async def put_wheel(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        digest = request.query_params.get("sha256", "")
        problem = wheel_name_problem(name) or sha256_problem(digest)
        if problem is not None:
            return JSONResponse({"error": problem}, status_code=422)
        size = request.headers.get("content-length", "")
        if not size.isdigit() or int(size) > MAX_WHEEL:
            return JSONResponse(
                {"error": f"a wheel is at most {MAX_WHEEL >> 20} MB, with its length"},
                status_code=422,
            )
        content = await read_body(request, MAX_WHEEL)
        error = await anyio.to_thread.run_sync(partial(wheels.put, name, digest, content))
        if error is not None:
            return JSONResponse({"error": error}, status_code=422)
        return JSONResponse({})

    async def put_version(request: Request) -> JSONResponse:
        login, app_name, version = _route(request)
        if not _known(login, app_name) or not _VERSION.fullmatch(version):
            return JSONResponse({"error": "not found"}, status_code=404)
        body = await _body(request)
        if body is None:
            return JSONResponse({"error": "not JSON"}, status_code=422)
        folder = folder_of(login, app_name, version)
        answer, status = await anyio.to_thread.run_sync(
            partial(_put, sdk_path, built_for, pool, wheels, lock_for, folder, body)
        )
        return JSONResponse(answer, status_code=status)

    async def run_version(request: Request) -> JSONResponse:
        login, app_name, version = _route(request)
        if not _known(login, app_name) or not _VERSION.fullmatch(version):
            return JSONResponse({"error": "not found"}, status_code=404)
        body = await _body(request)
        if body is None:
            return JSONResponse({"error": "not JSON"}, status_code=422)
        folder = root / "apps" / login / app_name / version
        answer, status = await anyio.to_thread.run_sync(partial(_run, pool, folder, body))
        return JSONResponse(answer, status_code=status)

    async def script_version(request: Request) -> JSONResponse:
        login, app_name, version = _route(request)
        if not _known(login, app_name) or not _VERSION.fullmatch(version):
            return JSONResponse({"error": "not found"}, status_code=404)
        body = await _body(request)
        if body is None:
            return JSONResponse({"error": "not JSON"}, status_code=422)
        folder = root / "apps" / login / app_name / version
        answer, status = await anyio.to_thread.run_sync(
            partial(_script, root, pictures, folder, body)
        )
        return JSONResponse(answer, status_code=status)

    async def test_version(request: Request) -> JSONResponse:
        login, app_name, version = _route(request)
        if not _known(login, app_name) or not _VERSION.fullmatch(version):
            return JSONResponse({"error": "not found"}, status_code=404)
        body = await _body(request)
        if body is None:
            return JSONResponse({"error": "not JSON"}, status_code=422)
        folder = root / "apps" / login / app_name / version
        answer, status = await anyio.to_thread.run_sync(partial(_test, folder, body))
        return JSONResponse(answer, status_code=status)

    async def take_pictures(request: Request) -> JSONResponse:
        login, app_name, version = _route(request)
        if not _known(login, app_name) or not _VERSION.fullmatch(version):
            return JSONResponse({"error": "not found"}, status_code=404)
        body = await _body(request)
        if body is None:
            return JSONResponse({"error": "not JSON"}, status_code=422)
        folder = root / "apps" / login / app_name / version
        answer, status = await anyio.to_thread.run_sync(
            partial(_pictures, root, pictures, folder, body)
        )
        return JSONResponse(answer, status_code=status)

    async def delete_app(request: Request) -> JSONResponse:
        login, app_name, _ = _route(request)
        if not _known(login, app_name):
            return JSONResponse({"error": "not found"}, status_code=404)
        answer = await anyio.to_thread.run_sync(partial(_delete, root, pool, login, app_name))
        return JSONResponse(answer)

    routes = [
        Route("/health", health),
        Route("/wheels/{name}", put_wheel, methods=["PUT"]),
        Route("/apps/{login}/{app}/versions/{version}", put_version, methods=["PUT"]),
        Route("/apps/{login}/{app}/versions/{version}/run", run_version, methods=["POST"]),
        Route("/apps/{login}/{app}/versions/{version}/test", test_version, methods=["POST"]),
        Route("/apps/{login}/{app}/versions/{version}/pictures", take_pictures, methods=["POST"]),
        Route("/apps/{login}/{app}/versions/{version}/script", script_version, methods=["POST"]),
        Route("/apps/{login}/{app}", delete_app, methods=["DELETE"]),
    ]

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            await anyio.to_thread.run_sync(pool.close)

    app = Starlette(
        routes=routes,
        middleware=[
            *([Middleware(_Token, token=token)] if token else []),
            gzipped_answers(),
        ],
        lifespan=lifespan,
    )
    app.state.pool = pool
    return app


class _Token:
    """Refuse a request that doesn't carry the sandbox's token, on every route."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or hmac.compare_digest(
            dict(scope["headers"]).get(b"x-sandbox-token", b""), self.token
        ):
            await self.app(scope, receive, send)
            return
        response = JSONResponse({"error": "wrong or missing token"}, status_code=401)
        await response(scope, receive, send)


def _route(request: Request) -> tuple[str, str, str]:
    return (
        request.path_params["login"],
        request.path_params["app"],
        request.path_params.get("version", ""),
    )


def _known(login: str, app_name: str) -> bool:
    return bool(_NAME.fullmatch(login) and _NAME.fullmatch(app_name))


# The answers already compressed, or streamed as events, that gzip would only slow down.
NOT_GZIPPED = (
    "text/event-stream",
    "application/zip",
    "application/gzip",
    "image/png",
    "image/jpeg",
    "image/webp",
    "font/woff2",
)


def gzipped_answers() -> Middleware:
    """Compress each answer with gzip for a client that accepts it, but those `NOT_GZIPPED`."""
    return Middleware(GZipMiddleware, minimum_size=500, exclude_content_types=NOT_GZIPPED)


# A request's body: a version's files or an app's data in base64, plus a call's arguments and the
# file an action takes, in base64 too. Gzipped, it unpacks to no more.
_MAX_REQUEST = MAX_ANSWER + MAX_BODY + -(-MAX_FILE_ARGUMENT // 3) * 4


async def _body(request: Request) -> dict[str, Any] | None:
    """The request's JSON object, or None. One over `_MAX_REQUEST`, sent or unpacked, is
    refused with a 413."""
    raw = await read_body(request, _MAX_REQUEST)
    try:
        if request.headers.get("content-encoding") == "gzip":
            unpacking = zlib.decompressobj(wbits=31)
            raw = unpacking.decompress(raw, _MAX_REQUEST + 1)
            if len(raw) > _MAX_REQUEST:
                raise HTTPException(413, f"a request unpacks to at most {size_text(_MAX_REQUEST)}")
        body = json.loads(raw)
    except json.JSONDecodeError, UnicodeDecodeError, zlib.error:
        return None
    return body if isinstance(body, dict) else None


# Putting a version


class _WheelStore:
    """The wheels the server sent, by file name, each checked against its hash once."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self._checked: dict[str, str] = {}
        self._lock = threading.Lock()

    def has(self, name: str, digest: str) -> bool:
        """Whether the wheel is here, with that hash."""
        path = self.folder / name
        if not path.is_file():
            return False
        with self._lock:
            if self._checked.get(name) == digest:
                return True
        found = hashlib.sha256(path.read_bytes()).hexdigest()
        with self._lock:
            self._checked[name] = found
        return found == digest

    def put(self, name: str, digest: str, content: bytes) -> str | None:
        """Keep a wheel if it matches its hash. Returns why not, or None."""
        if hashlib.sha256(content).hexdigest() != digest:
            return f"{name} doesn't match its hash: it was not kept"
        write_atomically(self.folder / name, content)
        with self._lock:
            self._checked[name] = digest
        return None


def _wanted(body: dict[str, Any]) -> tuple[list[tuple[str, str]] | None, str | None]:
    """The wheels a version needs, as (name, sha256), or why the list is wrong."""
    listed = body.get("wheels")
    if not isinstance(listed, list):
        return None, "there is no wheels list: the server sends the wheels a version needs"
    wanted = []
    for item in listed:
        if not isinstance(item, dict):
            return None, 'each wheel is {"name": ..., "sha256": ...}'
        name, digest = str(item.get("name", "")), str(item.get("sha256", ""))
        problem = wheel_name_problem(name) or sha256_problem(digest)
        if problem is not None:
            return None, problem
        wanted.append((name, digest))
    return wanted, None


def _put(
    sdk: Path | None,
    built_for: str,
    pool: WorkerPool,
    wheels: _WheelStore,
    lock_for: Callable[[Path], threading.Lock],
    folder: Path,
    body: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    version = folder.name
    with lock_for(folder):
        files = body.get("files")
        if not isinstance(files, dict):
            return {"error": "there is no files mapping"}, 422
        bundle: dict[str, bytes] = {}
        for name, text in files.items():
            if not isinstance(name, str) or kind_of(name) not in VERSION_KINDS:
                return {"error": f"not a file of a version: {name}"}, 422
            try:
                bundle[name] = base64.b64decode(text, validate=True)
            except binascii.Error, ValueError, TypeError:
                return {"error": f"{name} is not base64"}, 422
        if "app.py" not in bundle:
            return {"error": "there is no app.py"}, 422
        found = version_id(bundle)
        if found != version:
            return {"error": f"these files are version {found}, not {version}"}, 422
        return _build(folder, sdk, built_for, pool, wheels, bundle, body)


def _build(
    folder: Path,
    sdk: Path | None,
    built_for: str,
    pool: WorkerPool,
    wheels: _WheelStore,
    bundle: dict[str, bytes],
    body: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    folder.mkdir(parents=True, exist_ok=True)
    # A worker reads migrate.py afresh for each migration, so it is no part of the build.
    _replace(folder / MIGRATION, bundle.get(MIGRATION))
    ready = folder / ".ready.json"
    wanted, problem = _wanted(body)
    built_from = None if wanted is None else _built_from(bundle, wanted)
    known = _ready(folder)
    # Without a wheels list, a version already built answers from its build, as it can.
    if known is not None and built_from in (None, known.get("built_from")):
        return {"manifest": known["manifest"], "lock": _lock_text(folder)}, 200
    if wanted is None or built_from is None:
        return {"error": problem}, 422
    # Built by an older sandbox, from another lock, or its Python is gone: build it again,
    # beside the build that serves meanwhile, which goes only once the new one works.
    if sdk is None:
        # Without a local SDK, uv would look for it on a package index.
        return {
            "error": "this sandbox has no SDK folder to install apps against: set CLIQUET_SDK_PATH"
        }, 500
    try:
        source = set_sdk_source(bundle["app.py"].decode(), sdk)
    except ScaffoldError as e:
        return {"error": str(e)}, 422
    problem = sdk_source_problem(source, sdk)
    if problem is not None:
        return {"error": problem}, 422
    # The same code as the build that serves, so these leave it as it was.
    for name, content in {**bundle, "app.py": source.encode()}.items():
        if name not in (MIGRATION, LOCK):
            _replace(folder / name, content)
    missing = [name for name, digest in wanted if not wheels.has(name, digest)]
    if missing:
        return {"missing": missing}, 409
    env = folder / f".venv-{built_from[:16]}"
    serving = None if known is None else Path(known["python"]).parents[1]
    for old in [*folder.glob(".venv-*"), folder / ".venv"]:
        if old not in (env, serving):
            shutil.rmtree(old, ignore_errors=True)
    python, problem = _environment(env, sdk, wheels.folder, [name for name, _ in wanted])
    if python is None:
        return {"error": problem}, 422
    answer = pool.request(folder, {"op": "manifest"}, python=python)
    if "error" in answer:
        error = answer["error"]
        pool.stop(folder)
        if error["kind"] == "app_error":
            problems = [f"{p['where']}: {p['message']}" for p in error["problems"]]
            return {"problems": problems}, 422
        return {"error": error["message"]}, 422
    built = {
        "manifest": answer["result"],
        "python": python,
        "sdk": built_for,
        "built_from": built_from,
    }
    # A lock left from an earlier put must not be answered as this build's.
    _replace(folder / LOCK, bundle.get(LOCK))
    write_atomically(ready, (json.dumps(built) + "\n").encode())
    return {"manifest": answer["result"], "lock": _lock_text(folder)}, 200


def _built_from(bundle: dict[str, bytes], wanted: list[tuple[str, str]]) -> str:
    """A hash of the lock and the wheels a version was put with: what its environment holds.

    The version id hashes the code only, and the server may lock the same code again.
    """
    digest = hashlib.sha256()
    lock = bundle.get(LOCK, b"")
    digest.update(f"{len(lock)}\0".encode() + lock)
    for name, sha256 in sorted(wanted):
        digest.update(f"{name}\0{sha256}\0".encode())
    return digest.hexdigest()


def _replace(path: Path, content: bytes | None) -> None:
    """Write `path` whole, so a reader never sees half of it, or remove it when None."""
    if content is None:
        path.unlink(missing_ok=True)
    elif not path.is_file() or path.read_bytes() != content:
        write_atomically(path, content)


def _environment(
    env: Path, sdk: Path, wheels: Path, names: list[str]
) -> tuple[str | None, str | None]:
    """Make a version's environment in `env` from the wheels the server sent, offline.

    Returns its Python, or why it can't be made. Nothing here reaches a package index.
    """
    shutil.rmtree(env, ignore_errors=True)
    made = subprocess.run(
        ["uv", "venv", "--quiet", "--offline", "--python", sys.executable, str(env)],
        capture_output=True,
        text=True,
    )
    if made.returncode != 0:
        return None, f"uv can't make the app's environment: {made.stderr.strip()}"
    python = env / "bin" / "python"
    command = ["uv", "pip", "install", "--quiet", "--offline", "--no-index", "--no-deps"]
    command += ["--find-links", str(wheels), "--python", str(python)]
    command += [str(wheels / name) for name in names]
    command += ["--editable", str(sdk)]
    installed = subprocess.run(command, capture_output=True, text=True)
    if installed.returncode != 0:
        return None, f"uv can't install the app's libraries: {installed.stderr.strip()}"
    return str(python), None


def _sdk_id(sdk: Path | None) -> str:
    """A hash of the SDK's code: what a version's environment and manifest were made with."""
    return "" if sdk is None else scaffold.sdk_code_hash(sdk)


def _forget_builds(root: Path, built_for: str) -> None:
    """Forget the versions built with another SDK, so the server puts them again.

    The SDK changes in place: a version built with the old one has the old SDK's
    dependencies, and the manifest the old SDK read.
    """
    for ready in (root / "apps").glob("*/*/*/.ready.json"):
        try:
            found = json.loads(ready.read_text())
        except OSError, ValueError:
            found = None
        if not isinstance(found, dict) or found.get("sdk") != built_for:
            ready.unlink(missing_ok=True)


def _ready(folder: Path) -> dict[str, Any] | None:
    """What `.ready.json` says of a built version: its manifest and Python, or None."""
    try:
        ready = json.loads((folder / ".ready.json").read_text())
    except OSError, ValueError:
        return None
    if not isinstance(ready, dict) or not isinstance(ready.get("python"), str):
        return None
    return ready if Path(ready["python"]).is_file() else None


def _lock_text(folder: Path) -> str:
    path = folder / LOCK
    return base64.b64encode(path.read_bytes()).decode() if path.is_file() else ""


def _run(pool: WorkerPool, folder: Path, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    ready = _ready(folder)
    if ready is None:
        return {"error": "unknown version"}, 404
    return pool.request(folder, body, python=ready["python"]), 200


def _test(folder: Path, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    ready = _ready(folder)
    if ready is None:
        return {"error": "unknown version"}, 404
    command = [ready["python"], "-c", _IN_APP, "test", str(folder), "-q", "--color=no"]
    only = body.get("only")
    if only is not None:
        command += ["-k", str(only)]
    # Four bytes per character at most: the last characters are all kept.
    code, output, _ = _run_limited(
        command, folder, TEST_SECONDS, MEMORY_LIMIT, TEST_OUTPUT * 4, True
    )
    if code is None:
        return {
            "passed": False,
            "output": "The tests took more than five minutes and were stopped.",
        }, 200
    text = output.decode(errors="replace")[-TEST_OUTPUT:]
    return {"passed": code == 0, "output": text}, 200


def _preview_files(files: Any) -> tuple[dict[str, bytes] | None, str | None]:
    if not isinstance(files, dict):
        return None, "there is no files mapping"
    found = {}
    for name, text in files.items():
        problem = data_name_problem(name) if isinstance(name, str) else "it isn't a string"
        if problem is not None:
            return None, f"not a data file: {problem}"
        try:
            found[name] = base64.b64decode(text, validate=True)
        except binascii.Error, ValueError, TypeError:
            return None, f"{name} is not base64"
    return found, None


def _preview_data(scratch: Path, files: dict[str, bytes]) -> tuple[Path, Path]:
    data = scratch / "data"
    out = scratch / "out"
    data.mkdir()
    out.mkdir()
    write_files(data, files)
    return data, out


def _run_preview(
    python: str, arguments: list[str], cwd: Path, timeout: int
) -> tuple[int | None, str, str]:
    # The sandbox finds the browser, with its own CLIQUET_CHROME and PLAYWRIGHT_BROWSERS_PATH:
    # the preview runs app code, which starts with nothing of the sandbox's environment.
    try:
        browser = find_browser()
    except StoreError as e:
        return 1, "", str(e)
    if browser is not None:
        arguments = [*arguments, "--browser", browser]
    if no_sandbox_asked():
        # Chrome can take no picture in a container with Docker's own seccomp profile.
        arguments = [*arguments, "--browser-no-sandbox"]
    command = [python, "-c", _IN_APP, "preview", *arguments]
    # Chromium reserves far more address space than it uses: the Sprite's limit covers it.
    code, stdout, stderr = _run_limited(command, cwd, timeout, None, PRINTS_KEPT)
    if code is None:
        return None, "", f"the preview took more than {timeout} seconds and was stopped."
    return code, stdout.decode(errors="replace"), stderr.decode(errors="replace")


# Why a picture or a script asked for a theme the host page doesn't draw.
_NOT_A_THEME = 'theme must be "light" or "dark"'


def _pictures(
    root: Path,
    semaphore: threading.Semaphore,
    folder: Path,
    body: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    ready = _ready(folder)
    if ready is None:
        return {"error": "unknown version"}, 404
    files, error = _preview_files(body.get("files"))
    if error is not None:
        return {"error": error}, 422
    assert files is not None
    role = body.get("role")
    views = body.get("views")
    if views is not None and not isinstance(views, list):
        return {"error": "views must be a list or null"}, 422
    theme = body.get("theme", "light")
    if theme not in THEMES:
        return {"error": _NOT_A_THEME}, 422
    with semaphore:
        tmp_root = root / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="pictures-", dir=tmp_root))
        try:
            data, out = _preview_data(scratch, files)
            arguments = [
                str(folder), "--data", str(data), "--screenshot", str(out), "--theme", theme,
            ]  # fmt: skip
            if role is not None:
                arguments += ["--role", str(role)]
            for view in views or []:
                arguments += ["--view", str(view)]
            code, stdout, stderr = _run_preview(ready["python"], arguments, folder, 180)
            listed = _listed_pictures(out)
            if code != 0:
                answer: dict[str, Any] = {"error": (stderr or stdout)[-2000:]}
                if listed.get("failed"):
                    answer["capture_failed"] = True  # the view drew, but its picture failed
                return answer, 422
            heights = {one.get("file"): one.get("height") for one in listed["pictures"]}
            found = []
            for line in stdout.splitlines():
                path = Path(line.strip())
                if path.suffix != ".png" or not path.is_file():
                    continue
                view, _, width = path.stem.rpartition("-")
                if not view or not width.isdigit():
                    continue
                found.append(
                    {
                        "view": view,
                        "width": int(width),
                        "height": heights.get(path.name),
                        "png": base64.b64encode(path.read_bytes()).decode(),
                    }
                )
            return {"pictures": found}, 200
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


def _listed_pictures(out: Path) -> dict[str, Any]:
    """What `pictures.json` says of the pictures the preview took, or that it took none."""
    try:
        listed = json.loads((out / "pictures.json").read_text())
    except OSError, ValueError:
        listed = None
    if not isinstance(listed, dict) or not isinstance(listed.get("pictures"), list):
        return {"pictures": [], "failed": None}
    listed["pictures"] = [one for one in listed["pictures"] if isinstance(one, dict)]
    return listed


def _script(
    root: Path,
    semaphore: threading.Semaphore,
    folder: Path,
    body: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    ready = _ready(folder)
    if ready is None:
        return {"error": "unknown version"}, 404
    files, error = _preview_files(body.get("files"))
    if error is not None:
        return {"error": error}, 422
    assert files is not None
    view = body.get("view")
    if not isinstance(view, str):
        return {"error": "there is no view"}, 422
    steps = body.get("steps")
    if not isinstance(steps, list):
        return {"error": "steps must be a list"}, 422
    width = body.get("width", 768)
    role = body.get("role")
    theme = body.get("theme", "light")
    if theme not in THEMES:
        return {"error": _NOT_A_THEME}, 422
    with semaphore:
        tmp_root = root / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="script-", dir=tmp_root))
        try:
            data, out = _preview_data(scratch, files)
            steps_path = scratch / "steps.json"
            steps_path.write_text(json.dumps({"view": view, "width": width, "steps": steps}))
            arguments = [
                str(folder), "--data", str(data), "--script", str(steps_path), "--out", str(out),
                "--theme", theme,
            ]  # fmt: skip
            if role is not None:
                arguments += ["--role", str(role)]
            code, stdout, stderr = _run_preview(ready["python"], arguments, folder, 120)
            if code != 0:
                return {"error": (stderr or stdout)[-2000:]}, 422
            screen = out / "screen.png"
            result_path = out / "result.json"
            if not screen.is_file() or not result_path.is_file():
                return {"error": "the preview wrote no result"}, 422
            result = json.loads(result_path.read_text())
            return {
                "png": base64.b64encode(screen.read_bytes()).decode(),
                "calls": result.get("calls", []),
                "error": result.get("error"),
                "height": result.get("height"),
            }, 200
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


def _delete(root: Path, pool: WorkerPool, login: str, app_name: str) -> dict[str, Any]:
    folder = root / "apps" / login / app_name
    if folder.is_dir():
        for child in list(folder.glob("*")):
            pool.stop(child)
    shutil.rmtree(folder, ignore_errors=True)
    return {}


def _failed(message: str) -> dict[str, Any]:
    return {"error": {"kind": "failed", "message": message}}
