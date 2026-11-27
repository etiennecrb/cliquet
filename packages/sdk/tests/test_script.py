import json
import queue
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from conftest import APPS, HEADER
from test_runtime import make_app
from test_screenshot import needs_browser, preview_copy, run_preview
from test_views import OWNER

from cliquet.host import host_page
from cliquet.mcp_server import AppServer
from cliquet.preview import preview_server
from cliquet.screenshot import Browser, find_browser
from cliquet.script import _run_step, check_steps, run_script
from cliquet.store import StoreError

pytestmark = [pytest.mark.browser, pytest.mark.slow]


def write_script(tmp_path: Path, view: str, steps: list, width: int | None = None) -> Path:
    path = tmp_path / "steps.json"
    body = {"view": view, "steps": steps}
    if width is not None:
        body["width"] = width
    path.write_text(json.dumps(body))
    return path


def test_check_steps() -> None:
    assert check_steps(
        [
            {"click": "Record sale"},
            {"fill": "Receipt", "with": "R-1"},
            {"choose": "Product", "option": "Tea"},
            {"wait": 500},
        ]
    ) == [
        {"click": "Record sale"},
        {"fill": "Receipt", "with": "R-1"},
        {"choose": "Product", "option": "Tea"},
        {"wait": 500},
    ]

    for bad in (
        {"click": "x", "wait": 1},
        {"unknown": "x"},
        {"wait": 6000},
        {"click": 3},
    ):
        with pytest.raises(StoreError, match="Step 1"):
            check_steps([bad])
    with pytest.raises(StoreError, match="30"):
        check_steps([{"wait": 0}] * 31)


def test_the_host_page_has_no_driver_by_default() -> None:
    page = host_page()
    assert "cliquetCalls" in page
    assert "cliquetBusy" in page
    assert "driver" not in page
    assert "allow-same-origin" not in page
    driven = host_page(driver=True)
    assert 'if (query.has("driver"))' in driven
    assert "allow-same-origin" in driven


@needs_browser
def test_a_script_records_a_sale(tmp_path: Path) -> None:
    app, data = preview_copy(tmp_path, APPS / "stock")
    steps = write_script(
        tmp_path,
        "till",
        [
            {"fill": "Receipt", "with": "R-0900"},
            {"choose": "Product", "option": "Sencha green tea 100 g"},
            {"click": "Add to receipt"},
            {"click": "Record receipt"},
        ],
    )
    out = tmp_path / "out"

    result = run_preview(
        app, "--role", "staff", "--data", str(data), "--script", str(steps), "--out", str(out)
    )

    assert result.returncode == 0, result.stderr
    outcome = json.loads((out / "result.json").read_text())
    assert outcome["error"] in (None, "")
    by_name = {call["tool"]: call for call in outcome["calls"]}
    arguments = by_name["record_receipt"]["arguments"]
    assert arguments["receipt"] == "R-0900"
    assert [(line["sku"], int(line["quantity"])) for line in arguments["lines"]] == [("TEA-SEN", 1)]
    # After the sale, the till is laid out again, with no line left: it shows the new sale.
    assert by_name["till"]["error"] is None and by_name["till"]["result"]["arguments"] == {}
    assert (out / "screen.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # Only the picture and the calls: the server makes the calls again on its preview.
    assert sorted(path.name for path in out.iterdir()) == ["result.json", "screen.png"]
    assert "R-0900" not in (data / "movements.csv").read_text()


@needs_browser
def test_a_refusal_is_in_the_calls(tmp_path: Path) -> None:
    app, data = preview_copy(tmp_path, APPS / "stock")
    steps = write_script(
        tmp_path,
        "till",
        [
            {"fill": "Receipt", "with": "R-0900"},
            {"choose": "Product", "option": "Glass teapot 0.8 l"},
            {"fill": "Units", "with": "99"},
            {"click": "Add to receipt"},
            {"click": "Record receipt"},
        ],
    )
    out = tmp_path / "out"

    result = run_preview(
        app, "--role", "staff", "--data", str(data), "--script", str(steps), "--out", str(out)
    )

    assert result.returncode == 0, result.stderr
    outcome = json.loads((out / "result.json").read_text())
    sale = next(call for call in outcome["calls"] if call["tool"] == "record_receipt")
    assert sale["error"].startswith("Refused by: Stock never goes below zero")
    # The refusal is the app's answer, not a failure of the script.
    assert outcome["error"] is None


@needs_browser
def test_a_step_that_finds_nothing_says_what_is_there(tmp_path: Path) -> None:
    app, data = preview_copy(tmp_path, APPS / "stock")
    steps = write_script(tmp_path, "till", [{"click": "Sell"}])
    out = tmp_path / "out"

    run_preview(
        app, "--role", "staff", "--data", str(data), "--script", str(steps), "--out", str(out)
    )

    outcome = json.loads((out / "result.json").read_text())
    assert outcome["error"].startswith('Step 1: nothing on the screen reads "Sell".')
    assert '"record receipt"' in outcome["error"]
    assert (out / "screen.png").is_file()


@needs_browser
def test_a_disabled_button_is_said(tmp_path: Path) -> None:
    app, data = preview_copy(tmp_path, APPS / "stock")
    steps = write_script(tmp_path, "till", [{"click": "Record receipt"}])
    out = tmp_path / "out"

    run_preview(
        app, "--role", "staff", "--data", str(data), "--script", str(steps), "--out", str(out)
    )

    outcome = json.loads((out / "result.json").read_text())
    assert "disabled" in outcome["error"]


@needs_browser
def test_an_unknown_option_lists_the_options(tmp_path: Path) -> None:
    app, data = preview_copy(tmp_path, APPS / "stock")
    steps = write_script(tmp_path, "till", [{"choose": "Product", "option": "Coffee"}])
    out = tmp_path / "out"

    run_preview(
        app, "--role", "staff", "--data", str(data), "--script", str(steps), "--out", str(out)
    )

    outcome = json.loads((out / "result.json").read_text())
    assert '"sencha green tea 100 g"' in outcome["error"]


@needs_browser
def test_the_script_changes_nothing_real(tmp_path: Path) -> None:
    app, data = preview_copy(tmp_path, APPS / "stock")
    subprocess.run(["git", "init", "-q"], cwd=app, check=True)
    subprocess.run(["git", "add", "app.py"], cwd=app, check=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=t@example.com", "commit", "-qm", "app"],
        cwd=app,
        check=True,
    )
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=app, capture_output=True, text=True, check=True
    ).stdout
    steps = write_script(tmp_path, "till", [{"click": "Record receipt"}])
    out = tmp_path / "out"

    run_preview(
        app, "--role", "staff", "--data", str(data), "--script", str(steps), "--out", str(out)
    )

    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=app, capture_output=True, text=True, check=True
    ).stdout
    assert after == before


LINKS = (
    HEADER
    + """\
\"\"\"The supplier's pages, linked from a screen.\"\"\"

from pathlib import Path

from cliquet import App, QueryContext, Screen

links = App("links", files=["site.txt"])

SUPPLIER = \"\"\"
<p>See the <a class="link" href="{{ site }}/catalogue">Catalogue</a>.</p>
<p>And the <a class="link link-primary" href="{{ site }}/prices">price list</a>.</p>
<a class="btn" href="{{ site }}/order">Order form</a>
\"\"\"


@links.view()
def supplier(ctx: QueryContext) -> Screen:
    \"\"\"Links to the supplier's pages.\"\"\"
    return Screen(SUPPLIER, site=Path("site.txt").read_text().strip())
"""
)


@pytest.fixture
def site() -> Iterator[tuple[str, queue.Queue[str]]]:
    """A site on this machine, and each of its pages a browser opens, in order."""
    opened: queue.Queue[str] = queue.Queue()

    class Site(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/favicon.ico":
                opened.put(self.path)
            body = b"<p>The supplier</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", opened
    finally:
        server.shutdown()
        server.server_close()


def wait_for(browser: Browser, expression: str) -> Any:
    """The value of `expression` in the page, once it is truthy."""
    deadline = time.monotonic() + 15
    while not (value := browser.evaluate(expression)):
        assert time.monotonic() < deadline, f"{expression} stayed false"
        time.sleep(0.05)
    return value


@needs_browser
def test_the_screen_s_links_open_in_a_new_tab(
    tmp_path: Path, site: tuple[str, queue.Queue[str]]
) -> None:
    address, opened = site
    path = find_browser()
    assert path is not None
    with (
        closing(make_app(tmp_path / "links", LINKS, {"site.txt": address})) as runtime,
        closing(Browser(path)) as browser,
        preview_server(AppServer(runtime, OWNER), driver=True) as url,
    ):
        # Links in text and a link styled as a button: the frame may open none of them, so each
        # goes to the host page. For a script, the page notes it and opens no tab.
        browser.open(f"{url}/?view=supplier&shot&driver", 768)
        for number, text in enumerate(["Catalogue", "price list", "Order form"], 1):
            assert _run_step(browser, number, {"click": text}) is None
            wait_for(browser, f"window.cliquetLinks.length === {number}")
        assert browser.evaluate("window.cliquetLinks") == [
            f"{address}/catalogue",
            f"{address}/prices",
            f"{address}/order",
        ]
        # The screen's links have no real address: the frame never navigates by itself.
        hrefs = "[...document.getElementById('frame').contentDocument.querySelectorAll('[href]')]"
        assert browser.evaluate(f"{hrefs}.length") == 0
        # Any other address is refused by the host page, should a screen ever ask for one.
        assert "only a web or mail address opens" in browser.evaluate(
            "(() => { try { openLink('javascript:alert(1)'); } catch (e) { return e.message; } })()"
        )
        assert opened.empty()

        # For a person, the link opens in a new tab.
        browser.open(f"{url}/?view=supplier&driver", 768)
        assert _run_step(browser, 6, {"click": "Catalogue"}) is None
        assert opened.get(timeout=15) == "/catalogue"
        assert browser.evaluate("window.cliquetError || ''") == ""


DATES = (
    HEADER
    + """\
\"\"\"A day to remember, set from a screen.\"\"\"

from datetime import date
from pathlib import Path

from cliquet import ActionContext, App, QueryContext, Screen

days = App("days", files=["day.txt"])

DAY = \"\"\"
<form class="layout-stack" data-call="remember">
  <fieldset class="fieldset">
    <legend class="fieldset-legend">Due</legend>
    <input class="input" type="date" name="day" value="2026-11-01" required>
  </fieldset>
  <button class="btn btn-primary">Remember</button>
</form>
\"\"\"


@days.action()
def remember(ctx: ActionContext, day: date) -> None:
    \"\"\"Remember a day.\"\"\"
    Path("day.txt").write_text(day.isoformat())


@days.view()
def due(ctx: QueryContext) -> Screen:
    \"\"\"The day to remember.\"\"\"
    return Screen(DAY)
"""
)


@needs_browser
def test_a_date_is_set_as_its_picker_would(tmp_path: Path) -> None:
    with (
        closing(make_app(tmp_path / "days", DATES, {"day.txt": ""})) as runtime,
        preview_server(AppServer(runtime, OWNER), driver=True) as url,
    ):
        steps = [{"fill": "Due", "with": "2026-11-20"}, {"click": "Remember"}]
        result = run_script(url, "due", steps, 390)
        assert result.error is None
        (call,) = [call for call in result.calls if call["tool"] == "remember"]
        assert call["arguments"]["day"] == "2026-11-20" and call["error"] is None

        wrong = run_script(url, "due", [{"fill": "Due", "with": "20/11/2026"}], 390)
        assert wrong.error == (
            'Step 1: "Due" is a date field, which takes a day as YYYY-MM-DD, not "20/11/2026".'
        )


@needs_browser
def test_the_host_page_shows_a_spinner_until_the_screen_is_drawn(tmp_path: Path) -> None:
    # The screen's first call waits for the app's sandbox, which can take seconds.
    assert '<body class="loading">' in host_page()
    loading = "document.body.classList.contains('loading')"
    path = find_browser()
    assert path is not None
    with (
        closing(make_app(tmp_path / "days", DATES, {"day.txt": ""})) as runtime,
        closing(Browser(path)) as browser,
        preview_server(AppServer(runtime, OWNER), driver=True) as url,
    ):
        browser.open(f"{url}/?view=due", 768)
        assert not browser.evaluate(loading)
        # A screen that can't be drawn shows why, and no spinner.
        browser.visit(f"{url}/?view=nope", 768)
        wait_for(browser, "window.cliquetError")
        assert not browser.evaluate(loading)


@needs_browser
def test_the_page_around_a_screen_is_told_while_a_click_works(tmp_path: Path) -> None:
    # An app's own page asks the screen's frame for its `progress` and shows it along the top
    # of its window, since the screen's own bar would scroll out of sight with a long screen.
    path = find_browser()
    assert path is not None
    with (
        closing(make_app(tmp_path / "days", DATES, {"day.txt": ""})) as runtime,
        closing(Browser(path)) as browser,
        preview_server(AppServer(runtime, OWNER), driver=True) as url,
    ):
        # A page of the preview's own origin, with the host page in a frame, as an app's page.
        browser.open(f"{url}/?view=due", 768)
        browser.evaluate(
            "(() => { window.told = [];"
            " addEventListener('message', (e) => {"
            "   if (e.data?.cliquet === 'working') told.push(e.data.working); });"
            " const frame = document.createElement('iframe'); frame.id = 'page';"
            " frame.src = '?view=due&embed&driver'; document.body.append(frame); })()"
        )
        host = "document.getElementById('page').contentWindow"
        wait_for(browser, f"{host}.cliquetDrawn")

        # The screen's calls wait until the test answers them.
        browser.evaluate(
            f"(() => {{ const host = {host}; const rpc = host.rpc; host.held = [];"
            " host.rpc = (method, params) =>"
            "   new Promise((go) => host.held.push(() => go(rpc(method, params)))); })()"
        )
        screen = f"{host}.document.getElementById('frame').contentDocument"
        browser.evaluate(f"{screen}.querySelector('button').click()")
        wait_for(browser, "window.told.length === 1")
        assert not browser.evaluate(f"{screen}.querySelector('.s-working:not([hidden])')")
        # The action, then the layout after it: the page is told once each way.
        browser.evaluate(f"{host}.held.shift()()")
        wait_for(browser, f"{host}.held.length === 1")
        browser.evaluate(f"{host}.held.shift()()")
        wait_for(browser, "window.told.length === 2")
        assert browser.evaluate("window.told") == [True, False]


NOTES = (
    HEADER
    + """\
\"\"\"Notes, each with a button that clears it.\"\"\"

from pathlib import Path

from cliquet import ActionContext, App, QueryContext, Screen

notes = App("notes", files=["notes.txt"])

NOTES = \"\"\"
<ul class="list">
  {% for note in notes %}
  <li class="list-row">
    <span data-icon="sticky-note"></span>
    <span class="list-col-grow">{{ note }}</span>
    <form data-call="clear" data-args='{{ {"note": note} | tojson }}'>
      <button class="btn btn-square btn-ghost btn-sm" aria-label="Clear {{ note }}">
        <span data-icon="x"></span>
      </button>
    </form>
  </li>
  {% endfor %}
</ul>
<form class="layout-row" data-call="add">
  <input class="input" name="note" aria-label="Note" placeholder="New note…">
  <button class="btn">Add</button>
</form>
\"\"\"


@notes.action()
def add(ctx: ActionContext, note: str) -> None:
    \"\"\"Add a note.\"\"\"
    with Path("notes.txt").open("a") as notes:
        notes.write(note + "\\n")


@notes.action()
def clear(ctx: ActionContext, note: str) -> None:
    \"\"\"Clear a note.\"\"\"
    kept = [one for one in Path("notes.txt").read_text().splitlines() if one != note]
    Path("notes.txt").write_text("".join(one + "\\n" for one in kept))


@notes.view()
def board(ctx: QueryContext) -> Screen:
    \"\"\"The notes.\"\"\"
    return Screen(NOTES, notes=Path("notes.txt").read_text().splitlines())
"""
)


@needs_browser
def test_a_button_that_shows_only_an_icon_is_clicked_by_its_label(tmp_path: Path) -> None:
    with (
        closing(make_app(tmp_path / "notes", NOTES, {"notes.txt": "Tea\nMilk\n"})) as runtime,
        preview_server(AppServer(runtime, OWNER), driver=True) as url,
    ):
        result = run_script(url, "board", [{"click": "Clear Milk"}], 390)
        assert result.error is None
        (call,) = [call for call in result.calls if call["tool"] == "clear"]
        assert call["arguments"]["note"] == "Milk" and call["error"] is None


@needs_browser
def test_a_field_is_found_by_its_placeholder_with_or_without_its_ellipsis(
    tmp_path: Path,
) -> None:
    with (
        closing(make_app(tmp_path / "notes", NOTES, {"notes.txt": "Tea\n"})) as runtime,
        preview_server(AppServer(runtime, OWNER), driver=True) as url,
    ):
        for name in ("New note", "New note…", "New note..."):
            steps = [{"fill": name, "with": "Jam"}, {"click": "Add"}]
            result = run_script(url, "board", steps, 390)
            assert result.error is None, name
            (call,) = [call for call in result.calls if call["tool"] == "add"]
            assert call["arguments"]["note"] == "Jam" and call["error"] is None


MONTHS = (
    HEADER
    + """\
\"\"\"A month picked from a select in a label.\"\"\"

from cliquet import App, QueryContext, Screen

months = App("months", files=["months.txt"])

PICKER = \"\"\"
<form class="layout-row" data-view>
  <label class="select">
    <span class="label">Month</span>
    <select name="month">
      <option value="2026-10">October 2026</option>
      <option value="2026-11">November 2026</option>
    </select>
  </label>
  <button class="btn">Show the month</button>
</form>
\"\"\"


@months.view()
def picker(ctx: QueryContext, month: str = "2026-11") -> Screen:
    \"\"\"Pick a month.\"\"\"
    return Screen(PICKER, args={"month": month})
"""
)


@needs_browser
def test_a_step_that_finds_nothing_names_the_closest_texts_first(tmp_path: Path) -> None:
    with (
        closing(make_app(tmp_path / "months", MONTHS, {"months.txt": ""})) as runtime,
        preview_server(AppServer(runtime, OWNER), driver=True) as url,
    ):
        result = run_script(url, "picker", [{"click": "Show"}], 390)
    assert result.error is not None
    said, rest = result.error.split(" It also has: ")
    assert said == 'Step 1: nothing on the screen reads "Show". Did you mean "show the month"?'
    # The label wrapping the select is named by its own words, not by its options.
    assert '"month"' in rest and "october" not in result.error
