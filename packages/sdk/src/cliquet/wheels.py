"""Lock an app, and keep the wheels its sandbox needs.

The server runs this, never the sandbox: a sandbox has no network once it is set up. uv does the
work. It locks without building anything (`--no-build`), and `uv pip compile` picks the wheels
for the sandbox's platform, writing them as a PEP 751 `pylock.toml`. Nothing here imports the
app's code or runs a library's.
"""

import hashlib
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import tomllib
import urllib.request
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from cliquet.files import LOCK
from cliquet.manifest import SDK_DISTRIBUTION, sdk_source_problem, uv_environment
from cliquet.scaffold import ScaffoldError, local_sdk, set_sdk_source

INDEX = "https://pypi.org/simple"
"""The only package index the server's uv may use."""
WHEEL_HOST = "https://files.pythonhosted.org/"
"""The only place wheels are downloaded from."""
MAX_WHEEL = 128 << 20
"""The largest wheel taken: 128 MB."""
CACHE_LIMIT = 2 << 30
"""How much the server's cache of wheels keeps: 2 GB. The least recently used go first."""
TEST_TOOLS = ("pytest",)
"""Installed with every app, for `cliquet test`."""
_WHEEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+!-]*-[A-Za-z0-9_.+!-]+\.whl$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class WheelError(Exception):
    """An app's libraries can't be locked, or a wheel can't be had. The message says why."""


class WheelUnavailable(WheelError):
    """A wheel can't be downloaded now. Not the app's fault: try again later."""


@dataclass(frozen=True)
class Wheel:
    """One wheel a sandbox needs: its file name, where it comes from, and its hash."""

    name: str
    url: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Locked:
    """An app's lock file, and the wheels its environment needs on one platform."""

    lock: bytes
    wheels: tuple[Wheel, ...]


def wheel_name_problem(name: str) -> str | None:
    """Why `name` isn't a wheel's file name that may be kept, or None if it is."""
    if not _WHEEL_NAME.fullmatch(name) or ".." in name:
        return f"{name!r} is not the name of a wheel, such as pytest-9.2.0-py3-none-any.whl"
    return None


def sha256_problem(digest: str) -> str | None:
    """Why `digest` isn't a SHA-256 in hex, or None if it is."""
    return None if _SHA256.fullmatch(digest) else "a wheel's sha256 is 64 hex characters"


def default_cache() -> Path:
    """Where wheels are kept when no folder is given: under the system's temporary folder."""
    return Path(tempfile.gettempdir()) / "cliquet-wheels"


def sandbox_platform() -> tuple[str, str]:
    """This machine's Python, such as `3.14`, and its platform as uv's `--python-platform`."""
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    if sys.platform == "darwin":
        return python, f"{arch}-apple-darwin"
    return python, f"{arch}-manylinux_2_28"


class Wheels:
    """Locks apps, and keeps the wheels their sandboxes need in a cache folder."""

    def __init__(
        self,
        cache: Path,
        *,
        sdk: Path | None = None,
        limit: int = CACHE_LIMIT,
        remembered: int = 256,
    ) -> None:
        self.cache = Path(cache)
        self.limit = limit
        self.remembered = remembered
        self._sdk = sdk
        self._locked: OrderedDict[str, Locked] = OrderedDict()
        self._lock = threading.Lock()

    def lock(self, files: Mapping[str, bytes], python: str, platform_: str) -> Locked:
        """Lock the app in `files`, and list the wheels its environment needs on that platform.

        `files` holds `app.py`, and `app.py.lock` when the app has one: its versions are kept
        where they still fit, but the SDK's dependencies are always the SDK's own. The
        environment also gets the test tools and the SDK's build backend, which the sandbox
        needs to install the SDK from its own folder.
        """
        sdk = self._sdk or local_sdk()
        if sdk is None:
            raise WheelError("the server must run from a checkout of Cliquet to lock an app")
        if "app.py" not in files:
            raise WheelError("there is no app.py to lock")
        digest = hashlib.sha256()
        for part in (
            files["app.py"],
            files.get(LOCK, b""),
            python.encode(),
            platform_.encode(),
            str(sdk).encode(),
            (sdk / "pyproject.toml").read_bytes(),
        ):
            digest.update(len(part).to_bytes(8, "big") + part)
        key = digest.hexdigest()
        with self._lock:
            known = self._locked.get(key)
            if known is not None:
                self._locked.move_to_end(key)
                return known
        locked = self._lock_now(files, python, platform_, sdk)
        with self._lock:
            self._locked[key] = locked
            while len(self._locked) > self.remembered:
                self._locked.popitem(last=False)
        return locked

    def path(self, wheel: Wheel) -> Path:
        """The wheel's file in the cache, downloaded and checked against its hash if needed."""
        problem = wheel_name_problem(wheel.name) or sha256_problem(wheel.sha256)
        if problem is not None:
            raise WheelError(problem)
        if not wheel.url.startswith(WHEEL_HOST):
            raise WheelError(f"{wheel.name} comes from {wheel.url}, not from {WHEEL_HOST}")
        target = self.cache / wheel.sha256 / wheel.name
        if target.is_file():
            os.utime(target)
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=target.parent, suffix=".part")
        try:
            digest = hashlib.sha256()
            size = 0
            with (
                os.fdopen(handle, "wb") as out,
                urllib.request.urlopen(wheel.url, timeout=120) as answer,
            ):
                while chunk := answer.read(1 << 16):
                    size += len(chunk)
                    if size > MAX_WHEEL:
                        raise WheelError(f"{wheel.name} is larger than {MAX_WHEEL >> 20} MB")
                    digest.update(chunk)
                    out.write(chunk)
            if digest.hexdigest() != wheel.sha256:
                raise WheelError(f"{wheel.name} doesn't match its hash: it was not kept")
            os.replace(temporary, target)
        except OSError as error:
            raise WheelUnavailable(f"{wheel.name} can't be downloaded: {error}") from error
        finally:
            Path(temporary).unlink(missing_ok=True)
        self._prune(keep=target)
        return target

    def _lock_now(
        self, files: Mapping[str, bytes], python: str, platform_: str, sdk: Path
    ) -> Locked:
        with tempfile.TemporaryDirectory(prefix="cliquet-lock-") as scratch:
            folder = Path(scratch)
            try:
                text = set_sdk_source(files["app.py"].decode(), sdk)
            except (ScaffoldError, UnicodeDecodeError) as e:
                raise WheelError(str(e)) from e
            # Its other `[tool.uv]` settings and direct references are refused here too.
            problem = sdk_source_problem(text, sdk)
            if problem is not None:
                raise WheelError(problem)
            (folder / "app.py").write_text(text)
            if LOCK in files:
                (folder / LOCK).write_bytes(files[LOCK])
            # The SDK changes in place under the same path: an older uv keeps the dependencies
            # an existing lock gives it, so it is always read again.
            _uv(
                folder,
                "lock the app's libraries",
                "lock", "--no-build", "--script", "app.py", "--upgrade-package", SDK_DISTRIBUTION,
            )  # fmt: skip
            lock = (folder / LOCK).read_bytes()
            _uv(
                folder,
                "list the app's libraries",
                "export", "--script", "app.py", "--format", "requirements-txt",
                "--no-emit-package", "cliquet-sdk", "--output-file", "app.txt",
            )  # fmt: skip
            tools = [*TEST_TOOLS, *_build_backend(sdk)]
            (folder / "tools.txt").write_text("".join(f"{tool}\n" for tool in tools))
            _uv(
                folder,
                f"find wheels for {platform_}. Cliquet installs libraries from wheels only, so "
                "choose a version or a library that has one",
                "pip", "compile", "app.txt", "tools.txt", "--only-binary", ":all:",
                "--python-platform", platform_, "--python-version", python,
                "--format", "pylock.toml", "--output-file", "pylock.toml",
            )  # fmt: skip
            found = tomllib.loads((folder / "pylock.toml").read_text())
        wheels = []
        for package in found.get("packages", []):
            listed = package.get("wheels") or []
            if not listed:
                raise WheelError(
                    f"{package.get('name')} {package.get('version')} has no wheel for "
                    f"{platform_}. Cliquet installs libraries from wheels only."
                )
            first = listed[0]
            url = str(first["url"])
            wheel = Wheel(
                str(first.get("name") or url.rsplit("/", 1)[-1]),
                url,
                str(first["hashes"]["sha256"]),
                int(first.get("size") or 0),
            )
            problem = wheel_name_problem(wheel.name) or sha256_problem(wheel.sha256)
            if problem is not None or not url.startswith(WHEEL_HOST):
                raise WheelError(f"{package.get('name')}: {problem or 'not from PyPI'}")
            wheels.append(wheel)
        return Locked(lock, tuple(wheels))

    def _prune(self, keep: Path) -> None:
        """Remove the least recently used wheels until the cache is under its limit."""
        found = []
        for path in self.cache.glob("*/*.whl"):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            found.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _, size, _ in found)
        for _, size, path in sorted(found):
            if total <= self.limit:
                break
            if path == keep:
                continue
            path.unlink(missing_ok=True)
            with suppress(OSError):
                path.parent.rmdir()  # once its folder is empty
            total -= size


_INDEX_VARIABLES = ("UV_INDEX", "UV_EXTRA_INDEX", "UV_DEFAULT_INDEX", "UV_FIND_LINKS")


def _uv(folder: Path, what: str, *arguments: str) -> None:
    # Neither the server's uv configuration nor another index: PyPI only, whatever is set.
    env = {k: v for k, v in uv_environment().items() if not k.startswith(_INDEX_VARIABLES)}
    env |= {"UV_DEFAULT_INDEX": INDEX}
    done = subprocess.run(
        ["uv", *arguments, "--quiet"], cwd=folder, capture_output=True, text=True, env=env
    )
    if done.returncode != 0:
        raise WheelError(f"uv can't {what}: {done.stderr.strip()[-2000:]}")


def _build_backend(sdk: Path) -> list[str]:
    """What the SDK's `pyproject.toml` needs to be installed from its folder, such as uv_build."""
    pyproject = tomllib.loads((sdk / "pyproject.toml").read_text())
    requires = pyproject.get("build-system", {}).get("requires", [])
    return [str(requirement) for requirement in requires]
