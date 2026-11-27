"""Screenshots of the preview, taken with the Chrome already on the machine.

`cliquet preview --screenshot` lets an agent without a browser look at a view. It drives a
headless Chrome over the DevTools protocol, through a pipe, so the SDK needs no browser
library. Chrome's own `--screenshot` flag takes its picture before the view is drawn.
"""

import base64
import contextlib
import fcntl
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

from cliquet.store import StoreError

# Set it to a Chrome, Chromium or Edge binary when none is found.
BROWSER_VARIABLE = "CLIQUET_CHROME"
# Set it to 1 to run Chrome without its own sandbox, as a container without one needs: Docker's
# default seccomp profile stops Chromium's sandbox from starting.
NO_SANDBOX_VARIABLE = "CLIQUET_CHROME_NO_SANDBOX"
_TRUTHY = {"1", "true", "yes", "on"}
_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge")
_MAC_APPS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)
# What Playwright calls the Chromium it downloads, across its versions.
_PLAYWRIGHT_NAMES = frozenset(
    {"chrome", "Chromium", "Google Chrome for Testing", "chrome-headless-shell", "headless_shell"}
)
# How to get a browser where there is none, without adding anything to Cliquet or the app.
_GET_ONE = "uvx playwright install --with-deps chromium"
# Put the two pipes on fds 3 and 4, then become the browser.
_BECOME = (
    "import os, sys; r, w = int(sys.argv[1]), int(sys.argv[2]); os.dup2(r, 3); os.dup2(w, 4); "
    "os.close(r); os.close(w); os.execv(sys.argv[3], sys.argv[3:])"
)
# A view is drawn once its size has stopped changing for this long. Charts don't animate, but
# fonts and the frame's own height settle after the first drawing.
_QUIET_MS = 600
_TIMEOUT = 40.0
# How long close() keeps trying to remove a profile that Chrome's helpers still write to.
_CLEANUP_SECONDS = 5.0
# What the preview page knows about the view it shows. The height is the screen's own,
# not the window's, so a short screen gives a short picture.
# Until the new page is there, it reads as not drawn: `Page.navigate` answers before the old
# page is gone, and the old one, marked with `cliquetLeaving`, may hold a drawn view.
_STATE = (
    "(() => { const stage = document.getElementById('stage');"
    " if (window.cliquetLeaving || !stage) return JSON.stringify({drawn: 0});"
    " return JSON.stringify({drawn: window.cliquetDrawn || 0, now: Date.now(),"
    " error: window.cliquetError || '',"
    " height: Math.ceil(stage.getBoundingClientRect().bottom) + 12}); })()"
)
_HEIGHT = "Math.ceil(document.getElementById('stage').getBoundingClientRect().bottom) + 12"
# The tallest picture, in pixels: its top part of a longer screen. A model shrinks a taller
# picture beyond reading, and Chrome can fail to capture one many thousand pixels tall.
MAX_HEIGHT = 1600
# The themes a screen is drawn in, light by default: the host page passes its own on to the
# screen, which sets daisyUI's. Emulating the system's dark mode wouldn't reach the screen.
type Theme = Literal["light", "dark"]
THEMES: tuple[Theme, ...] = ("light", "dark")


def find_browser() -> str | None:
    """The path of a browser that speaks the DevTools protocol, or None if there is none."""
    chosen = os.environ.get(BROWSER_VARIABLE)
    if chosen:
        if not Path(chosen).is_file():
            raise StoreError(f"{BROWSER_VARIABLE} is set to {chosen}, which doesn't exist")
        return chosen
    for name in _NAMES:
        found = shutil.which(name)
        if found:
            return found
    installed = next((path for path in _MAC_APPS if Path(path).is_file()), None)
    return installed or _playwright_browser()


def _playwright_browser() -> str | None:
    """A Chromium downloaded by `playwright install chromium`, the newest one."""
    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if custom and custom != "0":
        cache = Path(custom)
    elif sys.platform == "darwin":
        cache = Path.home() / "Library/Caches/ms-playwright"
    else:
        cache = Path.home() / ".cache/ms-playwright"

    def revision(folder: Path) -> int:
        number = folder.name.rpartition("-")[2]
        return int(number) if number.isdigit() else 0

    for folder in sorted(cache.glob("chromium*"), key=revision, reverse=True):
        for file in sorted(folder.rglob("*")):
            if file.name in _PLAYWRIGHT_NAMES and file.is_file() and os.access(file, os.X_OK):
                return str(file)
    return None


def no_sandbox_asked() -> bool:
    """Whether Chrome must run without its own sandbox: as root, or when the setting asks."""
    asked = os.environ.get(NO_SANDBOX_VARIABLE, "").strip().lower()
    return os.geteuid() == 0 or asked in _TRUTHY


class Browser:
    """A headless Chrome, driven through the DevTools protocol's pipe."""

    def __init__(self, path: str, no_sandbox: bool = False) -> None:
        if sys.platform == "win32":
            raise StoreError("screenshots aren't supported on Windows: open the address instead")
        self._path = path
        self._profile = tempfile.TemporaryDirectory(prefix="cliquet-chrome-")
        low_read, self._commands = os.pipe()
        self._events, low_write = os.pipe()
        # Chrome reads commands on fd 3 and writes on fd 4. A small Python process puts our
        # pipes there, then becomes Chrome. A shell can't: dash only knows fds 0 to 9.
        # Higher numbers first, so that neither copy overwrites the other's source.
        commands_read = fcntl.fcntl(low_read, fcntl.F_DUPFD, 10)
        events_write = fcntl.fcntl(low_write, fcntl.F_DUPFD, 10)
        # Keep no copy of Chrome's ends here, or its exit would go unnoticed.
        os.close(low_read)
        os.close(low_write)
        arguments = [
            "--headless",
            "--remote-debugging-pipe",
            f"--user-data-dir={self._profile.name}",
            "--no-first-run",
            "--no-default-browser-check",
            "--hide-scrollbars",
            "--disable-gpu",
            # A picture needs no network: no updates, no safe browsing, no other background calls.
            "--disable-background-networking",
            "--disable-component-update",
        ]
        if sys.platform == "linux":
            arguments.append("--disable-dev-shm-usage")  # containers have a small /dev/shm
        if no_sandbox or no_sandbox_asked():
            # Chrome refuses to start as root, and in a container without its own sandbox.
            arguments.append("--no-sandbox")
        arguments.append("about:blank")
        self._errors = Path(self._profile.name) / "stderr.txt"
        try:
            pipes = [str(commands_read), str(events_write)]
            self._process = subprocess.Popen(
                [sys.executable, "-c", _BECOME, *pipes, path, *arguments],
                pass_fds=(commands_read, events_write),
                stdout=subprocess.DEVNULL,
                stderr=self._errors.open("wb"),
                start_new_session=True,  # its own group, so close() stops its helpers too
            )
        except OSError as e:
            raise StoreError(f"{path} can't be started: {e}") from e
        finally:
            os.close(commands_read)
            os.close(events_write)
        self._buffer = b""
        self._next = 0
        self._session: str | None = None
        try:
            target = self._send("Target.createTarget", {"url": "about:blank"})["targetId"]
            attached = self._send("Target.attachToTarget", {"targetId": target, "flatten": True})
            self._session = attached["sessionId"]
        except BaseException:
            with contextlib.suppress(Exception):  # the reason it failed matters more
                self.close()
            raise

    def close(self) -> None:
        """Stop the browser and its helpers, and remove its profile."""
        self._signal(signal.SIGTERM)
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._signal(signal.SIGKILL)
            self._process.wait(timeout=5)
        os.close(self._commands)
        os.close(self._events)
        # A helper can still be writing to the profile for a moment after Chrome exits.
        deadline = time.monotonic() + _CLEANUP_SECONDS
        while True:
            try:
                self._profile.cleanup()
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)

    def _signal(self, number: signal.Signals) -> None:
        """Send a signal to the browser's process group: Chrome and every helper it started."""
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self._process.pid, number)

    def _send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one command and wait for its answer. Events in between are dropped."""
        self._next += 1
        message: dict[str, Any] = {"id": self._next, "method": method, "params": params or {}}
        if self._session:
            message["sessionId"] = self._session
        try:
            os.write(self._commands, json.dumps(message).encode() + b"\0")
        except BrokenPipeError:
            raise self._no_answer() from None  # it exited before reading a command
        deadline = time.monotonic() + _TIMEOUT
        while True:
            while b"\0" in self._buffer:
                raw, self._buffer = self._buffer.split(b"\0", 1)
                answer = json.loads(raw)
                if answer.get("id") == self._next:
                    if "error" in answer:
                        raise StoreError(f"the browser refused {method}: {answer['error']}")
                    return answer.get("result", {})
            ready, _, _ = select.select([self._events], [], [], max(deadline - time.monotonic(), 0))
            chunk = os.read(self._events, 1 << 16) if ready else b""
            if not chunk:
                raise self._no_answer()
            self._buffer += chunk

    def _no_answer(self) -> StoreError:
        """The error for a program that stopped, or never started, speaking the protocol."""
        said = self._errors.read_text(errors="replace").strip().splitlines()
        why = f" It said: {said[-1]}" if said else ""
        return StoreError(
            f"{self._path} didn't answer. Screenshots need a Chrome, Chromium or Edge.{why}"
            f" On Linux, `{_GET_ONE}` also installs the libraries Chromium needs."
        )

    def press(self, key: str) -> None:
        """Press a key, such as Enter, in the element that has the focus, as a keyboard would."""
        codes = {"Enter": (13, "\r"), "Tab": (9, ""), "Escape": (27, "")}
        code, text = codes[key]
        down = {"type": "keyDown", "key": key, "code": key, "windowsVirtualKeyCode": code}
        self._send("Input.dispatchKeyEvent", {**down, "text": text} if text else down)
        self._send("Input.dispatchKeyEvent", {**down, "type": "keyUp"})

    def evaluate(self, expression: str) -> Any:
        """Run JavaScript in the page and return its value."""
        answer = self._send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in answer:
            details = answer["exceptionDetails"]
            text = (
                details.get("exception", {}).get("description")
                or details.get("text")
                or "the page raised an error"
            )
            raise StoreError(f"the page failed: {text}")
        return answer["result"].get("value")

    def open(self, url: str, width: int) -> None:
        """Open the preview's page and wait until its view is drawn."""
        self._width = width
        self.resize(900)
        self.evaluate("window.cliquetLeaving = true")
        self._send("Page.navigate", {"url": url})
        deadline = time.monotonic() + _TIMEOUT
        while True:
            state = json.loads(self.evaluate(_STATE) or "{}")
            if state.get("error"):
                raise StoreError(state["error"])
            if state.get("drawn") and state["now"] - state["drawn"] > _QUIET_MS:
                return
            if time.monotonic() > deadline:
                raise StoreError("the screen wasn't drawn in time")
            time.sleep(0.2)

    def visit(self, url: str, width: int, height: int = 900) -> None:
        """Open any page at this size, and wait until it has loaded."""
        self._width = width
        self.resize(height)
        self.evaluate("window.cliquetLeaving = true")
        self._send("Page.navigate", {"url": url})
        deadline = time.monotonic() + _TIMEOUT
        while self.evaluate("!window.cliquetLeaving && document.readyState") != "complete":
            if time.monotonic() > deadline:
                raise StoreError(f"{url} didn't load in time")
            time.sleep(0.1)

    def resize(self, height: int) -> None:
        """Set the page to a given height, keeping the width of `open` or `visit`, at one
        pixel per CSS pixel."""
        metrics = {"width": self._width, "height": height, "deviceScaleFactor": 1, "mobile": False}
        self._send("Emulation.setDeviceMetricsOverride", metrics)

    def click(self, x: float, y: float) -> None:
        """Click at a point of the page, as a mouse would."""
        self._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        for kind in ("mousePressed", "mouseReleased"):
            self._send(
                "Input.dispatchMouseEvent",
                {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1},
            )

    def type(self, text: str) -> None:
        """Type text into the element that has the focus."""
        self._send("Input.insertText", {"text": text})

    def capture(self) -> bytes:
        """A PNG of the page as it is."""
        shot = self._send("Page.captureScreenshot", {"format": "png"})
        return base64.b64decode(shot["data"])

    def fit(self, most: int | None = None) -> int:
        """Make the page as tall as the screen it shows, up to `most` pixels, wait until that
        height holds, and return the screen's whole height: a taller page can make the screen
        lay out again, as its frame reports a new size."""
        deadline = time.monotonic() + 5
        height = int(self.evaluate(_HEIGHT))
        steady = 0
        size = None
        while time.monotonic() < deadline and steady < 3:
            wanted = height if most is None else min(height, most)
            if wanted != size:  # the same size again would only lay the page out again
                self.resize(wanted)
                size = wanted
            self.evaluate("new Promise(r => requestAnimationFrame(() => setTimeout(r, 100)))")
            now = int(self.evaluate(_HEIGHT))
            steady = steady + 1 if now == height else 0
            height = now
        return height


def browser_or_none_found(browser_path: str | None = None) -> str:
    """`browser_path`, or the browser found on this machine: a `StoreError` says how to get
    one when there is none."""
    path = browser_path or find_browser()
    if path is None:
        raise StoreError(
            "no Chrome, Chromium or Edge was found to take screenshots with. Get one with "
            f"`{_GET_ONE}`, or set {BROWSER_VARIABLE} to a browser's path."
        )
    return path


def take_screenshots(
    base_url: str,
    views: list[str],
    folder: Path,
    widths: tuple[int, ...] = (768, 390),
    browser_path: str | None = None,
    no_sandbox: bool = False,
    theme: Theme = "light",
) -> list[tuple[Path, int]]:
    """Save a PNG of each view at each width, as `<view>-<width>.png` in `folder`, and return
    each picture's path and how tall its screen is.

    Each is drawn at one pixel per CSS pixel, as a script on a view is, and shows at most the
    top `MAX_HEIGHT` pixels of its screen. `pictures.json` in `folder` lists them, with
    `failed` set to the view that couldn't be captured, if one couldn't. All are drawn in
    `theme`. `browser_path` is the browser's path, found on this machine when it is None, and
    `no_sandbox` runs it without Chrome's own sandbox.
    """
    path = browser_or_none_found(browser_path)
    folder.mkdir(parents=True, exist_ok=True)
    browser = Browser(path, no_sandbox)
    saved: list[tuple[Path, int]] = []
    listed: dict[str, Any] = {"pictures": [], "failed": None}
    try:
        for view in views:
            for width in widths:
                url = f"{base_url}/?view={view}&width={width}&theme={theme}&shot"
                try:
                    browser.open(url, width)
                    height = browser.fit(MAX_HEIGHT)
                except StoreError as e:
                    raise StoreError(f"{view}, {width} pixels wide: {e}") from e
                try:
                    png = browser.capture()
                except StoreError as e:
                    listed["failed"] = {"view": view, "width": width, "height": height}
                    raise StoreError(
                        f"{view}, {width} pixels wide, on a screen {height} pixels tall, "
                        f"couldn't be captured: {e}"
                    ) from e
                target = folder / f"{view}-{width}.png"
                target.write_bytes(png)
                saved.append((target, height))
                listed["pictures"].append(
                    {"file": target.name, "view": view, "width": width, "height": height}
                )
    finally:
        (folder / "pictures.json").write_text(json.dumps(listed))
        browser.close()
    return saved
