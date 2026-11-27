import json
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from conftest import APPS, EXAMPLES
from test_runtime import make_app
from test_views import BOARD, OWNER, READER, board, broken

from cliquet import sandbox, screenshot
from cliquet.mcp_server import AppServer
from cliquet.preview import screenshot_preview
from cliquet.runtime import EVERYONE
from cliquet.scaffold import local_sdk, set_sdk_source
from cliquet.store import StoreError

pytestmark = [pytest.mark.browser, pytest.mark.slow]


needs_browser = pytest.mark.skipif(
    screenshot.find_browser() is None, reason="needs a Chrome, Chromium or Edge on this machine"
)


# A page the preview has drawn, and one that takes a while to arrive and to draw.
DRAWN = b"<div id='stage'>first</div><script>window.cliquetDrawn = Date.now() - 5000</script>"
SLOW = (
    b"<script>setTimeout(() => { document.body.innerHTML = \"<div id='stage'>second</div>\";"
    b" window.cliquetDrawn = Date.now() - 5000 }, 300)</script>"
)


class _Pages(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/slow"):
            time.sleep(1)  # a slow server, not a wait in the test
        body = SLOW if self.path.startswith("/slow") else DRAWN
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.fixture
def pages() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Pages)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def size(png: Path) -> tuple[int, int]:
    data = png.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def pixel(png: bytes, x: int, y: int) -> tuple[int, int, int]:
    """The colour of one pixel of an 8-bit RGB or RGBA PNG, as Chrome draws one."""
    width, _, depth, kind = struct.unpack(">IIBB", png[16:26])
    assert depth == 8 and kind in (2, 6)
    step = 3 if kind == 2 else 4
    chunks, at = [], 8
    while at < len(png):
        length, name = struct.unpack(">I4s", png[at : at + 8])
        if name == b"IDAT":
            chunks.append(png[at + 8 : at + 8 + length])
        at += 12 + length
    raw, stride = zlib.decompress(b"".join(chunks)), width * step
    prior = bytearray(stride)
    for row in range(y + 1):
        start = row * (stride + 1)
        filtering, line = raw[start], bytearray(raw[start + 1 : start + 1 + stride])
        for i in range(stride):
            a, b = line[i - step] if i >= step else 0, prior[i]
            c = prior[i - step] if i >= step else 0
            p = a + b - c
            paeth = min((abs(p - a), 0, a), (abs(p - b), 1, b), (abs(p - c), 2, c))[2]
            line[i] = (line[i] + (0, a, b, (a + b) // 2, paeth)[filtering]) & 255
        prior = line
    return prior[x * step], prior[x * step + 1], prior[x * step + 2]


def dark(png: bytes) -> bool:
    """Whether a 768-pixel picture's screen is dark: a pixel near the frame's top right."""
    return sum(pixel(png, 700, 40)) < 200


def test_the_browser_can_be_chosen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    chrome = tmp_path / "chrome"
    chrome.write_text("")
    monkeypatch.setenv(screenshot.BROWSER_VARIABLE, str(chrome))
    assert screenshot.find_browser() == str(chrome)
    monkeypatch.setenv(screenshot.BROWSER_VARIABLE, str(tmp_path / "missing"))
    with pytest.raises(
        StoreError, match=r"CLIQUET_CHROME is set to .*missing, which doesn't exist"
    ):
        screenshot.find_browser()


def no_browser_installed(monkeypatch: pytest.MonkeyPatch, playwright: Path) -> None:
    """A machine with no browser, where Playwright keeps its downloads in `playwright`."""
    monkeypatch.delenv(screenshot.BROWSER_VARIABLE, raising=False)
    monkeypatch.setattr(screenshot.shutil, "which", lambda name: None)
    monkeypatch.setattr(screenshot, "_MAC_APPS", ())
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(playwright))


def test_screenshots_need_a_browser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    no_browser_installed(monkeypatch, tmp_path / "empty")
    with pytest.raises(StoreError) as error:
        screenshot.take_screenshots("http://127.0.0.1:1", ["overview"], tmp_path)
    assert "no Chrome, Chromium or Edge was found" in str(error.value)
    # The way out adds nothing to Cliquet or to the app.
    assert "`uvx playwright install --with-deps chromium`" in str(error.value)


def test_a_chromium_downloaded_by_playwright_is_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    old = tmp_path / "chromium-1169/chrome-linux/chrome"
    new = tmp_path / "chromium_headless_shell-1228/chrome-headless-shell-linux64"
    for file in (old, new / "chrome-headless-shell", tmp_path / "webkit-2368/pw_run.sh"):
        file.parent.mkdir(parents=True)
        file.write_text("")
        file.chmod(0o755)
    (new / "LICENSE").write_text("")
    no_browser_installed(monkeypatch, tmp_path)
    assert screenshot.find_browser() == str(new / "chrome-headless-shell")  # the newest


def test_a_program_that_isn_t_a_browser_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(screenshot.BROWSER_VARIABLE, "/bin/echo")
    with pytest.raises(StoreError, match="/bin/echo didn't answer"):
        screenshot.take_screenshots("http://127.0.0.1:1", ["overview"], tmp_path)


def recording_program(folder: Path) -> tuple[Path, Path]:
    """A program that isn't a browser: it writes its arguments to a file and exits at once."""
    program, said = folder / "not-a-browser", folder / "arguments.txt"
    program.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {said}\n')
    program.chmod(0o755)
    return program, said


def test_a_browser_runs_without_its_own_sandbox_when_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A container has no user namespaces to give Chromium's own sandbox, so Docker's default
    # seccomp profile stops it: the image, and a test on such a machine, say so.
    monkeypatch.setattr(screenshot.os, "geteuid", lambda: 1000)
    monkeypatch.delenv(screenshot.NO_SANDBOX_VARIABLE, raising=False)
    program, said = recording_program(tmp_path)

    def arguments(*, no_sandbox: bool = False) -> list[str]:
        said.unlink(missing_ok=True)
        with pytest.raises(StoreError):
            screenshot.Browser(str(program), no_sandbox=no_sandbox)
        return said.read_text().split()

    assert "--no-sandbox" not in arguments()
    assert "--no-sandbox" in arguments(no_sandbox=True)
    monkeypatch.setenv(screenshot.NO_SANDBOX_VARIABLE, "1")
    assert "--no-sandbox" in arguments()


def test_a_preview_is_told_to_run_the_browser_without_its_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The preview runs with the app's own empty environment, so it can't read the setting.
    seen: list[list[str]] = []

    def run(command: list[str], *args: Any) -> tuple[int, bytes, bytes]:
        seen.append(command)
        return 0, b"", b""

    monkeypatch.setattr(sandbox, "find_browser", lambda: "/usr/bin/chromium")
    monkeypatch.setattr(sandbox, "_run_limited", run)
    monkeypatch.setattr(sandbox, "no_sandbox_asked", lambda: True)
    sandbox._run_preview(sys.executable, ["--view", "till"], tmp_path, 10)
    assert seen[0][-3:] == ["--browser", "/usr/bin/chromium", "--browser-no-sandbox"]

    monkeypatch.setattr(sandbox, "no_sandbox_asked", lambda: False)
    sandbox._run_preview(sys.executable, ["--view", "till"], tmp_path, 10)
    assert "--browser-no-sandbox" not in seen[1]


def test_a_program_that_exits_before_the_first_command_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    program, _ = recording_program(tmp_path)
    send = screenshot.Browser._send

    def after_its_exit(
        browser: screenshot.Browser, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        browser._process.wait(timeout=10)  # the program is gone before the command is written
        return send(browser, method, params)

    monkeypatch.setattr(screenshot.Browser, "_send", after_its_exit)
    with pytest.raises(StoreError, match=f"{program} didn't answer"):
        screenshot.Browser(str(program))


def test_the_browser_stays_off_the_network(tmp_path: Path) -> None:
    program, said = recording_program(tmp_path)
    with pytest.raises(StoreError, match="didn't answer"):
        screenshot.Browser(str(program))
    arguments = said.read_text().splitlines()
    assert "--disable-background-networking" in arguments
    assert "--disable-component-update" in arguments
    assert "--headless" in arguments


@needs_browser
def test_a_screenshot_waits_for_the_new_page(pages: str) -> None:
    path = screenshot.find_browser()
    assert path is not None
    browser = screenshot.Browser(path)
    try:
        browser.open(f"{pages}/drawn", 768)
        # The old page is drawn and the new one has no stage yet: neither may count.
        browser.open(f"{pages}/slow", 768)
        assert browser.evaluate("document.getElementById('stage').textContent") == "second"
    finally:
        browser.close()


@needs_browser
def test_closing_outlasts_the_browser_writing_its_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    path = screenshot.find_browser()
    assert path is not None
    browser = screenshot.Browser(path)
    profile = Path(browser._profile.name)
    remove = browser._profile.cleanup
    tries: list[int] = []

    def busy() -> None:
        # Chrome's helper processes can still write to the profile as it is removed.
        tries.append(1)
        if len(tries) == 1:
            raise OSError(39, "Directory not empty", str(profile / "Default"))
        remove()

    monkeypatch.setattr(browser._profile, "cleanup", busy)
    browser.close()
    assert len(tries) == 2
    assert not profile.exists()


@needs_browser
def test_a_picture_of_each_view_at_two_widths(tmp_path: Path) -> None:
    runtime = board(tmp_path)
    server = AppServer(runtime, OWNER)
    shots = screenshot_preview(server, ["overview", "settings"], tmp_path / "shots")
    assert [path.name for path, _ in shots] == [
        "overview-768.png",
        "overview-390.png",
        "settings-768.png",
        "settings-390.png",
    ]
    wide, narrow = size(shots[0][0]), size(shots[1][0])
    # Both are drawn at one pixel per CSS pixel, as try_view is, and show the whole screen.
    assert wide[0] == 768 and narrow[0] == 390
    assert 150 < wide[1] < 700 and 150 < narrow[1] < 700
    assert [height for _, height in shots[:2]] == [wide[1], narrow[1]]
    runtime.close()


@needs_browser
def test_a_picture_in_the_dark_theme(tmp_path: Path) -> None:
    runtime = board(tmp_path)
    server = AppServer(runtime, OWNER)
    (light, _), _ = screenshot_preview(server, ["overview"], tmp_path / "light")
    (night, _), _ = screenshot_preview(server, ["overview"], tmp_path / "dark", theme="dark")
    # The screen itself is dark, not only the page around it, and the names stay the same.
    assert not dark(light.read_bytes()) and dark(night.read_bytes())
    assert night.name == light.name == "overview-768.png"
    runtime.close()


@needs_browser
def test_a_long_screen_s_picture_shows_its_top(tmp_path: Path) -> None:
    tasks = "".join(f"Task number {n}\n" for n in range(220))
    runtime = make_app(tmp_path / "board", BOARD, {"tasks.txt": tasks})
    server = AppServer(runtime, OWNER)
    shots = screenshot_preview(server, ["overview"], tmp_path / "shots")
    path, height = shots[1]
    assert path.name == "overview-390.png"
    # About 10,000 pixels tall: the picture is its top, at one pixel per CSS pixel.
    assert 9000 < height < 13000
    assert size(path) == (390, screenshot.MAX_HEIGHT)
    listed = json.loads((tmp_path / "shots" / "pictures.json").read_text())
    assert listed["pictures"][1] == {
        "file": "overview-390.png",
        "view": "overview",
        "width": 390,
        "height": height,
    }
    runtime.close()


@needs_browser
def test_a_failed_capture_names_the_view_and_its_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fails(self: screenshot.Browser) -> bytes:
        raise StoreError("the browser refused Page.captureScreenshot: Unable to capture")

    monkeypatch.setattr(screenshot.Browser, "capture", fails)
    runtime = board(tmp_path)
    server = AppServer(runtime, OWNER)
    with pytest.raises(StoreError) as error:
        screenshot_preview(server, ["overview"], tmp_path / "shots")
    assert re.match(
        r"overview, 768 pixels wide, on a screen \d+ pixels tall, couldn't be captured: the "
        r"browser refused Page.captureScreenshot: Unable to capture$",
        str(error.value),
    )
    # The sandbox learns which view's picture failed, to say what to call next.
    listed = json.loads((tmp_path / "shots" / "pictures.json").read_text())
    assert listed["failed"]["view"] == "overview" and listed["failed"]["width"] == 768
    runtime.close()


@needs_browser
def test_a_view_with_problems_gives_no_picture(tmp_path: Path) -> None:
    # As in `cliquet preview`: the view's problems are the owner's to read.
    runtime = board(tmp_path, broken("{% if owner %}", "{% if True %}"), owner=EVERYONE)
    server = AppServer(runtime, READER)
    with pytest.raises(StoreError, match=r"(?s)overview, 768 pixels wide: .*may open this view"):
        screenshot_preview(server, ["overview"], tmp_path / "shots")
    assert not list((tmp_path / "shots").glob("*.png"))
    runtime.close()


def preview_copy(tmp_path: Path, folder: Path) -> tuple[Path, Path]:
    app = tmp_path / "app"
    app.mkdir()
    source = (folder / "app.py").read_text()
    (app / "app.py").write_text(set_sdk_source(source, local_sdk()))
    data = tmp_path / "data"
    shutil.copytree(folder / "data", data)
    return app, data


def run_preview(app: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cliquet.cli", "preview", str(app), *arguments],
        capture_output=True,
        text=True,
    )


@needs_browser
def test_preview_takes_its_data_from_a_folder(tmp_path: Path) -> None:
    app, data = preview_copy(tmp_path, APPS / "stock")
    out = tmp_path / "out"

    result = run_preview(
        app,
        "--role",
        "owner",
        "--data",
        str(data),
        "--screenshot",
        str(out),
    )

    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in out.glob("*.png")) == [
        "catalogue-390.png",
        "catalogue-768.png",
        "delivery-390.png",
        "delivery-768.png",
        "shelves-390.png",
        "shelves-768.png",
        "takings-390.png",
        "takings-768.png",
        "till-390.png",
        "till-768.png",
    ]


# The examples are what agents copy: every view, for every role, draws at both widths.
EXAMPLE_VIEWS = [
    ("meeting-rooms", "admin", ["administration", "day", "find_room", "week"]),
    ("meeting-rooms", "member", ["day", "find_room", "week"]),
    ("environments", "reader", ["catalog", "details"]),
    ("ledger", "owner", ["assets", "budget", "transactions", "wealth"]),
    ("ledger", "reader", ["assets", "budget", "transactions", "wealth"]),
]


@needs_browser
@pytest.mark.parametrize(("example", "role", "views"), EXAMPLE_VIEWS)
def test_the_examples_have_a_picture_test(
    tmp_path: Path, example: str, role: str, views: list[str]
) -> None:
    app, data = preview_copy(tmp_path, EXAMPLES / example)
    out = tmp_path / "out"

    result = run_preview(app, "--role", role, "--data", str(data), "--screenshot", str(out))

    assert result.returncode == 0, result.stderr
    expected = sorted(f"{view}-{width}.png" for view in views for width in (390, 768))
    assert sorted(path.name for path in out.glob("*.png")) == expected
    for picture in out.glob("*.png"):
        _, height = size(picture)
        assert height > 300, picture.name  # a screen, not an empty page


@needs_browser
def test_preview_can_be_limited_to_some_views(tmp_path: Path) -> None:
    app, data = preview_copy(tmp_path, EXAMPLES / "ledger")
    out = tmp_path / "out"

    result = run_preview(
        app,
        "--role",
        "owner",
        "--data",
        str(data),
        "--screenshot",
        str(out),
        "--view",
        "budget",
    )
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in out.glob("*.png")) == ["budget-390.png", "budget-768.png"]

    missing = run_preview(
        app,
        "--role",
        "owner",
        "--data",
        str(data),
        "--screenshot",
        str(tmp_path / "other"),
        "--view",
        "nope",
    )
    assert missing.returncode == 1
    assert "budget" in missing.stderr
