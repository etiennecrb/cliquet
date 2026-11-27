"""The renderer page in a real browser, handed screens straight, as a host would.

The server checks every screen before it reaches a browser, so these tests also hand the
renderer screens the server would refuse: the script must build none of what isn't allowed.
"""

import json
import threading
from collections.abc import Iterator
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from test_screenshot import needs_browser
from test_script import wait_for

from cliquet import icons, vocabulary
from cliquet.screens import _CHART_TONES
from cliquet.screenshot import Browser, find_browser
from cliquet.views import renderer_html

pytestmark = [pytest.mark.browser, pytest.mark.slow, needs_browser]

# A host for the renderer: it hands the frame `window.screens[view]` for each view laid out,
# answers each other call with `window.answers[tool]`, or "done", and keeps every call and
# notification. Its capabilities add `window.capabilities`. While `window.holding`, the nth
# call waits for `window.release(n)`.
HOST = """\
<!doctype html>
<html><head><meta charset="utf-8"><title>Host</title></head>
<body>
<iframe id="frame" sandbox="allow-scripts allow-same-origin"
        style="width: 100%; height: 900px; border: 0"></iframe>
<script>
"use strict";
const frame = document.getElementById("frame");
window.calls = [];
window.answers = {};
window.screens = {};
window.contexts = [];
window.links = [];
window.notes = [];
window.capabilities = {};
window.holding = false;
const held = new Map();
window.release = (n) => held.get(n)();
const send = (message) => frame.contentWindow.postMessage(message, "*");
function result(name, args) {
  if (window.screens[name]) {
    const screen = {...window.screens[name], arguments: args};
    return {content: [{type: "text", text: "shown"}], structuredContent: screen};
  }
  return window.answers[name] || {content: [{type: "text", text: "done"}]};
}
async function answer({method, params}) {
  if (method === "ui/initialize") {
    return {protocolVersion: params.protocolVersion, hostInfo: {name: "host", version: "0"},
            hostCapabilities: {serverTools: {}, openLinks: {}, ...window.capabilities},
            hostContext: {theme: "light", displayMode: "inline"}};
  }
  if (method === "tools/call") {
    const n = window.calls.length;
    window.calls.push({tool: params.name, arguments: params.arguments || {}});
    if (window.holding) await new Promise((go) => held.set(n, go));
    const inner = params.arguments && params.arguments.view || params.arguments &&
      params.arguments.tool;
    const relays = ["write_app_data", "read_app_data", "show_app_view"];
    const name = relays.includes(params.name) ? inner : params.name;
    const args = name === params.name ? params.arguments : params.arguments.arguments;
    return result(name, args || {});
  }
  if (method === "ui/update-model-context") { window.contexts.push(params); return {}; }
  if (method === "ui/open-link") { window.links.push(params.url); return {}; }
  throw new Error("no " + method);
}
window.addEventListener("message", async (event) => {
  const message = event.data;
  if (event.source !== frame.contentWindow || !message || message.jsonrpc !== "2.0") return;
  if (message.method === "ui/notifications/initialized") {
    const first = window.screens[window.first];
    send({jsonrpc: "2.0", method: "ui/notifications/tool-result", params:
          {content: [], structuredContent: first}});
  } else if (message.method && message.id !== undefined) {
    try {
      send({jsonrpc: "2.0", id: message.id, result: await answer(message)});
    } catch (error) {
      send({jsonrpc: "2.0", id: message.id, error: {code: -32603, message: String(error)}});
    }
  } else if (message.method) {
    window.notes.push(message);
  }
});
window.start = async (first) => {
  window.first = first;
  frame.srcdoc = await (await fetch("renderer.html")).text();
};
</script>
</body></html>
"""


def screen(view: str, body: list[Any], **extra: Any) -> dict[str, Any]:
    """A screen as the server sends one."""
    input_schema = {"type": "object", "properties": {"note": {"type": "string"}}}
    tools = {
        view: {"kind": "view", "input": input_schema},
        "add": {
            "kind": "action",
            "input": {
                "type": "object",
                "properties": {"task": {"type": "string"}, "count": {"type": "integer"}},
                "required": ["task"],
            },
        },
    }
    return {
        "screen": 1,
        "app": "board",
        "view": view,
        "label": view.capitalize(),
        "arguments": {},
        "body": body,
        "tools": tools,
        "accent": "#4338ca",
        "transaction": None,
        "relay": None,
        **extra,
    }


@pytest.fixture
def host() -> Iterator[str]:
    """The host page and the renderer, served on this machine."""
    pages = {"/": HOST.encode(), "/renderer.html": renderer_html().encode()}

    class Pages(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = pages.get(self.path.split("?")[0], b"")
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Pages)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def browser() -> Iterator[Browser]:
    path = find_browser()
    assert path is not None
    with closing(Browser(path)) as opened:
        yield opened


def show(
    browser: Browser,
    host: str,
    screens: dict[str, dict[str, Any]],
    first: str,
    width: int = 768,
    sandbox: str | None = None,
    setup: str | None = None,
) -> None:
    """Open the host with these screens, and wait until the first one is drawn and laid out
    again, as a frame that opens does. `sandbox` replaces the frame's, which is the least the
    MCP Apps spec lets a host give. `setup` runs in the host before the frame opens."""
    browser.visit(host, width)
    if sandbox is not None:
        browser.evaluate(f"frame.sandbox = {json.dumps(sandbox)}")
    if setup is not None:
        browser.evaluate(setup)
    browser.evaluate(f"window.screens = {json.dumps(screens)}")
    browser.evaluate(f"window.start({json.dumps(first)})")
    wait_for(browser, "window.calls.length >= 1")
    wait_for(browser, "frame.contentDocument.querySelector('#screen *') !== null")


DOC = "frame.contentDocument"


def set_theme(browser: Browser, theme: str) -> None:
    """Tell the screen the host's theme is now `theme`, and wait for it to wear it."""
    browser.evaluate(
        'send({jsonrpc: "2.0", method: "ui/notifications/host-context-changed", '
        f'params: {{theme: "{theme}"}}}})'
    )
    wait_for(browser, f"{DOC}.documentElement.dataset.theme === '{theme}'")


def submit(browser: Browser, selector: str) -> None:
    """Press the submit button of the form `selector` finds, as a person would."""
    browser.evaluate(f"{DOC}.querySelector({json.dumps(selector)} + ' button').click()")


def test_a_hostile_tree_is_never_built(browser: Browser, host: str) -> None:
    hostile = [
        ["img", {"src": "x", "onerror": "window.parent.pwned = 1"}],
        ["a", {"href": "javascript:window.parent.pwned = 2", "class": "link"}, "Run"],
        ["div", {"onclick": "window.parent.pwned = 3", "style": "color: red", "class": "card"}],
        ["script", {}, "window.parent.pwned = 4"],
        ["svg", {"onload": "window.parent.pwned = 5"}],
        ["iframe", {"srcdoc": "<script>parent.parent.pwned = 6</script>"}],
        ["form", {"action": "https://example.com", "data-call": "add"}, ["button", {}, "Go"]],
        ["p", {}, "<b>not bold</b>"],
    ]
    show(browser, host, {"board": screen("board", hostile)}, "board")
    document = "frame.contentDocument.getElementById('screen')"
    tags = browser.evaluate(f"[...{document}.querySelectorAll('*')].map(e => e.tagName)")
    assert not {"IMG", "SCRIPT", "SVG", "IFRAME"} & set(tags)
    attributes = browser.evaluate(
        f"[...{document}.querySelectorAll('*')].flatMap(e => [...e.attributes].map(a => a.name))"
    )
    assert not {"onerror", "onclick", "onload", "style", "src", "srcdoc", "href", "action"} & set(
        attributes
    )
    assert browser.evaluate(f"{document}.querySelector('p').textContent") == "<b>not bold</b>"
    assert browser.evaluate(f"{document}.querySelector('p b')") is None
    browser.evaluate(f"{document}.querySelector('a').click()")
    browser.evaluate(
        f"{document}.querySelector('.card') && {document}.querySelector('.card').click()"
    )
    # The form's call goes out after anything those clicks could have set off.
    submit(browser, "form")
    wait_for(browser, "window.calls.some(c => c.tool === 'add')")
    assert browser.evaluate("window.pwned") is None
    assert browser.evaluate("window.links") == []


def test_a_call_carries_its_transaction_relay_and_a_fresh_request_id(
    browser: Browser, host: str
) -> None:
    form = [
        "form",
        {"data-call": "add", "data-args": '{"count": 2}', "data-then": '{"note": "added"}'},
        ["input", {"name": "task", "aria-label": "Task", "value": "Tea"}],
        ["button", {"class": "btn btn-primary"}, "Add"],
    ]
    board = screen(
        "board",
        [form],
        transaction="t-0123",
        relay={
            "routes": {
                "action": {"tool": "write_app_data", "key": "tool"},
                "view": {"tool": "show_app_view", "key": "view"},
            },
            "fixed": {"app": "sam/board"},
        },
    )
    show(browser, host, {"board": board}, "board")
    calls = wait_for(browser, "window.calls.length >= 1 && window.calls")
    # When the frame opens, the view is laid out again, through the relay, in the transaction.
    assert calls[0] == {
        "tool": "show_app_view",
        "arguments": {
            "app": "sam/board",
            "view": "board",
            "arguments": {"app_transaction": "t-0123"},
        },
    }
    for _ in range(2):
        submit(browser, "form")
        wait_for(browser, "window.calls.filter(c => c.tool === 'write_app_data').length")
        wait_for(browser, f"window.calls.length >= {len(calls) + 2}")
        calls = browser.evaluate("window.calls")
    writes = [call for call in calls if call["tool"] == "write_app_data"]
    assert len(writes) == 2
    first, second = (call["arguments"]["arguments"] for call in writes)
    assert {key: first[key] for key in ("task", "count", "app_transaction")} == {
        "task": "Tea",
        "count": 2,
        "app_transaction": "t-0123",
    }
    assert first["app_request_id"] != second["app_request_id"]
    # After the call, the view is laid out again, its arguments changed by data-then.
    after = calls[calls.index(writes[0]) + 1]
    assert after["tool"] == "show_app_view"
    assert after["arguments"]["arguments"] == {"note": "added", "app_transaction": "t-0123"}


def test_a_refusal_shows_in_its_form_and_keeps_what_was_typed(browser: Browser, host: str) -> None:
    body = [
        [
            "form",
            {"data-call": "add", "id": "one"},
            ["input", {"name": "task", "aria-label": "Task"}],
            ["button", {}, "Add"],
        ],
    ]
    show(browser, host, {"board": screen("board", body)}, "board")
    browser.evaluate(
        "window.answers.add = {isError: true, "
        "content: [{type: 'text', text: 'Refused: A task needs a name'}]}"
    )
    browser.evaluate(f"{DOC}.querySelector('input').value = 'Typed'")
    submit(browser, "#one")
    alert = wait_for(browser, f"{DOC}.querySelector('#one [role=alert]')?.textContent")
    assert alert == "A task needs a name"
    assert browser.evaluate(f"{DOC}.querySelector('input').value") == "Typed"


# Whether the bar along the top of the screen shows that a click works, and what the host was
# told of it instead.
WORKING = f"!{DOC}.querySelector('.s-working').hidden"
TOLD = (
    "window.notes.filter(n => n.method === 'cliquet/notifications/working')"
    ".map(n => n.params.working)"
)


def test_a_slow_click_shows_a_bar_along_the_top_of_the_screen(browser: Browser, host: str) -> None:
    form = [
        "form",
        {"data-call": "add"},
        ["input", {"name": "task", "aria-label": "Task", "value": "Tea"}],
        ["button", {"class": "btn"}, "Add"],
    ]
    link = ["a", {"data-view": "", "data-args": '{"note": "again"}'}, "Again"]
    board = screen("board", [form, link])
    show(browser, host, {"board": board}, "board", setup="window.holding = true")
    # Each change of the bar, as it happens.
    browser.evaluate(
        f"window.bar = []; new MutationObserver(() => window.bar.push({WORKING}))"
        f".observe({DOC}.querySelector('.s-working'), {{attributes: true}})"
    )

    # A link's layout shows the bar until it is drawn. The layout the frame asked for when it
    # opened still runs, and shows nothing: it is Cliquet's, not a click.
    browser.evaluate(f"{DOC}.querySelector('a').click()")
    wait_for(browser, f"window.calls.length === 2 && {WORKING}")
    browser.evaluate("window.release(1)")
    wait_for(browser, f"!{WORKING}")
    assert browser.evaluate("window.bar") == [True, False]

    # A form's call, then its layout: one bar from the click until the screen is drawn again,
    # beside the button's own spinner.
    submit(browser, "form")
    wait_for(browser, f"window.calls.length === 3 && {WORKING}")
    assert browser.evaluate(f"{DOC}.querySelector('form button .loading') !== null")
    browser.evaluate("window.release(2)")
    wait_for(browser, "window.calls.length === 4")
    browser.evaluate("window.release(3)")
    wait_for(browser, f"!{WORKING}")
    assert browser.evaluate("window.bar") == [True, False, True, False]
    # A host that didn't ask is told nothing.
    assert browser.evaluate(TOLD) == []


def test_a_host_that_shows_a_click_works_is_told_instead(browser: Browser, host: str) -> None:
    # Cliquet's own app page shows it along the top of its window, which a long screen in a
    # frame as tall as itself can't do.
    link = ["a", {"data-view": "", "data-args": '{"note": "again"}'}, "Again"]
    setup = "window.holding = true; window.capabilities = {cliquetShowsWork: true}"
    show(browser, host, {"board": screen("board", [link])}, "board", setup=setup)

    browser.evaluate(f"{DOC}.querySelector('a').click()")
    wait_for(browser, f"{TOLD}.length === 1")
    assert not browser.evaluate(WORKING)
    browser.evaluate("window.release(1)")
    wait_for(browser, f"{TOLD}.length === 2")
    assert browser.evaluate(TOLD) == [True, False]
    assert not browser.evaluate(WORKING)


def press_enter(browser: Browser, selector: str) -> None:
    """Press Enter in the field `selector` finds, as a person's keyboard does."""
    browser.evaluate(f"{DOC}.querySelector({json.dumps(selector)}).focus()")
    browser.press("Enter")


# A form whose button names a value, and which needs its field.
TASK_FORM = [
    "form",
    {"data-call": "add", "id": "one"},
    ["input", {"name": "task", "aria-label": "Task", "required": ""}],
    ["button", {"name": "count", "value": "3"}, "Add three"],
]
ADDS = "window.calls.filter(c => c.tool === 'add')"


@pytest.mark.parametrize("forms", [False, True], ids=["without-allow-forms", "with-allow-forms"])
def test_a_form_is_sent_once_whatever_the_frame_allows(
    browser: Browser, host: str, forms: bool
) -> None:
    # A host's frame may leave out `allow-forms`, which the spec doesn't require: the browser
    # then sends no form. The script sends each one itself, once, with or without it.
    sandbox = "allow-scripts allow-forms allow-same-origin" if forms else None
    show(browser, host, {"board": screen("board", [TASK_FORM])}, "board", sandbox=sandbox)
    field = f"{DOC}.querySelector('#one input')"

    # Enter in the field presses the form's first button, whose name and value go with it.
    browser.evaluate(f"{field}.value = 'Tea'")
    press_enter(browser, "#one input")
    wait_for(browser, f"{ADDS}.length === 1")
    wait_for(browser, f"!{DOC}.querySelector('#one button').disabled")
    browser.evaluate(f"{field}.value = 'Milk'")
    submit(browser, "#one")
    wait_for(browser, f"{ADDS}.length === 2")
    wait_for(browser, f"!{DOC}.querySelector('#one button').disabled")

    sent = [call["arguments"] for call in browser.evaluate(ADDS)]
    assert [(call["task"], call["count"]) for call in sent] == [("Tea", 3), ("Milk", 3)]
    # Each press sent one call, then one layout: the browser sent nothing more.
    tools = [call["tool"] for call in browser.evaluate("window.calls")]
    assert tools == ["board", "add", "board", "add", "board"]


def test_a_form_with_a_missing_field_is_not_sent(browser: Browser, host: str) -> None:
    show(browser, host, {"board": screen("board", [TASK_FORM])}, "board")

    submit(browser, "#one")
    press_enter(browser, "#one input")

    # The calls arrive in order: a call from the empty form would come before this one.
    browser.evaluate(f"{DOC}.querySelector('#one input').value = 'Tea'")
    submit(browser, "#one")
    wait_for(browser, f"{ADDS}.length >= 1")
    assert [call["arguments"]["task"] for call in browser.evaluate(ADDS)] == ["Tea"]


def test_what_the_person_did_survives_a_new_layout(browser: Browser, host: str) -> None:
    body = [
        [
            "form",
            {"data-view": ""},
            ["input", {"name": "note", "aria-label": "Note"}],
            ["button", {}, "Show"],
        ],
        [
            "form",
            {"data-call": "add"},
            ["input", {"name": "task", "aria-label": "Task"}],
            ["button", {}, "Add"],
        ],
        [
            "div",
            {"role": "tablist", "class": "tabs tabs-border"},
            [
                "input",
                {"type": "radio", "name": "t", "class": "tab", "aria-label": "A", "checked": ""},
            ],
            ["div", {"class": "tab-content"}, "First"],
            ["input", {"type": "radio", "name": "t", "class": "tab", "aria-label": "B"}],
            ["div", {"class": "tab-content"}, "Second"],
        ],
        ["details", {}, ["summary", {}, "More"], "Hidden"],
    ]
    show(browser, host, {"board": screen("board", body)}, "board")
    before = len(browser.evaluate("window.calls"))
    browser.evaluate(
        f"{DOC}.querySelector('[name=task]').value = 'Half typed';"
        f"{DOC}.querySelector('[aria-label=B]').click();"
        f"{DOC}.querySelector('details').open = true;"
        f"{DOC}.querySelector('[name=note]').value = 'look'"
    )
    submit(browser, "form")
    wait_for(browser, f"window.calls.length > {before}")
    wait_for(browser, f"{DOC}.querySelector('[name=note]').value === ''")
    # The view was laid out again with the note; the rest is as the person left it.
    assert browser.evaluate("window.calls.at(-1)") == {
        "tool": "board",
        "arguments": {"note": "look"},
    }
    assert browser.evaluate(f"{DOC}.querySelector('[name=task]').value") == "Half typed"
    assert browser.evaluate(f"{DOC}.querySelector('[aria-label=B]').checked") is True
    assert browser.evaluate(f"{DOC}.querySelector('details').open") is True


def test_only_the_classes_the_check_refuses_show_a_tab_content(browser: Browser, host: str) -> None:
    tab = {"type": "radio", "name": "t", "class": "tab", "aria-label": "A", "checked": ""}
    body = [
        [
            "div",
            {"role": "tablist", "class": "tabs tabs-border"},
            ["input", tab],
            ["div", {"class": "tab-content"}, "First"],
            ["input", {"type": "radio", "name": "t", "class": "tab", "aria-label": "B"}],
            ["div", {"class": "tab-content", "id": "second"}, "Second"],
        ]
    ]
    show(browser, host, {"board": screen("board", body)}, "board")
    classes = sorted(
        {name for name in vocabulary.daisyui_classes() if ":" not in name} | set(vocabulary.LAYOUT)
    )
    # Each class in turn on the content of the tab that isn't checked, which daisyUI hides.
    shown = browser.evaluate(f"""(() => {{
      const second = {DOC}.getElementById('second');
      const view = {DOC}.defaultView;
      return {json.dumps(classes)}.filter((name) => {{
        second.className = 'tab-content ' + name;
        return view.getComputedStyle(second).display !== 'none';
      }});
    }})()""")
    assert set(shown) == vocabulary.SETS_DISPLAY


BASES = ("bg-base-100", "bg-base-200", "bg-base-300")
# The text and background colours of each element of the screen whose id starts with probe, as
# [red, green, blue]: its colour, and the backgrounds of the page and every element down to it,
# each laid over the ones beneath, as the browser blends them. The browser resolves daisyUI's
# oklch and color-mix colours.
PROBED_COLOURS = f"""(() => {{
  const view = {DOC}.defaultView;
  const probe = document.createElement('canvas').getContext('2d', {{willReadFrequently: true}});
  const paint = (colour) => {{
    probe.fillStyle = colour;
    probe.fillRect(0, 0, 1, 1);
    return [...probe.getImageData(0, 0, 1, 1).data.slice(0, 3)];
  }};
  return [...{DOC}.querySelectorAll('[id^=probe]')].map((element) => {{
    probe.clearRect(0, 0, 1, 1);
    paint('#fff');
    let background;
    const chain = [];
    for (let node = element; node; node = node.parentElement) chain.unshift(node);
    for (const node of chain) background = paint(view.getComputedStyle(node).backgroundColor);
    return [paint(view.getComputedStyle(element).color), background];
  }});
}})()"""


def contrast(text: list[int], background: list[int]) -> float:
    """WCAG's contrast ratio of two sRGB colours, from 1 to 21."""

    def luminance(rgb: list[int]) -> float:
        channels = [c / 255 for c in rgb]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted([luminance(text), luminance(background)], reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def contrasts(browser: Browser, host: str, probes: list[tuple[str, ...]]) -> list[dict[str, float]]:
    """The contrast of each probe's text in each theme. A probe is the classes of an element
    with text, and of the elements around it, innermost first."""
    body = []
    for n, (classes, *around) in enumerate(probes):
        node: list[Any] = ["span", {"class": classes, "id": f"probe-{n}"}, "Abc"]
        for outer in around:
            node = ["div", {"class": outer}, node]
        body.append(node)
    show(browser, host, {"board": screen("board", body)}, "board")
    found: list[dict[str, float]] = [{} for _ in probes]
    for theme in ["light", "dark"]:
        set_theme(browser, theme)
        # daisyUI eases a button's colours from one theme to the other.
        wait_for(browser, f"{DOC}.getAnimations().length === 0")
        for ratios, (text, background) in zip(found, browser.evaluate(PROBED_COLOURS), strict=True):
            ratios[theme] = contrast(text, background)
    return found


def _whole(mix: frozenset[str]) -> str:
    """A mix of the check's list as a screen writes it, with its component's own class."""
    parts = {name.split("-")[0] for name in mix} & {"badge", "btn", "link"}
    return " ".join([*parts, *sorted(mix)])


def test_the_mixes_the_check_refuses_can_t_be_read_and_their_replacements_can(
    browser: Browser, host: str
) -> None:
    mixes = list(vocabulary.UNREADABLE.items())
    written = [classes for mix, instead in mixes for classes in (_whole(mix), instead)]
    ratios = iter(contrasts(browser, host, [(one, base) for one in written for base in BASES]))
    for mix, instead in mixes:
        for base in BASES:
            refused = next(ratios)
            assert refused["dark"] < 1.5, (mix, base, refused)
        for base in BASES:
            replacement = next(ratios)
            assert min(replacement.values()) >= 4.5, (instead, base, replacement)


def test_every_mix_that_can_t_be_read_on_a_base_background_is_refused(
    browser: Browser, host: str
) -> None:
    names = vocabulary.daisyui_classes()
    mixes = []
    for part in ["badge", "btn", "alert"]:
        styles = [f" {part}-{one}" for one in vocabulary.NOT_SOLID if f"{part}-{one}" in names]
        for style in ["", *styles]:
            mixes += [
                f"{part}{style} {part}-{colour}"
                for colour in vocabulary.COLOURS
                if f"{part}-{colour}" in names
            ]
    # A colour faded by an opacity, such as text-info/30, is faint by choice, in both themes.
    for colour in [*vocabulary.COLOURS, *(f"{one}-content" for one in vocabulary.COLOURS)]:
        mixes += [f"text-{colour}"] if f"text-{colour}" in names else []
        mixes += [f"link link-{colour}"] if f"link-{colour}" in names else []
    # Each content colour inside each class that gives it its own colour's background.
    on = [
        (colour, f"bg-{colour}" if part == "bg" else f"{part} {part}-{colour}")
        for colour in vocabulary.COLOURS
        for part in vocabulary.ON_COLOUR
        if f"{part}-{colour}" in names
    ]
    probes = [(mix, base) for mix in mixes for base in BASES]
    contents = [(f"text-{colour}-content", around) for colour, around in on]
    ratios = contrasts(browser, host, [*probes, *contents])
    missed = []
    for (classes, base), ratio in zip(probes, ratios[: len(probes)], strict=True):
        refused = vocabulary.unreadable(classes, [base])
        if min(ratio.values()) < 1.5 and not refused:
            missed.append((classes, base, ratio))
        if classes.endswith("-content"):
            # The check refuses a content colour off its own, as it never reads in one theme.
            assert refused and min(ratio.values()) < 2, (classes, base, ratio)
    assert not missed, "\n".join(map(str, missed))
    inside = dict(zip(on, ratios[len(probes) :], strict=True))
    for (colour, around), ratio in inside.items():
        assert not vocabulary.unreadable(f"text-{colour}-content", [around])
        assert ratio == inside[colour, f"bg-{colour}"], around


def test_a_long_part_scrolls_and_a_long_line_is_cut_short(browser: Browser, host: str) -> None:
    rows = [["li", {"class": "list-row"}, f"Movement {n}"] for n in range(40)]
    name = "A product whose name is far too long to fit on one line of a phone " * 3
    cells = [["td", {"class": "layout-truncate"}, name], ["td", {"class": "layout-end"}, "12"]]
    body = [
        ["ul", {"class": "list layout-scroll", "id": "history"}, *rows],
        ["table", {"class": "table", "id": "stock"}, ["tbody", {}, ["tr", {}, *cells]]],
        [
            "div",
            {"class": "layout-scroll-x", "id": "steps"},
            ["p", {"class": "layout-nowrap"}, name],
        ],
    ]
    show(browser, host, {"board": screen("board", body)}, "board")
    history = f"{DOC}.getElementById('history')"
    # 20rem of a 16-pixel rem, then the rest scrolls.
    assert browser.evaluate(f"{history}.clientHeight") == 320
    assert browser.evaluate(f"{history}.scrollHeight > {history}.clientHeight") is True
    # The cut-short cell takes the room the amount leaves, so the table keeps to its width.
    stock = f"{DOC}.getElementById('stock')"
    assert browser.evaluate(f"{stock}.scrollWidth <= {stock}.parentElement.clientWidth") is True
    cell = f"{stock}.querySelector('td')"
    assert browser.evaluate(f"{cell}.scrollWidth > {cell}.clientWidth") is True
    steps = f"{DOC}.getElementById('steps')"
    assert browser.evaluate(f"{steps}.scrollWidth > {steps}.clientWidth") is True


def test_a_row_of_stats_fits_a_phone_with_its_numbers_whole(browser: Browser, host: str) -> None:
    # Three and four numbers in a row, as the views guide asks, each as short as it says.
    def stats(row: str, numbers: list[tuple[str, str]]) -> list[Any]:
        parts = [
            [
                "div",
                {"class": "stat"},
                ["div", {"class": "stat-title"}, title],
                ["div", {"class": "stat-value"}, value],
            ]
            for title, value in numbers
        ]
        return ["div", {"class": "stats card-border", "id": row}, *parts]

    body = [
        stats("three", [("Over a year", "+€1.5M"), ("Costs", "€9.1k"), ("Balance", "+€32k")]),
        stats(
            "four", [("Sales", "€7.8k"), ("Costs", "€9.1k"), ("Stock", "€1.2k"), ("Margin", "42%")]
        ),
    ]
    show(browser, host, {"board": screen("board", body)}, "board", width=380)
    for row in ["three", "four"]:
        found = browser.evaluate(f"""(() => {{
          const row = {DOC}.getElementById('{row}');
          const values = [...row.querySelectorAll('.stat-value')];
          return [row.scrollWidth <= row.clientWidth,
            row.getBoundingClientRect().right <= {DOC}.documentElement.clientWidth,
            values.every((v) => v.scrollWidth <= v.clientWidth)];
        }})()""")
        assert found == [True, True, True], row


def test_a_part_shows_on_a_phone_or_from_480_pixels_wide(browser: Browser, host: str) -> None:
    body = [
        ["span", {"class": "layout-only-narrow", "id": "short"}, "Qty"],
        ["span", {"class": "layout-only-wide md:layout-row", "id": "long"}, "Quantity"],
        ["p", {"class": "layout-lines", "id": "note"}, "Leave at the door.\nCall before 9."],
    ]
    shown = f"[...{DOC}.querySelectorAll('span')].filter(e => e.offsetWidth).map(e => e.id)"
    for width, wanted in [(390, ["short"]), (768, ["long"])]:
        show(browser, host, {"board": screen("board", body)}, "board", width)
        assert browser.evaluate(shown) == wanted
    # The note's line break shows: two lines of text, not one.
    note = f"{DOC}.getElementById('note')"
    line = f"parseFloat(getComputedStyle({note}).lineHeight)"
    assert browser.evaluate(f"{note}.offsetHeight >= 2 * {line}") is True


def test_a_wide_table_scrolls_with_only_its_rows_names_pinned(browser: Browser, host: str) -> None:
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul"]
    head = ["tr", {}, ["th", {}, "Category"], *[["th", {}, month] for month in months]]
    rows = [
        ["tr", {}, ["th", {}, name], *[["td", {}, f"€{n}00"] for n in range(7)]]
        for name in ["Food", "Rent"]
    ]
    table = ["table", {"class": "table table-pin-cols"}, ["thead", {}, head], ["tbody", {}, *rows]]
    body = [["div", {"class": "layout-scroll-x", "id": "wide"}, table]]
    show(browser, host, {"board": screen("board", body)}, "board", 390)
    # Where each cell of the header and of the first row starts, from the scrolling box's left.
    places = f"""(() => {{
      const box = {DOC}.getElementById('wide');
      const left = (cell) => Math.round(cell.getBoundingClientRect().left
        - box.getBoundingClientRect().left);
      return [...box.querySelectorAll('tr')].slice(0, 2).map(
        (row) => [...row.children].map(left));
    }})()"""
    (header, first) = browser.evaluate(places)
    assert browser.evaluate(f"{DOC}.getElementById('wide').scrollWidth > 390") is True
    browser.evaluate(f"{DOC}.getElementById('wide').scrollLeft = 150")
    (scrolled_header, scrolled_first) = browser.evaluate(places)
    # The names that start the rows stay; every other cell, the header's months too, moves.
    assert scrolled_header[0] == header[0] and scrolled_first[0] == first[0]
    moved = zip(header[1:] + first[1:], scrolled_header[1:] + scrolled_first[1:], strict=True)
    assert all(after == before - 150 for before, after in moved)


def test_a_filter_bar_lines_its_button_up_with_the_labelled_field(
    browser: Browser, host: str
) -> None:
    field = [
        "fieldset",
        {"class": "fieldset"},
        ["legend", {"class": "fieldset-legend"}, "Category"],
        ["select", {"class": "select", "name": "note", "id": "field"}, ["option", {}, "Food"]],
    ]
    button = ["button", {"class": "btn", "id": "button"}, "Show"]
    body = [["form", {"class": "layout-row layout-bottom", "data-view": ""}, field, button]]
    show(browser, host, {"board": screen("board", body)}, "board", 390)
    bottom = f"(id) => Math.round({DOC}.getElementById(id).getBoundingClientRect().bottom)"
    field_bottom, button_bottom = browser.evaluate(f"['field', 'button'].map({bottom})")
    assert field_bottom == button_bottom


def test_a_select_alone_in_a_view_form_lays_the_view_out_as_it_changes(
    browser: Browser, host: str
) -> None:
    def year(id: str) -> list[Any]:
        attributes = {"class": "select", "name": "note", "id": id, "aria-label": "Year"}
        return ["select", attributes, ["option", {}, "2026"], ["option", {}, "2025"]]

    alone = ["form", {"data-view": "", "data-args": '{"count": 2}'}, year("alone")]
    shown = ["form", {"data-view": ""}, year("shown"), ["button", {"class": "btn"}, "Show"]]
    show(browser, host, {"board": screen("board", [alone, shown])}, "board", 390)
    # The select alone is as wide as its options; the one beside a button takes its column.
    width = f"(id) => {DOC}.getElementById(id).getBoundingClientRect().width"
    alone_width, shown_width = browser.evaluate(f"['alone', 'shown'].map({width})")
    assert alone_width < 120 < shown_width
    before = len(browser.evaluate("window.calls"))
    pick = (
        f"(id) => {{ const select = {DOC}.getElementById(id); select.value = '2025';"
        " select.dispatchEvent(new Event('change', {bubbles: true})); }"
    )

    # A select beside a submit button waits for it; one alone lays the view out at once, once.
    browser.evaluate(f"({pick})('shown')")
    browser.evaluate(f"({pick})('alone')")
    wait_for(browser, f"window.calls.length > {before}")
    wait_for(browser, f"!{DOC}.querySelector('form').hasAttribute('aria-busy')")
    submit(browser, "form:has(#shown)")
    wait_for(browser, f"window.calls.length > {before + 1}")
    assert browser.evaluate(f"window.calls.slice({before})") == [
        {"tool": "board", "arguments": {"count": 2, "note": "2025"}},
        {"tool": "board", "arguments": {"count": 2, "note": "2025"}},
    ]


def test_a_select_s_latest_choice_is_laid_out_and_a_refused_one_undone(
    browser: Browser, host: str
) -> None:
    options = [["option", {}, "2026"], ["option", {}, "2025"], ["option", {}, "2024"]]
    select = ["select", {"class": "select", "name": "note", "aria-label": "Year"}, *options]
    board = screen("board", [["form", {"data-view": ""}, select]])
    show(browser, host, {"board": board}, "board", setup="window.holding = true")
    browser.evaluate("window.release(0)")
    pick = (
        f"(year) => {{ const select = {DOC}.querySelector('select'); select.value = year;"
        " select.dispatchEvent(new Event('change', {bubbles: true})); }"
    )

    # A second choice while the first is laid out isn't lost: the latest is the one drawn.
    browser.evaluate(f"({pick})('2025')")
    wait_for(browser, "window.calls.length === 2")
    browser.evaluate(f"({pick})('2024')")
    wait_for(browser, "window.calls.length === 3")
    browser.evaluate("window.release(2); window.release(1)")
    wait_for(browser, f"!{DOC}.querySelector('form').hasAttribute('aria-busy')")
    assert [call["arguments"] for call in browser.evaluate("window.calls.slice(1)")] == [
        {"note": "2025"},
        {"note": "2024"},
    ]
    told = browser.evaluate("window.contexts.at(-1).content[0].text")
    assert 'the arguments {"note":"2024"}' in told

    # A refused choice goes back to the one the screen shows, so it can be made again.
    browser.evaluate(
        "window.holding = false; delete window.screens.board; window.answers.board = "
        "{isError: true, content: [{type: 'text', text: 'Refused: no such year.'}]}"
    )
    browser.evaluate(f"({pick})('2025')")
    wait_for(browser, f"{DOC}.querySelector('.s-problem') !== null")
    wait_for(browser, f"{DOC}.querySelector('select').value === '2026'")


# The screen's chart as Chart.js draws it, in a script's `chart`, or undefined.
FIND_CHART = f"""
  const canvas = {DOC}.querySelector('[data-chart] canvas');
  const chart = canvas && frame.contentWindow.Chart.getChart(canvas);"""
# The colours of each chart of the screen as Chart.js draws them, as [red, green, blue]: one per
# series, or per slice of a donut. An area's fading fill isn't one colour, and is left out.
CHART_COLOURS = f"""(() => {{
  const canvases = [...{DOC}.querySelectorAll('[data-chart] canvas')];
  const charts = canvases.map((canvas) => frame.contentWindow.Chart.getChart(canvas));
  if (!charts.length || !charts.every(Boolean)) return null;
  const rgb = (colour) => colour.match(/\\d+/g).slice(0, 3).map(Number);
  return charts.map((chart) => chart.data.datasets.flatMap((d) =>
    [d.backgroundColor].flat().filter((c) => typeof c === 'string').map(rgb)));
}})()"""
# The colour of each series of the screen's first chart.
SERIES_COLOURS = f"({CHART_COLOURS} ?? [null])[0]"
# A CSS colour of the frame's page as [red, green, blue].
PAGE_COLOUR = f"""((name) => {{
  const value = getComputedStyle({DOC}.documentElement).getPropertyValue(name).trim();
  const probe = document.createElement('canvas').getContext('2d', {{willReadFrequently: true}});
  probe.fillStyle = value;
  probe.fillRect(0, 0, 1, 1);
  return [...probe.getImageData(0, 0, 1, 1).data.slice(0, 3)];
}})"""


def test_a_series_tone_gives_it_daisyui_s_colour_in_both_themes(
    browser: Browser, host: str
) -> None:
    chart = {
        "kind": "bar",
        "labels": ["Jun", "Jul"],
        "series": [
            {"name": "Income", "values": [3200, 3100], "tone": "success"},
            {"name": "Spending", "values": [2800, 2950], "tone": "error"},
            {"name": "Savings", "values": [400, 150], "tone": "neutral"},
            {"name": "Food", "values": [600, 640]},
        ],
    }
    body = [["div", {"data-chart": json.dumps(chart)}]]
    show(browser, host, {"board": screen("board", body)}, "board")
    neutral = {}
    for theme in ["light", "dark"]:
        set_theme(browser, theme)
        income, spending, savings, food = wait_for(browser, SERIES_COLOURS)
        assert income == browser.evaluate(f"{PAGE_COLOUR}('--color-success')")
        assert spending == browser.evaluate(f"{PAGE_COLOUR}('--color-error')")
        # The neutral grey stands apart from the page, as daisyUI's near-black neutral
        # wouldn't on the dark one.
        page = browser.evaluate(f"{PAGE_COLOUR}('--color-base-100')")
        assert sum(abs(a - b) for a, b in zip(savings, page, strict=True)) > 150
        assert food not in (income, spending, savings)
        neutral[theme] = savings
    assert neutral["light"] != neutral["dark"]


def test_a_category_keeps_its_colour_in_every_chart_whatever_its_tones(
    browser: Browser, host: str
) -> None:
    # Extras is a slice of a donut, then a series beside each tone: its colour never changes,
    # since no colour of the palette reads as a tone, so none is avoided.
    donut = {
        "kind": "donut",
        "labels": ["Rent", "Extras", "Food"],
        "series": [{"name": "Spending", "values": [900, 120, 400]}],
    }
    charts = [donut] + [
        {
            "kind": "bar",
            "labels": ["Jun", "Jul"],
            "series": [
                {"name": "Toned", "values": [3200, 3100], "tone": tone},
                {"name": "Extras", "values": [100, 120]},
            ],
        }
        for tone in _CHART_TONES
    ]
    body = [["div", {"data-chart": json.dumps(chart)}] for chart in charts]
    show(browser, host, {"board": screen("board", body)}, "board")
    names = [f"'--s-chart-{slot}'" for slot in range(1, 8)]
    names += [f"'--s-tone-{tone}'" for tone in _CHART_TONES]
    for theme in ["light", "dark"]:
        set_theme(browser, theme)
        (_, extras, _), *bars = wait_for(browser, CHART_COLOURS)
        palette = browser.evaluate(f"[{', '.join(names)}].map({PAGE_COLOUR})")
        assert extras in palette[:7]
        for tone, colour, (toned, other) in zip(_CHART_TONES, palette[7:], bars, strict=True):
            assert (toned, other) == (colour, extras), (theme, tone)


def test_a_category_never_takes_the_app_s_accent(browser: Browser, host: str) -> None:
    # The violet accent looks like the palette's violet: two categories would look the same.
    names = ["Rent", "Extras", "Food", "Travel", "Gifts", "Books", "Tea"]
    chart = {
        "kind": "bar",
        "labels": ["Jun", "Jul"],
        "series": [{"name": name, "values": [40, 42]} for name in names],
    }
    body = [["div", {"data-chart": json.dumps(chart)}]]
    show(browser, host, {"board": screen("board", body, accent="#7c3aed")}, "board")
    for theme in ["light", "dark"]:
        set_theme(browser, theme)
        colours = wait_for(browser, SERIES_COLOURS)
        assert browser.evaluate(f"{PAGE_COLOUR}('--color-primary')") not in colours
        assert len({tuple(colour) for colour in colours}) == len(names)


# What Chart.js drew for the screen's chart: each series' type and axis, and each axis' place,
# the last tick it wrote and its title.
CHART_LAYOUT = f"""(() => {{{FIND_CHART}
  if (!chart) return null;
  const types = chart.data.datasets.map(
    (d, i) => [chart.getDatasetMeta(i).type, d.yAxisID, chart.getDatasetMeta(i).order]);
  const axes = Object.fromEntries(['y', 'y2'].filter((id) => chart.scales[id]).map((id) => {{
    const scale = chart.scales[id];
    const title = scale.options.title;
    return [id, [scale.position, scale.ticks.at(-1).label, title.display ? title.text : null]];
  }}));
  const legend = chart.legend.legendItems.map((item) => item.text);
  return {{types, axes, legend}};
}})()"""


def test_a_line_over_bars_has_its_own_axis_and_format(browser: Browser, host: str) -> None:
    chart = {
        "kind": "stacked-bar",
        "labels": ["Jun", "Jul"],
        "series": [
            {"name": "Food", "values": [400, 500]},
            {"name": "Rent", "values": [900, 900]},
            {"name": "Margin", "values": [0.31, 0.28], "kind": "line", "axis": "y2"},
        ],
        "y1": {"format": "currency:EUR"},
        "y2": {"title": "Margin", "format": "percent", "max": 1},
    }
    body = [["div", {"data-chart": json.dumps(chart)}]]
    show(browser, host, {"board": screen("board", body)}, "board")
    drawn = wait_for(browser, CHART_LAYOUT)
    assert [t[:2] for t in drawn["types"]] == [["bar", "y"], ["bar", "y"], ["line", "y2"]]
    # The line is drawn last, over the bars.
    assert drawn["types"][2][2] < drawn["types"][0][2]
    assert drawn["legend"] == ["Food", "Rent", "Margin"]
    # y1 is y: the bars' axis, on the left. y2 is on the right, with its own title and format.
    (left, money, none), (right, rate, title) = drawn["axes"]["y"], drawn["axes"]["y2"]
    assert (left, right) == ("left", "right")
    assert (none, title) == (None, "Margin")
    # The bars stack to 1,400 on the left.
    assert money.startswith("€") and float(money[1:].rstrip("K")) >= 1.4
    # The margin is in percent on the right, up to the 100% its span covers.
    assert rate == "100%"
    cells = browser.evaluate(f"[...{DOC}.querySelectorAll('td')].map(c => c.textContent)")
    assert cells == ["€400.00", "€900.00", "31%", "€500.00", "€900.00", "28%"]


def test_lines_on_the_bars_axis_are_never_stacked(browser: Browser, host: str) -> None:
    chart = {
        "kind": "stacked-bar",
        "labels": ["Jun", "Jul"],
        "series": [
            {"name": "Food", "values": [400, 500]},
            {"name": "Rent", "values": [900, 900]},
            {"name": "Budget", "values": [1200, 1200], "kind": "line"},
            {"name": "Target", "values": [1000, 1000], "kind": "line"},
        ],
    }
    body = [["div", {"data-chart": json.dumps(chart)}]]
    show(browser, host, {"board": screen("board", body)}, "board")
    # Where each line's points are, and where 1,200 and 1,000 are on the axis they share: two
    # lines are stacked neither on the bars nor on each other.
    drawn = f"""(() => {{{FIND_CHART}
      if (!chart) return null;
      const y = chart.scales.y;
      const points = [2, 3].map((i) => chart.getDatasetMeta(i).data.map((p) => Math.round(p.y)));
      const at = [1200, 1000].map((v) => Math.round(y.getPixelForValue(v)));
      return [points, at, Object.keys(chart.scales)];
    }})()"""
    points, (budget, target), scales = wait_for(browser, drawn)
    assert points == [[budget, budget], [target, target]]
    assert "y2" not in scales


PLUS = [["path", {"d": "M5 12h14"}], ["path", {"d": "M12 5v14"}]]
# What the computed style of an element of the frame says, for each property.
STYLE = f"""((selector, ...names) => {{
  const style = getComputedStyle({DOC}.querySelector(selector));
  return names.map((name) => style.getPropertyValue(name));
}})"""


def test_an_icon_takes_the_colour_and_size_of_its_text_in_both_themes(
    browser: Browser, host: str
) -> None:
    button = ["button", {"class": "btn btn-primary"}, ["span", {"data-icon": "plus"}], " Ajouter"]
    body = [["form", {"data-view": ""}, button]]
    show(browser, host, {"board": screen("board", body, icons={"plus": PLUS})}, "board")
    paths = browser.evaluate(
        f"[...{DOC}.querySelectorAll('button svg path')].map(p => p.getAttribute('d'))"
    )
    assert paths == ["M5 12h14", "M12 5v14"]
    assert browser.evaluate(f"{DOC}.querySelector('button').innerText.trim()") == "Ajouter"
    colours = {}
    for theme in ["light", "dark"]:
        set_theme(browser, theme)
        text, size = browser.evaluate(f"{STYLE}('button', 'color', 'font-size')")
        stroke, width, height = browser.evaluate(
            f"{STYLE}('button svg', 'stroke', 'width', 'height')"
        )
        assert stroke == text
        assert width == height == size
        colours[theme] = text
    assert colours["light"] != colours["dark"]


def test_an_icon_alone_in_a_square_button_is_a_quarter_larger(browser: Browser, host: str) -> None:
    close = [
        "button",
        {"class": "btn btn-square btn-sm", "aria-label": "Close"},
        ["span", {"data-icon": "plus"}],
    ]
    body = [["form", {"data-view": ""}, close]]
    show(browser, host, {"board": screen("board", body, icons={"plus": PLUS})}, "board")
    (size,) = browser.evaluate(f"{STYLE}('button', 'font-size')")
    width, height = browser.evaluate(f"{STYLE}('button svg', 'width', 'height')")
    assert width == height == f"{float(size.removesuffix('px')) * 1.25:g}px"


def test_only_the_shapes_of_an_icon_are_drawn(browser: Browser, host: str) -> None:
    hostile = [
        ["path", {"d": "M5 12h14", "onload": "window.parent.pwned = 1", "style": "x"}],
        ["image", {"href": "https://example.com/x.png"}],
        ["script", {}, "window.parent.pwned = 2"],
        ["foreignObject", {}],
        ["circle", {"cx": "1", "cy": "1", "r": "1", "fill": "url(https://example.com/x)"}],
        ["use", {"href": "#x"}],
        "text",
    ]
    body = [
        ["h2", {}, ["span", {"data-icon": "plus"}], " Ajouter"],
        ["p", {}, ["span", {"data-icon": "x"}], " Not sent"],
    ]
    show(browser, host, {"board": screen("board", body, icons={"plus": hostile})}, "board")
    drawn = browser.evaluate(
        f"[...{DOC}.querySelectorAll('#screen svg *')]"
        ".map(e => [e.tagName, ...[...e.attributes].map(a => a.name)])"
    )
    assert drawn == [["path", "d"], ["circle", "cx", "cy", "r"]]
    # An icon the server sent no drawing for is left empty.
    assert browser.evaluate(f"{DOC}.querySelector('p svg')") is None
    assert browser.evaluate("window.pwned") is None


# The value axis of the screen's chart: its lowest and highest value, and every tick it wrote.
VALUE_AXIS = f"""(() => {{{FIND_CHART}
  if (!chart) return null;
  const y = chart.scales.y;
  return {{min: y.min, max: y.max, ticks: y.ticks.map((t) => t.label)}};
}})()"""


def test_a_line_fits_its_axis_to_a_narrow_band(browser: Browser, host: str) -> None:
    def chart(kind: str) -> list[Any]:
        """A chart of monthly uptime near 100%, in percent."""
        spec = {
            "kind": kind,
            "labels": ["Jul", "Aug", "Sep", "Oct"],
            "series": [{"name": "Uptime", "values": [0.991, 0.995, 0.993, 0.999]}],
            "y": {"format": "percent"},
        }
        return [["div", {"data-chart": json.dumps(spec)}]]

    screens = {kind: screen(kind, chart(kind)) for kind in ("line", "bar")}
    show(browser, host, screens, "line")
    line = wait_for(browser, VALUE_AXIS)
    # The axis spans about the values, and no two ticks read the same.
    assert 0.98 <= line["min"] <= 0.991 and 0.999 <= line["max"] <= 1.01
    assert len(set(line["ticks"])) == len(line["ticks"]) > 2
    show(browser, host, screens, "bar")
    bar = wait_for(browser, VALUE_AXIS)
    # A bar's length is its value, so its axis starts at zero.
    assert bar["min"] == 0


CATEGORIES = ["Food", "Rent", "Leisure"]


def expenses_grid(count: int, *, wide: bool = False) -> dict[str, Any]:
    """A grid of `count` made-up expenses: a shop, a day, a category and an amount, with
    four more columns of figures when it is `wide`."""
    columns: list[dict[str, Any]] = [
        {"field": "paid_to", "header": "Paid to"},
        {"field": "day", "header": "Day", "format": "date"},
        {"field": "category"},
        {"field": "amount", "header": "Amount", "format": "currency:EUR", "total": "sum"},
    ]
    if wide:
        columns += [{"field": f"q{n}", "header": f"Q{n}", "format": "integer"} for n in range(4)]
    rows = [
        {
            "paid_to": f"Shop {n}",
            "day": f"2026-{1 + n % 12:02d}-{1 + n % 28:02d}",
            "category": CATEGORIES[n % 3],
            "amount": n + 1.5,
            **({f"q{q}": 1000 * n + q for q in range(4)} if wide else {}),
        }
        for n in range(count)
    ]
    return {"columns": columns, "rows": rows}


def euros(amount: float) -> str:
    """An amount as the grid writes it."""
    return f"€{amount:,.2f}"


GRID = f"{DOC}.querySelector('.s-grid')"
# The text of each cell of the grid's body, row by row.
GRID_ROWS = (
    f"[...{GRID}.querySelectorAll('tbody tr')].map((r) => [...r.children].map((c) => "
    "c.textContent))"
)
# The text of each cell of the grid's footer.
GRID_TOTALS = f"[...{GRID}.querySelectorAll('tfoot td, tfoot th')].map((c) => c.textContent)"
# Where the grid's rows are among its pages, as it says.
GRID_SHOWN = f"{GRID}.querySelector('.s-grid-shown').textContent"
# How each header of the grid says its rows are sorted.
GRID_SORT = f"[...{GRID}.querySelectorAll('thead th')].map((th) => th.getAttribute('aria-sort'))"


def shown(first: int, last: int, rows: int) -> str:
    """Where a grid's rows are among its pages, as it says, such as 1 to 25 of 42."""
    return f"{first}\N{EN DASH}{last} of {rows}"


def press(browser: Browser, text: str) -> None:
    """Press the grid's button that reads or is labelled `text`, or tick its box labelled so."""
    found = browser.evaluate(f"""(() => {{
      const all = [...{GRID}.querySelectorAll('button, label.label')];
      const one = all.find((e) => e.textContent.trim() === {json.dumps(text)}
        || e.getAttribute('aria-label') === {json.dumps(text)});
      if (!one) return false;
      (one.tagName === 'LABEL' ? one.querySelector('input') : one).click();
      return true;
    }})()""")
    assert found, f"no {text!r} in the grid"


def enter(browser: Browser, text: str, legend: str | None = None, label: str = "") -> None:
    """Type `text` in the grid's search, or in the field `label` of its filter `legend`."""
    browser.evaluate(f"""(() => {{
      const legend = {json.dumps(legend)};
      let field = {GRID}.querySelector('input[type=search]');
      if (legend) {{
        const set = [...{GRID}.querySelectorAll('fieldset')].find(
          (f) => f.querySelector('legend').textContent === legend);
        field = [...set.querySelectorAll('label')].find(
          (l) => l.textContent.trim() === {json.dumps(label)}).querySelector('input');
      }}
      field.value = {json.dumps(text)};
      field.dispatchEvent(new Event('input', {{bubbles: true}}));
    }})()""")


def test_a_grid_sorts_searches_filters_and_pages_its_rows(browser: Browser, host: str) -> None:
    grid = expenses_grid(42)
    amounts = [row["amount"] for row in grid["rows"]]
    body = [["div", {"data-grid": json.dumps(grid), "aria-label": "Expenses"}]]
    show(browser, host, {"board": screen("board", body)}, "board")
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    rows = browser.evaluate(GRID_ROWS)
    assert len(rows) == 25
    # A date reads as people write it, an amount in its currency, a header without one as
    # its field.
    assert rows[0] == ["Shop 0", "1 Jan 2026", "Food", "€1.50"]
    assert browser.evaluate(f"{GRID}.querySelectorAll('thead th')[2].textContent") == "Category"
    assert browser.evaluate(GRID_SHOWN) == shown(1, 25, 42)
    assert browser.evaluate(GRID_TOTALS) == ["Total", "", "", euros(sum(amounts))]
    # Every control is a button or a labelled field, so a keyboard and a screen reader reach it.
    assert browser.evaluate(
        f"[...{GRID}.querySelectorAll('thead th')].every((th) => "
        "th.querySelector('button[type=button]'))"
    )
    assert browser.evaluate(
        f"[...{GRID}.querySelectorAll('input')].every((i) => "
        "i.labels.length || i.getAttribute('aria-label'))"
    )

    # A header's arrow shows only once it sorts, on the side away from where its values line
    # up: after a text's header, before a number's.
    arrow = f"[...{GRID}.querySelectorAll('.s-grid-arrow')].map((a) => getComputedStyle(a).opacity)"
    assert browser.evaluate(arrow) == ["0"] * 4
    after = (
        f"[...{GRID}.querySelectorAll('.s-grid-sort')].map((b) => "
        "b.querySelector('.s-grid-arrow').getBoundingClientRect().left"
        " > b.getBoundingClientRect().left + 1)"
    )
    assert browser.evaluate(after) == [True, True, True, False]
    # A header sorts up, then down, then as the view sent the rows.
    press(browser, "Amount")
    assert browser.evaluate(GRID_SORT) == ["none", "none", "none", "ascending"]
    # Its arrow fades in.
    wait_for(browser, f"{arrow}[3] === '1'")
    assert browser.evaluate(GRID_ROWS)[0][3] == "€1.50"
    press(browser, "Amount")
    assert browser.evaluate(GRID_SORT)[3] == "descending"
    assert browser.evaluate(GRID_ROWS)[0][3] == "€42.50"
    press(browser, "Amount")
    assert browser.evaluate(GRID_SORT) == ["none"] * 4
    assert browser.evaluate(GRID_ROWS) == rows
    # Dates sort by day, not by their words, and text as people sort it: Shop 2 before Shop 10.
    press(browser, "Day")
    press(browser, "Day")
    assert browser.evaluate(GRID_ROWS)[0][1] == "24 Dec 2026"
    press(browser, "Paid to")
    names = [row[0] for row in browser.evaluate(GRID_ROWS)]
    assert names[:12] == [f"Shop {n}" for n in range(12)]
    press(browser, "Paid to")
    press(browser, "Paid to")

    # Pages, 25 rows each.
    press(browser, "Next")
    assert browser.evaluate(GRID_SHOWN) == shown(26, 42, 42)
    assert len(browser.evaluate(GRID_ROWS)) == 17
    disabled = (
        f"['Previous', 'Next'].map((t) => [...{GRID}.querySelectorAll('button')]"
        ".find((b) => b.getAttribute('aria-label') === t).disabled)"
    )
    # Two pages need no buttons to go straight to the first or the last.
    assert browser.evaluate(f"{GRID}.querySelectorAll('.s-grid-page').length") == 2
    assert browser.evaluate(disabled) == [False, True]
    press(browser, "Previous")
    assert browser.evaluate(disabled) == [True, False]

    # The search matches any cell's shown text, whatever its case.
    enter(browser, "shop 4")
    assert [row[0] for row in browser.evaluate(GRID_ROWS)] == ["Shop 4", "Shop 40", "Shop 41"]
    assert browser.evaluate(GRID_SHOWN) == shown(1, 3, 3)
    # Rows that fit on one page show no page buttons.
    assert browser.evaluate(f"{GRID}.querySelector('.s-grid-pager .join').offsetHeight") == 0
    assert browser.evaluate(GRID_TOTALS)[3] == euros(amounts[4] + amounts[40] + amounts[41])

    # The filters open behind their button, and narrow the search's rows further.
    toggle = f"[...{GRID}.querySelectorAll('button')].find((b) => b.textContent === 'Filters')"
    assert browser.evaluate(f"{toggle}.getAttribute('aria-expanded')") == "false"
    press(browser, "Filters")
    assert browser.evaluate(f"{toggle}.getAttribute('aria-expanded')") == "true"
    assert browser.evaluate(f"{GRID}.querySelector('.s-grid-panel').offsetHeight > 0")
    # A category is ticked from its values, as it has few of them.
    press(browser, "Rent")
    assert [row[0] for row in browser.evaluate(GRID_ROWS)] == ["Shop 4", "Shop 40"]
    assert browser.evaluate(f"{toggle}.textContent") == "Filters"
    # Its badge counts the panel's filters, not the search, which shows its own words.
    assert browser.evaluate(f"{toggle}.querySelector('.badge').dataset.count") == "1"
    press(browser, "Clear filters")
    assert browser.evaluate(GRID_SHOWN) == shown(1, 25, 42)
    assert browser.evaluate(f"{GRID}.querySelector('input[type=search]').value") == ""
    assert browser.evaluate(f"{GRID}.querySelectorAll('input:checked').length") == 0

    # A number is filtered from and to. The totals add up every row that passes, on every page.
    enter(browser, "2", "Amount", "From")
    assert browser.evaluate(GRID_SHOWN) == shown(1, 25, 41)
    assert browser.evaluate(GRID_TOTALS)[3] == euros(sum(amounts[1:]))
    enter(browser, "20", "Amount", "To")
    assert browser.evaluate(GRID_SHOWN) == shown(1, 18, 18)
    enter(browser, "", "Amount", "From")
    enter(browser, "", "Amount", "To")
    # A date too, and a text with too many values to tick is matched by what it contains.
    enter(browser, "2026-12-01", "Day", "From")
    assert {row[1][-8:] for row in browser.evaluate(GRID_ROWS)} == {"Dec 2026"}
    enter(browser, "", "Day", "From")
    enter(browser, "shop 3", "Paid to", "Contains")
    assert browser.evaluate(GRID_SHOWN) == shown(1, 11, 11)

    # No row matches: the grid says so, with a way to clear the filters.
    enter(browser, "nothing like it")
    assert browser.evaluate(GRID_ROWS) == [["No row matches. Clear filters"]]
    assert browser.evaluate(f"{GRID}.querySelector('.s-grid-pager').offsetHeight") == 0
    browser.evaluate(f"{GRID}.querySelector('tbody button').click()")
    assert browser.evaluate(GRID_SHOWN) == shown(1, 25, 42)


def test_a_long_grid_goes_straight_to_its_first_and_last_pages(browser: Browser, host: str) -> None:
    body = [["div", {"data-grid": json.dumps(expenses_grid(80))}]]
    show(browser, host, {"board": screen("board", body)}, "board")
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    press(browser, "Last page")
    assert browser.evaluate(GRID_SHOWN) == shown(76, 80, 80)
    press(browser, "Previous")
    assert browser.evaluate(GRID_SHOWN) == shown(51, 75, 80)
    press(browser, "First page")
    assert browser.evaluate(GRID_SHOWN) == shown(1, 25, 80)
    # Narrowed to two pages, the grid hides the buttons to its ends.
    enter(browser, "food")
    assert browser.evaluate(GRID_SHOWN) == shown(1, 25, 27)
    pages = f"[...{GRID}.querySelectorAll('.s-grid-page')].filter((b) => b.offsetWidth).length"
    assert browser.evaluate(pages) == 2


def test_a_small_grid_only_sorts_and_writes_every_cell_as_text(browser: Browser, host: str) -> None:
    grid = expenses_grid(3)
    grid["rows"][1]["paid_to"] = "<b>Bold</b>"
    empty = {**grid, "rows": []}
    body = [["div", {"data-grid": json.dumps(grid)}], ["div", {"data-grid": json.dumps(empty)}]]
    show(browser, host, {"board": screen("board", body)}, "board")
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    # No search, filters or pages for three rows.
    assert browser.evaluate(f"{GRID}.querySelectorAll('input, .s-grid-pager').length") == 0
    assert [row[0] for row in browser.evaluate(GRID_ROWS)] == ["Shop 0", "<b>Bold</b>", "Shop 2"]
    assert browser.evaluate(f"{DOC}.querySelector('#screen b')") is None
    press(browser, "Amount")
    press(browser, "Amount")
    assert [row[3] for row in browser.evaluate(GRID_ROWS)] == ["€3.50", "€2.50", "€1.50"]
    # A grid without rows says so.
    second = f"{DOC}.querySelectorAll('.s-grid')[1]"
    assert browser.evaluate(f"{second}.querySelector('tbody').textContent") == "No rows"


GRID_TOOLS = ["sort", "search", "filters", "pages"]
# Which of its tools the grid shows: headers that sort, a search, a Filters button and pages.
GRID_TOOLS_SHOWN = f"""(() => ({{
  sort: {GRID}.querySelectorAll('thead button.s-grid-sort').length > 0,
  search: !!{GRID}.querySelector('input[type=search]'),
  filters: [...{GRID}.querySelectorAll('button')].some((b) => b.textContent === 'Filters'),
  pages: !!{GRID}.querySelector('.s-grid-pager'),
}}))()"""


@pytest.mark.parametrize("left_out", GRID_TOOLS)
def test_a_grid_shows_only_the_tools_it_lists(browser: Browser, host: str, left_out: str) -> None:
    # Five rows, which show sort alone when the grid lists no tools.
    grid = {**expenses_grid(5), "tools": [tool for tool in GRID_TOOLS if tool != left_out]}
    show(
        browser,
        host,
        {"board": screen("board", [["div", {"data-grid": json.dumps(grid)}]])},
        "board",
    )
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    assert browser.evaluate(GRID_TOOLS_SHOWN) == {tool: tool != left_out for tool in GRID_TOOLS}
    if left_out == "sort":
        # Its headers are words alone, which say nothing of a sort.
        assert browser.evaluate(f"{GRID}.querySelector('thead th').textContent") == "Paid to"
        assert browser.evaluate(GRID_SORT) == [None] * 4
    if left_out == "pages":
        # Its count is said to a screen reader alone, as its search changes the rows.
        count = f"{GRID}.querySelector('.s-grid-shown')"
        assert browser.evaluate(f"{count}.getAttribute('aria-live')") == "polite"
        assert browser.evaluate(f"{count}.offsetWidth <= 1") is True
        enter(browser, "shop 4")
        assert browser.evaluate(GRID_SHOWN) == shown(1, 1, 1)
    if left_out == "search":
        press(browser, "Filters")
        enter(browser, "shop 3", "Paid to", "Contains")
        assert browser.evaluate(GRID_ROWS)[0][0] == "Shop 3"
    if left_out == "filters":
        enter(browser, "rent")
        assert browser.evaluate(GRID_SHOWN) == shown(1, 2, 2)


def test_a_grid_without_pages_or_tools_shows_every_row(browser: Browser, host: str) -> None:
    unpaged = {**expenses_grid(42), "tools": ["sort", "search"]}
    bare = {**expenses_grid(42), "tools": []}
    body = [["div", {"data-grid": json.dumps(unpaged)}], ["div", {"data-grid": json.dumps(bare)}]]
    show(browser, host, {"board": screen("board", body)}, "board", 390)
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    # Every row, in the grid's box, which scrolls down past a phone's height.
    assert len(browser.evaluate(GRID_ROWS)) == 42
    assert browser.evaluate(GRID_SHOWN) == shown(1, 42, 42)
    box = f"{GRID}.querySelector('.s-grid-scroll')"
    assert browser.evaluate(f"{box}.scrollHeight > {box}.clientHeight + 200") is True
    press(browser, "Amount")
    press(browser, "Amount")
    assert browser.evaluate(GRID_ROWS)[0][3] == "€42.50"
    # A search that matches nothing is said aloud too.
    enter(browser, "nothing at all")
    assert browser.evaluate(f"{GRID}.querySelector('.s-grid-shown').hidden") is False
    assert browser.evaluate(GRID_SHOWN) == "No row matches."
    enter(browser, "")
    # No tools: the table alone, with no count, even for a screen reader.
    second = f"{DOC}.querySelectorAll('.s-grid')[1]"
    assert browser.evaluate(f"{second}.querySelectorAll('tbody tr').length") == 42
    assert browser.evaluate(f"{second}.childElementCount") == 1
    assert browser.evaluate(f"{second}.querySelectorAll('button, input').length") == 0


def test_a_compact_grid_is_as_tall_as_a_table_xs(browser: Browser, host: str) -> None:
    grid = expenses_grid(12)
    # Without totals, as the plain table has no footer.
    columns = [{k: v for k, v in c.items() if k != "total"} for c in grid["columns"]]
    grid.update(columns=columns, tools=["sort"], density="compact")
    cells = [[str(row[c["field"]]) for c in columns] for row in grid["rows"]]
    table = [
        "table",
        {"class": "table table-xs"},
        ["thead", ["tr", *[["th", c.get("header", c["field"])] for c in columns]]],
        ["tbody", *[["tr", *[["td", cell] for cell in row]] for row in cells]],
    ]
    body = [["div", {"data-grid": json.dumps(grid)}], table]
    show(browser, host, {"board": screen("board", body)}, "board")
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    heights = browser.evaluate(
        f"[...{DOC}.querySelectorAll('table')].map((t) => t.getBoundingClientRect().height)"
    )
    assert len(heights) == 2 and abs(heights[0] - heights[1]) <= 1, heights
    # Every row, with no search and no pages.
    assert len(browser.evaluate(GRID_ROWS)) == 12
    assert browser.evaluate(f"{GRID}.querySelectorAll('input, .s-grid-pager').length") == 0
    # A grid of normal density keeps its taller rows.
    normal = [["div", {"data-grid": json.dumps({**grid, "density": "normal"})}]]
    show(browser, host, {"board": screen("board", normal)}, "board")
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    taller = browser.evaluate(f"{GRID}.querySelector('table').getBoundingClientRect().height")
    assert taller > heights[0] + 50


def test_a_wide_grid_scrolls_sideways_with_its_first_column_pinned(
    browser: Browser, host: str
) -> None:
    body = [["div", {"data-grid": json.dumps(expenses_grid(30, wide=True))}]]
    show(browser, host, {"board": screen("board", body)}, "board", 390)
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    box = f"{GRID}.querySelector('.s-grid-scroll')"
    # The page keeps to the phone's width: only the grid's own box scrolls.
    assert browser.evaluate(f"{DOC}.documentElement.scrollWidth") <= 390
    assert browser.evaluate(f"{box}.scrollWidth > {box}.clientWidth + 150") is True
    # The search and the Filters button share a row that fits.
    assert browser.evaluate(f"{GRID}.querySelector('.s-grid-bar').scrollWidth <= 390") is True
    places = f"""(() => {{
      const box = {box};
      const left = (cell) => Math.round(cell.getBoundingClientRect().left
        - box.getBoundingClientRect().left);
      return [...box.querySelectorAll('tr')].slice(0, 2).map((row) => [...row.children].map(left));
    }})()"""
    header, first = browser.evaluate(places)
    browser.evaluate(f"{box}.scrollLeft = 150")
    wait_for(
        browser,
        f"{GRID}.classList.contains('s-grid') && {box}.classList.contains('s-grid-scrolled')",
    )
    # A sign shows on the right while more columns wait there.
    assert browser.evaluate(f"{box}.classList.contains('s-grid-more')") is True
    scrolled_header, scrolled_first = browser.evaluate(places)
    # The first column stays; every other cell, the header's too, moves.
    assert scrolled_header[0] == header[0] and scrolled_first[0] == first[0]
    moved = zip(header[1:] + first[1:], scrolled_header[1:] + scrolled_first[1:], strict=True)
    assert all(after == before - 150 for before, after in moved)


def test_a_long_grid_keeps_its_header_and_totals_in_sight(browser: Browser, host: str) -> None:
    form = ["form", {"data-view": ""}, ["button", {"class": "btn"}, "Refresh"]]
    body = [form, ["div", {"data-grid": json.dumps(expenses_grid(42))}]]
    show(browser, host, {"board": screen("board", body)}, "board", 390)
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    box = f"{GRID}.querySelector('.s-grid-scroll')"
    # A page of 25 rows is taller than a phone shows: it scrolls down in the grid's own box,
    # since the frame grows to its content and the host's page scrolls it.
    assert browser.evaluate(f"{box}.scrollHeight > {box}.clientHeight + 200") is True
    assert browser.evaluate(f"{box}.clientHeight < 600") is True
    places = f"""(() => {{
      const box = {box}.getBoundingClientRect();
      const top = (cell) => Math.round(cell.getBoundingClientRect().top - box.top);
      const bottom = (cell) => Math.round(box.bottom - cell.getBoundingClientRect().bottom);
      const header = [...{GRID}.querySelectorAll('thead th')].map(top);
      const totals = [...{GRID}.querySelectorAll('tfoot :is(th, td)')].map(bottom);
      const first = top({GRID}.querySelector('tbody th'));
      return {{header, totals, first}};
    }})()"""
    before = browser.evaluate(places)
    browser.evaluate(f"{box}.scrollTop = 300")
    wait_for(browser, f"{box}.scrollTop === 300")
    after = browser.evaluate(places)
    # The rows move up under the header, which stays at the box's top, every cell of it; the
    # totals stay at its bottom.
    assert after["first"] < before["first"] - 250
    assert after["header"] == before["header"] and set(after["header"]) <= {0, 1}
    assert len(set(after["totals"])) == 1 and after["totals"][0] <= 2
    # The view laid out again leaves the rows where the person scrolled them.
    calls = browser.evaluate("window.calls.length")
    submit(browser, "form")
    wait_for(browser, f"window.calls.length > {calls}")
    wait_for(browser, f"{box}.scrollTop === 300")
    # Another page is read from its top.
    press(browser, "Next")
    wait_for(browser, f"{GRID_SHOWN} === {json.dumps(shown(26, 42, 42))}")
    assert browser.evaluate(f"{box}.scrollTop") == 0


# A made-up household's lines of spending, under the heading each sits in.
ESSENTIALS = ["Rent", "Energy", "Water", "Groceries", "Insurance", "Transport"]
EXTRAS = ["Books", "Cinema", "Concerts", "Gifts", "Restaurants", "Travel"]


def statement_grid() -> dict[str, Any]:
    """A grid of a made-up statement: 15 lines of income, 15 of living costs, with Essentials
    and Extras as headings over the lines indented under them, and 15 of savings."""
    columns = [
        {"field": "line", "header": "Line"},
        {"field": "kind", "header": "Kind"},
        {"field": "amount", "header": "Amount", "format": "currency:EUR", "total": "sum"},
    ]
    kinds = ["Fixed", "Variable"]
    rows: list[dict[str, Any]] = [
        {"section": "Income", "line": f"Client {n}", "kind": kinds[n % 2], "amount": 100 * n}
        for n in range(1, 16)
    ]
    for heading, lines in (("Essentials", ESSENTIALS), ("Extras", EXTRAS)):
        rows.append({"section": "Living costs", "line": heading, "amount": None})
        for n, line in enumerate(lines):
            rows.append(
                {
                    "section": "Living costs",
                    "line": line,
                    "kind": kinds[n % 2],
                    "amount": 10 + n,
                    "indent": 1,
                }
            )
            # Groceries has a line of its own under it.
            if line == "Groceries":
                rows.append(
                    {"section": "Living costs", "line": "Market", "kind": "Variable", "amount": 5}
                )
                rows[-1]["indent"] = 2
    rows += [
        {"section": "Savings", "line": f"Fund {n}", "kind": "Fixed", "amount": n}
        for n in range(1, 16)
    ]
    return {"columns": columns, "rows": rows, "group_by": "section"}


# The titles of the grid's groups it shows now, in order.
GRID_TITLES = f"[...{GRID}.querySelectorAll('tr.s-grid-title')].map((r) => r.textContent)"
# The text of the first cell of each row of the grid's body that is a row of data.
GRID_LINES = (
    f"[...{GRID}.querySelectorAll('tbody tr:not(.s-grid-title, .s-grid-subtotal)')]"
    ".map((r) => r.firstChild.textContent)"
)
# The text of each cell of the grid's subtotals.
GRID_SUBTOTALS = (
    f"[...{GRID}.querySelectorAll('tr.s-grid-subtotal')].map((r) => "
    "[...r.children].map((c) => c.textContent))"
)


def test_a_grouped_grid_pins_its_titles_and_totals_each_group(browser: Browser, host: str) -> None:
    grid = statement_grid()
    amounts = {
        title: sum(row["amount"] or 0 for row in grid["rows"] if row["section"] == title)
        for title in ("Income", "Living costs", "Savings")
    }
    compact = {**grid, "density": "compact", "tools": []}
    # A long title, a group named with a dash, and rows with no group or an empty one.
    titled = {
        "columns": [
            {"field": "line"},
            *[{"field": f"q{n}", "format": "currency:EUR"} for n in range(5)],
        ],
        "rows": [
            {"section": section, "line": line, **{f"q{n}": 123456 for n in range(5)}}
            for section, line in (
                ("Everything spent on the house", "Rent"),
                ("—", "Dashed"),
                (None, "Loose"),
                ("", "Blank"),
            )
        ],
        "group_by": "section",
        "tools": [],
    }
    body = [["div", {"data-grid": json.dumps(g)}] for g in (grid, compact, titled)]
    show(browser, host, {"board": screen("board", body)}, "board", 390)
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    # Each group is a body of its own, titled, and its subtotal follows its last row. The page
    # counts rows of data alone: a group it cuts has its subtotal where it ends.
    assert browser.evaluate(f"{GRID}.querySelectorAll('tbody').length") == 2
    assert browser.evaluate(GRID_TITLES) == ["Income", "Living costs"]
    assert browser.evaluate(GRID_SUBTOTALS) == [["Total Income", "", euros(amounts["Income"])]]
    assert browser.evaluate(GRID_SHOWN) == shown(1, 25, 45)
    assert browser.evaluate(GRID_TOTALS) == ["Total", "", euros(sum(amounts.values()))]
    # A line sits in from its heading, and by less in a compact grid.
    indents = f"""[...{DOC}.querySelectorAll('.s-grid')].slice(0, 2).map((g) => ['Essentials',
      'Rent', 'Market'].map((line) => [...g.querySelectorAll('tbody th')]
      .find((th) => th.textContent === line)).map((th) => th && parseFloat(
        getComputedStyle(th).paddingLeft)))"""
    (_, rent, _), (heading, small, market) = browser.evaluate(indents)
    assert heading < small < market and small < rent
    assert (
        browser.evaluate(
            f"[...{DOC}.querySelectorAll('.s-grid')][1].querySelectorAll('.s-grid-subtotal').length"
        )
        == 3
    )

    # A group's title stays just under the header while its rows scroll under it.
    pinned = f"""(() => {{
      const box = {GRID}.querySelector('.s-grid-scroll');
      const header = {GRID}.querySelector('thead th').getBoundingClientRect();
      const cell = {DOC}.elementFromPoint(header.left + 20, header.bottom + 2).closest('th, td');
      const row = cell.closest('tr');
      return {{title: row.classList.contains('s-grid-title') ? row.textContent : null,
               gap: cell.getBoundingClientRect().top - header.bottom, top: box.scrollTop}};
    }})()"""
    assert browser.evaluate(pinned) == {"title": "Income", "gap": 0, "top": 0}
    box = f"{GRID}.querySelector('.s-grid-scroll')"
    browser.evaluate(f"{box}.scrollTop = 300")
    wait_for(browser, f"{box}.scrollTop === 300")
    after = browser.evaluate(pinned)
    assert after["title"] == "Income" and abs(after["gap"]) <= 1
    # Past the next group's title, that one is pinned instead.
    browser.evaluate(f"""(() => {{
      const box = {box};
      const title = {GRID}.querySelectorAll('tr.s-grid-title')[1];
      box.scrollTop += title.getBoundingClientRect().top - box.getBoundingClientRect().top + 120;
    }})()""")
    wait_for(browser, f"{box}.scrollTop > 400")
    after = browser.evaluate(pinned)
    assert after["title"] == "Living costs" and abs(after["gap"]) <= 1

    # The next page titles the group it cut again, and gives its subtotal, over all its rows.
    press(browser, "Next")
    assert browser.evaluate(GRID_TITLES) == ["Living costs", "Savings"]
    assert browser.evaluate(GRID_LINES)[0] == "Cinema"
    assert browser.evaluate(GRID_SUBTOTALS) == [
        ["Total Living costs", "", euros(amounts["Living costs"])],
        ["Total Savings", "", euros(amounts["Savings"])],
    ]
    press(browser, "Previous")

    # A filter hides rows, and the subtotals follow; a group with no row left is hidden.
    press(browser, "Filters")
    press(browser, "Variable")
    variable = [row for row in grid["rows"] if row.get("kind") == "Variable"]
    assert browser.evaluate(GRID_SHOWN) == shown(1, len(variable), len(variable))
    assert browser.evaluate(GRID_TITLES) == ["Income", "Living costs"]
    assert browser.evaluate(GRID_SUBTOTALS) == [
        [f"Total {title}", "", euros(sum(r["amount"] for r in variable if r["section"] == title))]
        for title in ("Income", "Living costs")
    ]
    press(browser, "Clear filters")

    # A search matches a group's title too.
    enter(browser, "living")
    assert browser.evaluate(GRID_TITLES) == ["Living costs"]
    assert browser.evaluate(GRID_SHOWN) == shown(1, 15, 15)
    # A sort keeps a line under its heading: the headings sort among themselves, and the
    # lines under each among themselves.
    press(browser, "Line")
    press(browser, "Line")
    extras = sorted(EXTRAS, reverse=True)
    essentials = sorted(ESSENTIALS, reverse=True)
    at = essentials.index("Groceries") + 1
    essentials[at:at] = ["Market"]
    assert browser.evaluate(GRID_LINES) == ["Extras", *extras, "Essentials", *essentials]
    enter(browser, "")
    # A sort never moves a group, and sorts the rows within each.
    press(browser, "Amount")
    press(browser, "Amount")
    assert browser.evaluate(GRID_TITLES) == ["Income", "Living costs"]
    assert browser.evaluate(GRID_LINES)[:3] == ["Client 15", "Client 14", "Client 13"]

    # A long title reads across its row, past the first column, which keeps its width. Rows
    # with no group, or an empty one, share one titled with a dash, apart from a group named so.
    third = f"{DOC}.querySelectorAll('.s-grid')[2]"
    assert browser.evaluate(f"""[...{third}.querySelectorAll('tr.s-grid-title')]
      .map((r) => r.textContent)""") == ["Everything spent on the house", "—", "—"]
    assert browser.evaluate(f"""[...{third}.querySelectorAll('tbody')].map((b) =>
      [...b.querySelectorAll('tr:not(.s-grid-title) > th')].map((th) => th.textContent))""") == [
        ["Rent"],
        ["Dashed"],
        ["Loose", "Blank"],
    ]
    browser.evaluate(f"{third}.scrollIntoView()")
    sideways = f"{third}.querySelector('.s-grid-scroll')"
    title = f"""(() => {{
      const th = {third}.querySelector('tr.s-grid-title > th'), words = th.firstChild;
      const text = words.getBoundingClientRect();
      const end = {DOC}.elementFromPoint(text.right - 2, (text.top + text.bottom) / 2);
      return {{first: {third}.querySelector('tbody tr:not(.s-grid-title) > th').offsetWidth,
               text: text.width, whole: words.scrollWidth <= words.clientWidth,
               seen: th.contains(end), left: text.left - {sideways}.getBoundingClientRect().left}};
    }})()"""
    found = browser.evaluate(title)
    assert found["first"] < found["text"] and found["whole"] and found["seen"], found
    # It stays in sight as the columns scroll sideways under it.
    browser.evaluate(f"{sideways}.scrollLeft = 100")
    wait_for(browser, f"{sideways}.scrollLeft === 100")
    assert browser.evaluate(title) == found


def test_a_grid_writes_every_decimal_its_numbers_have(browser: Browser, host: str) -> None:
    grid = {
        "columns": [
            {"field": "environment"},
            {"field": "uptime", "format": "percent", "total": "avg"},
            {"field": "minutes", "format": "number", "total": "avg"},
            {"field": "cost", "format": "currency:EUR", "total": "sum"},
        ],
        "rows": [
            {"environment": "Prod", "uptime": 0.99998, "minutes": 99.975, "cost": 12.5},
            {"environment": "Staging", "uptime": 0.99975, "minutes": 0.1 + 0.2, "cost": 0.125},
            {"environment": "Dev", "uptime": 1, "minutes": 3, "cost": 7},
        ],
    }
    show(
        browser,
        host,
        {"board": screen("board", [["div", {"data-grid": json.dumps(grid)}]])},
        "board",
    )
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    # An uptime is never rounded up to 100%: an SLA reads in its last decimals. A float's
    # leftovers from arithmetic are not decimals, and a currency keeps its cents.
    assert browser.evaluate(GRID_ROWS) == [
        ["Prod", "99.998%", "99.975", "€12.50"],
        ["Staging", "99.975%", "0.3", "€0.125"],
        ["Dev", "100%", "3", "€7.00"],
    ]
    # An average has one decimal more than its most precise value.
    assert browser.evaluate(GRID_TOTALS) == ["Total", "Avg 99.991%", "Avg 34.425", "€19.625"]


def test_a_grid_writes_its_numbers_in_the_digits_their_format_gives(
    browser: Browser, host: str
) -> None:
    grid = {
        "columns": [
            {"field": "line"},
            {"field": "whole", "format": "currency:EUR:1.0-0", "total": "sum"},
            {"field": "cents", "format": "currency:EUR:1.2-2", "total": "sum"},
            {"field": "up_to_two", "format": "number:1.0-2", "total": "avg"},
            {"field": "share", "format": "percent:1.1-1", "total": "avg"},
            {"field": "code", "format": "number:3.0-0"},
        ],
        "rows": [
            {"line": "Sales", "whole": 1295870.4, "cents": 12, "up_to_two": 1.234, "share": 0.255},
            {"line": "Refunds", "whole": -3.6, "cents": 0.125, "up_to_two": 2, "share": 0.1},
            {"line": "Fees", "whole": 0.5, "cents": 7.5, "up_to_two": 0.1 + 0.2, "share": 1},
        ],
    }
    for row, code in zip(grid["rows"], (7, 42, 1234), strict=True):
        row["code"] = code
    body = [["div", {"data-grid": json.dumps(grid)}]]
    screens = {"board": screen("board", body), "french": screen("french", body, language="fr")}
    show(browser, host, screens, "board")
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    # Each number is rounded to the nearest of its digits, half away from zero.
    assert browser.evaluate(GRID_ROWS) == [
        ["Sales", "€1,295,870", "€12.00", "1.23", "25.5%", "007"],
        ["Refunds", "-€4", "€0.13", "2", "10.0%", "042"],
        ["Fees", "€1", "€7.50", "0.3", "100.0%", "1,234"],
    ]
    # A total is in its column's digits, an average too.
    assert browser.evaluate(GRID_TOTALS) == [
        "Total",
        "€1,295,867",
        "€19.63",
        "Avg 1.18",
        "Avg 45.2%",
        "",
    ]
    show(browser, host, screens, "french")
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    spaced = browser.evaluate(f"{GRID_ROWS}[0][1]").replace("\u202f", " ").replace("\u00a0", " ")
    assert spaced == "1 295 870 €"


# A CSS colour as [red, green, blue], whatever notation it is in.
AS_RGB = """((value) => {
  const probe = document.createElement('canvas').getContext('2d', {willReadFrequently: true});
  probe.fillStyle = value;
  probe.fillRect(0, 0, 1, 1);
  return [...probe.getImageData(0, 0, 1, 1).data.slice(0, 3)];
})"""
# Each cell of the grid's body and footer, by its column's field: its text colour, the classes
# of its badge, if any, and the badge's background.
GRID_TONES = f"""(() => {{
  const fields = [...{GRID}.querySelectorAll('thead th')].map((th) => th.textContent);
  const rows = [...{GRID}.querySelectorAll('tbody tr, tfoot tr')];
  return rows.map((row) => Object.fromEntries([...row.children].map((cell, at) => {{
    const badge = cell.querySelector('.badge');
    const style = (e) => frame.contentWindow.getComputedStyle(e);
    return [fields[at], {{
      text: cell.textContent,
      colour: {AS_RGB}(style(cell).color),
      badge: badge && badge.className,
      background: badge && {AS_RGB}(style(badge).backgroundColor),
    }}];
  }})));
}})()"""


def test_a_grid_colours_amounts_by_their_sign_and_states_as_soft_badges(
    browser: Browser, host: str
) -> None:
    grid = {
        "columns": [
            {"field": "line", "header": "Line"},
            {"field": "amount", "header": "Amount", "format": "currency:EUR", "total": "sum"},
            {"field": "result", "header": "Result", "format": "integer", "tone": "negative"},
            {
                "field": "status",
                "header": "Status",
                "tones": {"Paid": "success", "Late": "error", "Draft": "neutral"},
            },
        ],
        "rows": [
            {"line": "Sencha", "amount": 120, "result": 5, "status": "Paid"},
            {"line": "Assam", "amount": -80.5, "result": -3, "status": "Late"},
            {"line": "Rooibos", "amount": 0, "result": 0, "status": "Draft"},
            {"line": "Oolong", "amount": -60, "result": 2, "status": "Sent"},
        ],
    }
    grid["columns"][1]["tone"] = "signed"
    show(
        browser,
        host,
        {"board": screen("board", [["div", {"data-grid": json.dumps(grid)}]])},
        "board",
    )
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    colours = {}
    for theme in ["light", "dark"]:
        browser.evaluate(
            'send({jsonrpc: "2.0", method: "ui/notifications/host-context-changed", '
            f'params: {{theme: "{theme}"}}}})'
        )
        wait_for(browser, f"{DOC}.documentElement.dataset.theme === '{theme}'")
        success = browser.evaluate(f"{PAGE_COLOUR}('--color-success')")
        error = browser.evaluate(f"{PAGE_COLOUR}('--color-error')")
        plain = browser.evaluate(f"{PAGE_COLOUR}('--color-base-content')")
        page = browser.evaluate(f"{PAGE_COLOUR}('--color-base-100')")
        *rows, totals = browser.evaluate(GRID_TONES)
        # Income in green, spending in red, zero plain, each number with its own minus.
        amounts = [(row["Amount"]["text"], row["Amount"]["colour"]) for row in rows]
        assert amounts == [
            ("€120.00", success),
            ("-€80.50", error),
            ("€0.00", plain),
            ("-€60.00", error),
        ]
        # The total takes its column's tone.
        assert (totals["Amount"]["text"], totals["Amount"]["colour"]) == ("-€20.50", error)
        # Negative colours only the negatives.
        assert [row["Result"]["colour"] for row in rows] == [plain, error, plain, plain]
        # A state in the map is a soft badge of its tone, neutral a plain soft one, and the
        # others plain text. The cell's text is still its value.
        assert [row["Status"]["badge"] for row in rows] == [
            "badge badge-soft badge-success",
            "badge badge-soft badge-error",
            "badge badge-soft",
            None,
        ]
        assert [row["Status"]["text"] for row in rows] == ["Paid", "Late", "Draft", "Sent"]
        for row in rows[:3]:
            assert row["Status"]["background"] != page
        colours[theme] = rows[0]["Status"]["background"]
    assert colours["light"] != colours["dark"]
    # A sign is the number's as its cell shows it: a sum of float noise, or a value its digits
    # round to zero, is a plain zero, without a minus.
    noise = {
        "columns": [
            {"field": "line"},
            {"field": "net", "format": "currency:EUR", "tone": "signed", "total": "sum"},
            {"field": "units", "format": "number:1.0-0", "tone": "negative"},
        ],
        "rows": [
            {"line": "A", "net": 0.1, "units": -0.4},
            {"line": "B", "net": 0.2, "units": 1},
            {"line": "C", "net": -0.3, "units": 2},
        ],
    }
    show(
        browser,
        host,
        {"noise": screen("noise", [["div", {"data-grid": json.dumps(noise)}]])},
        "noise",
    )
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    *rows, totals = browser.evaluate(GRID_TONES)
    assert (rows[0]["Units"]["text"], rows[0]["Units"]["colour"]) == (
        "0",
        rows[1]["Units"]["colour"],
    )
    assert (totals["Net"]["text"], totals["Net"]["colour"]) == ("€0.00", rows[1]["Units"]["colour"])
    show(
        browser,
        host,
        {"board": screen("board", [["div", {"data-grid": json.dumps(grid)}]])},
        "board",
    )
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    # Sorting, search and filters still read the values.
    press(browser, "Amount")
    assert [row[0] for row in browser.evaluate(GRID_ROWS)] == [
        "Assam",
        "Oolong",
        "Rooibos",
        "Sencha",
    ]


def test_a_grid_speaks_the_app_s_language(browser: Browser, host: str) -> None:
    grid = expenses_grid(42)
    grid["columns"].append({"field": "paid", "header": "Payé"})
    for n, row in enumerate(grid["rows"]):
        row["paid"] = n % 2 == 0
    body = [["div", {"data-grid": json.dumps(grid)}]]
    screens = {
        "board": screen("board", body, language="fr"),
        "other": screen("other", body, language="de"),
    }

    def plain(script: str) -> Any:
        """What `script` gives, with French's narrow and unbreakable spaces as plain ones."""
        found = browser.evaluate(f"JSON.stringify({script})")
        return json.loads(found.replace("\u00a0", " ").replace("\u202f", " "))

    show(browser, host, screens, "board")
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    assert browser.evaluate(f"{DOC}.documentElement.lang") == "fr"
    assert plain(GRID_ROWS)[1] == ["Shop 1", "2 févr. 2026", "Rent", "2,50 €", "Non"]
    assert plain(GRID_TOTALS) == ["Total", "", "", "924,00 €", ""]
    search = f"{GRID}.querySelector('input[type=search]')"
    assert browser.evaluate(f"{search}.placeholder") == "Rechercher…"
    assert browser.evaluate(GRID_SHOWN) == "1\N{EN DASH}25 sur 42"
    # A number is found as a person types it, with a plain space.
    enter(browser, "2,50 €")
    names = [row[0] for row in browser.evaluate(GRID_ROWS)]
    assert names == ["Shop 1", "Shop 11", "Shop 21", "Shop 31", "Shop 41"]
    enter(browser, "")
    press(browser, "Suivante")
    assert browser.evaluate(GRID_SHOWN) == "26\N{EN DASH}42 sur 42"
    press(browser, "Filtres")
    press(browser, "Oui")
    assert browser.evaluate(GRID_SHOWN) == "1\N{EN DASH}21 sur 21"
    enter(browser, "nothing at all")
    assert browser.evaluate(GRID_ROWS) == [["Aucune ligne ne correspond. Effacer les filtres"]]
    # A language the renderer doesn't know reads as English.
    show(browser, host, screens, "other")
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    assert browser.evaluate(f"{DOC}.documentElement.lang") == "en"
    assert browser.evaluate(f"{GRID_ROWS}[1]") == ["Shop 1", "2 Feb 2026", "Rent", "€2.50", "No"]


# The drawings the server sends beside a screen with a grid.
GRID_ICONS = {name: icons.drawings()[name] for name in ("funnel", "search", "x")}
# The words of each chip of the grid's filters on, and its name for a screen reader.
GRID_CHIPS = (
    f"[...{GRID}.querySelectorAll('.s-grid-chip')].map((c) => "
    "[c.textContent, c.getAttribute('aria-label')])"
)
BAR_WORDS: dict[str, dict[str, Any]] = {
    "en": {
        "search": "Search…",
        "clear": "Clear the search",
        "filters": "Filters",
        "chips": "Filters on",
        "remove": "Remove",
        "fields": ("From", "To", "Contains"),
        "on": ["Filters, 4 on", "Filters, 2 on", "Filters, 1 on"],
        "shown": [
            "Paid to: “shop 3”",
            "Day: ≥ 1 Dec 2026",
            "Category: Food, Rent",
            "Amount: €2.00\N{EN DASH}€20.00",
        ],
    },
    "fr": {
        "search": "Rechercher…",
        "clear": "Effacer la recherche",
        "filters": "Filtres",
        "chips": "Filtres actifs",
        "remove": "Retirer",
        "fields": ("De", "À", "Contient"),
        "on": ["Filtres, 4 actifs", "Filtres, 2 actifs", "Filtres, 1 actif"],
        "shown": [
            "Paid to : « shop 3 »",
            "Day : ≥ 1 déc. 2026",
            "Category : Food, Rent",
            "Amount : 2,00 €\N{EN DASH}20,00 €",
        ],
    },
}


def click(browser: Browser, script: str) -> None:
    """Click the middle of what `script` finds in the frame, as a person would: the frame's
    own script may move the focus only after a person's click."""
    x, y = browser.evaluate(f"""(() => {{
      const outer = frame.getBoundingClientRect(), r = ({script}).getBoundingClientRect();
      return [outer.left + r.left + r.width / 2, outer.top + r.top + r.height / 2];
    }})()""")
    browser.click(x, y)


@pytest.mark.parametrize("language", ["en", "fr"])
def test_a_grid_s_bar_clears_its_search_and_shows_each_filter_on(
    browser: Browser, host: str, language: str
) -> None:
    said = BAR_WORDS[language]
    body = [["div", {"data-grid": json.dumps(expenses_grid(42))}]]
    screens = {"board": screen("board", body, language=language, icons=GRID_ICONS)}

    def plain(script: str) -> Any:
        """What `script` gives, with the language's unbreakable spaces as plain ones."""
        found = browser.evaluate(f"JSON.stringify({script})")
        return json.loads(found.replace("\u00a0", " ").replace("\u202f", " "))

    def seen(script: str) -> bool:
        """Whether what `script` finds takes room on the screen."""
        return browser.evaluate(f"(({script}).getBoundingClientRect().width > 1)")

    show(browser, host, screens, "board")
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    search = f"{GRID}.querySelector('input[type=search]')"
    clear = f"{GRID}.querySelector('.s-grid-search button')"
    toggle = f"{GRID}.querySelector('.s-grid-toggle')"
    # The search has a magnifier before it, and a button that clears it once it holds text.
    assert browser.evaluate(f"{search}.placeholder") == said["search"]
    assert browser.evaluate(f"{GRID}.querySelector('.s-grid-search').firstChild.tagName") == "svg"
    assert browser.evaluate(f"{clear}.getAttribute('aria-label')") == said["clear"]
    assert not seen(clear)
    enter(browser, "shop 4")
    assert seen(clear) and seen(f"{clear}.querySelector('svg')")
    assert len(browser.evaluate(GRID_ROWS)) == 3
    click(browser, clear)
    assert browser.evaluate(f"{search}.value") == ""
    assert len(browser.evaluate(GRID_ROWS)) == 25
    assert not seen(clear)
    assert browser.evaluate(f"{DOC}.activeElement === {search}")

    # The Filters button: its word, its funnel, and no count or chip while no filter is on.
    assert browser.evaluate(f"{toggle}.textContent") == said["filters"]
    assert browser.evaluate(f"{toggle}.getAttribute('aria-label')") == said["filters"]
    assert seen(f"{toggle}.querySelector('svg')")
    assert not seen(f"{toggle}.querySelector('.badge')")
    chips = f"{GRID}.querySelector('.s-grid-chips')"
    assert browser.evaluate(f"{chips}.getAttribute('aria-label')") == said["chips"]
    assert browser.evaluate(f"{chips}.offsetHeight") == 0
    press(browser, said["filters"])
    assert browser.evaluate(f"{toggle}.classList.contains('btn-active')")
    press(browser, "Rent")
    press(browser, "Food")
    start, end, contains = said["fields"]
    enter(browser, "2", "Amount", start)
    enter(browser, "20", "Amount", end)
    enter(browser, "2026-12-01", "Day", start)
    enter(browser, "shop 3", "Paid to", contains)
    # With the panel closed, each filter on shows as a chip, in the columns' order.
    press(browser, said["filters"])
    assert browser.evaluate(f"{GRID}.querySelector('.s-grid-panel').offsetHeight") == 0
    assert not browser.evaluate(f"{toggle}.classList.contains('btn-active')")
    remove = said["remove"]
    assert plain(GRID_CHIPS) == [[text, f"{remove} {text}"] for text in said["shown"]]
    assert browser.evaluate(f"{toggle}.textContent") == said["filters"]
    assert browser.evaluate(f"{toggle}.querySelector('.badge').dataset.count") == "4"
    assert plain(f"{toggle}.getAttribute('aria-label')") == said["on"][0]

    # A chip removes its filter, empties its fields in the panel, and hands the focus on.
    chip = f"{GRID}.querySelectorAll('.s-grid-chip')"
    focused = f"{DOC}.activeElement.getAttribute('aria-label')"
    click(browser, f"{chip}[2]")
    assert browser.evaluate(f"{GRID}.querySelectorAll('input:checked').length") == 0
    assert plain(focused) == f"{remove} {said['shown'][3]}"
    click(browser, f"{chip}[2]")
    amount = (
        f"[...[...{GRID}.querySelectorAll('fieldset')].find((f) => "
        "f.querySelector('legend').textContent === 'Amount').querySelectorAll('input')]"
        ".map((i) => i.value)"
    )
    assert browser.evaluate(amount) == ["", ""]
    assert plain(focused) == f"{remove} {said['shown'][1]}"
    assert plain(f"{toggle}.getAttribute('aria-label')") == said["on"][1]
    click(browser, f"{chip}[1]")
    assert plain(f"{toggle}.getAttribute('aria-label')") == said["on"][2]
    click(browser, f"{chip}[0]")
    assert browser.evaluate(f"{DOC}.activeElement === {toggle}")
    assert browser.evaluate(f"{toggle}.getAttribute('aria-label')") == said["filters"]
    assert browser.evaluate(f"{chips}.offsetHeight") == 0
    assert len(browser.evaluate(GRID_ROWS)) == 25

    # The box and the button stand well apart.
    gap = (
        f"{toggle}.getBoundingClientRect().left - "
        f"{GRID}.querySelector('.s-grid-search').getBoundingClientRect().right"
    )
    assert browser.evaluate(gap) >= 16
    # On a phone, the button shows its funnel and its count, its word kept from sight only.
    show(browser, host, screens, "board", 390)
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    press(browser, said["filters"])
    press(browser, "Rent")
    assert not seen(f"{toggle}.querySelector('.s-grid-word')")
    assert seen(f"{toggle}.querySelector('svg')") and seen(f"{toggle}.querySelector('.badge')")
    assert browser.evaluate(f"{toggle}.innerText.trim()") == said["filters"]
    assert browser.evaluate(f"{GRID}.querySelector('.s-grid-bar').scrollWidth <= 390")

    # A long chip keeps its words to one line, as tall as a short one's, and cuts them short.
    long = expenses_grid(42)
    for row in long["rows"]:
        row["category"] += " and everything else bought for the house"
    body = [["div", {"data-grid": json.dumps(long)}]]
    screens = {"board": screen("board", body, language=language, icons=GRID_ICONS)}
    show(browser, host, screens, "board", 390)
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    press(browser, said["filters"])
    for category in CATEGORIES:
        press(browser, f"{category} and everything else bought for the house")
    enter(browser, "shop", "Paid to", contains)
    press(browser, said["filters"])
    sizes = f"""[...{GRID}.querySelectorAll('.s-grid-chip')].map((c) => [c.firstChild.offsetHeight,
      c.firstChild.scrollWidth > c.firstChild.clientWidth])"""
    (short_height, short_cut), (long_height, long_cut) = browser.evaluate(sizes)
    assert long_height == short_height and long_cut and not short_cut


def test_a_grid_writes_a_date_in_a_label_s_format_and_sorts_by_it(
    browser: Browser, host: str
) -> None:
    grid = expenses_grid(12)
    grid["columns"][1] = {
        "field": "day",
        "header": "Day",
        "format": "month:short",
        "total": "max",
    }
    body = [["div", {"data-grid": json.dumps(grid)}]]
    show(browser, host, {"board": screen("board", body)}, "board")
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    # A cell is the date it is, written as its month.
    assert [row[1] for row in browser.evaluate(GRID_ROWS)] == [
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sept",
        "Oct",
        "Nov",
        "Dec",
    ]
    # The column is still a date column: it sorts by the date, not by the month's name.
    press(browser, "Day")
    press(browser, "Day")
    assert browser.evaluate(GRID_ROWS)[0][1] == "Dec"
    # And its filter is still a range of dates.
    press(browser, "Filters")
    day = (
        f"[...{GRID}.querySelectorAll('fieldset')].find((f) => "
        "f.querySelector('legend').textContent === 'Day')"
    )
    assert browser.evaluate(f"[...{day}.querySelectorAll('input')].map((i) => i.type)") == [
        "date",
        "date",
    ]
    # Its total is written in the column's format too, so it reads a month and not the date
    # behind it: the views guide tells a builder who needs the day to give the column date:short.
    assert browser.evaluate(GRID_TOTALS)[1] == "Max Dec"


def test_a_grid_keeps_its_sort_and_search_when_the_view_is_laid_out_again(
    browser: Browser, host: str
) -> None:
    form = ["form", {"data-view": ""}, ["button", {"class": "btn"}, "Refresh"]]
    body = [form, ["div", {"data-grid": json.dumps(expenses_grid(42))}]]
    show(browser, host, {"board": screen("board", body)}, "board")
    wait_for(browser, "window.calls.length >= 1")
    wait_for(browser, f"!!({GRID} && {GRID}.querySelector('tbody tr'))")
    press(browser, "Amount")
    press(browser, "Amount")
    enter(browser, "shop 1")
    before = browser.evaluate(GRID_ROWS)
    calls = browser.evaluate("window.calls.length")
    submit(browser, "form")
    wait_for(browser, f"window.calls.length > {calls}")
    assert browser.evaluate(GRID_SORT)[3] == "descending"
    assert browser.evaluate(f"{GRID}.querySelector('input[type=search]').value") == "shop 1"
    assert browser.evaluate(GRID_ROWS) == before
    assert before[0][0] == "Shop 19"


# What Chart.js drew for the screen's scatter: its type, each axis's title, span and ticks, and
# what the tooltip says of its first point.
SCATTER_LAYOUT = f"""(() => {{{FIND_CHART}
  if (!chart) return null;
  const axes = Object.fromEntries(['x', 'y'].map((id) => {{
    const scale = chart.scales[id];
    const title = scale.options.title;
    return [id, {{type: scale.type, title: title.display ? title.text : null, min: scale.min,
                  max: scale.max, ticks: scale.ticks.map((t) => t.label)}}];
  }}));
  const tips = chart.options.plugins.tooltip.callbacks;
  const first = {{dataset: chart.data.datasets[0], datasetIndex: 0,
                 raw: chart.data.datasets[0].data[0]}};
  return {{type: chart.config.type, axes, tip: [tips.title([first]), tips.label(first)],
           width: chart.width}};
}})()"""
# The screen-reader table of the screen's chart, row by row.
CHART_TABLE = (
    f"[...{DOC}.querySelectorAll('[data-chart] tr')]"
    ".map((row) => [...row.cells].map((cell) => cell.textContent))"
)


def test_a_scatter_draws_each_point_where_its_x_and_y_say(browser: Browser, host: str) -> None:
    chart = {
        "kind": "scatter",
        "x": {"title": "Units sold a month", "format": "integer"},
        "y": {"title": "Margin", "min": 0, "format": "percent"},
        "series": [
            {
                "name": "Green tea",
                "points": [{"x": 140, "y": 0.42, "label": "Sencha"}, {"x": 60, "y": 0.35}],
            },
            {"name": "Black tea", "points": [{"x": 210, "y": 0.28, "label": "Assam"}]},
        ],
    }
    body = [["div", {"data-chart": json.dumps(chart)}]]
    for width in (768, 390):
        show(browser, host, {"board": screen("board", body)}, "board", width)
        drawn = wait_for(browser, SCATTER_LAYOUT)
        assert drawn["type"] == "scatter" and 0 < drawn["width"] <= width
        x, y = drawn["axes"]["x"], drawn["axes"]["y"]
        assert (x["type"], x["title"]) == ("linear", "Units sold a month")
        assert (y["type"], y["title"]) == ("linear", "Margin")
        # The x axis is in whole units, and the y axis starts at the min the view set.
        assert all(tick.replace(",", "").isdigit() for tick in x["ticks"])
        assert x["min"] <= 60 and x["max"] >= 210
        assert y["min"] == 0 and y["max"] >= 0.42
        assert all(tick.endswith("%") for tick in y["ticks"])
        assert drawn["tip"] == [["Sencha"], " Green tea: 140, 42%"]
    assert browser.evaluate(CHART_TABLE) == [
        ["Series", "Label", "Units sold a month", "Margin"],
        ["Green tea", "Sencha", "140", "42%"],
        ["Green tea", "", "60", "35%"],
        ["Black tea", "Assam", "210", "28%"],
    ]


def test_a_chart_writes_its_numbers_in_the_digits_their_format_gives(
    browser: Browser, host: str
) -> None:
    chart = {
        "kind": "bar",
        "labels": ["Jun", "Jul"],
        "series": [
            {"name": "Spent", "values": [1295.5, 80.25]},
            {"name": "Margin", "values": [0.255, 0.1], "kind": "line", "axis": "y2"},
        ],
        "y": {"title": "Spent", "format": "currency:EUR:1.0-0"},
        "y2": {"title": "Margin", "format": "percent:1.1-1"},
    }
    body = [["div", {"data-chart": json.dumps(chart)}]]
    show(browser, host, {"board": screen("board", body)}, "board")
    wait_for(browser, f"{DOC}.querySelectorAll('[data-chart] tr').length === 3")
    assert browser.evaluate(CHART_TABLE) == [
        ["", "Spent", "Margin"],
        ["Jun", "€1,296", "25.5%"],
        ["Jul", "€80", "10.0%"],
    ]
    # The ticks stay short, and step by no less than the format's most decimals: whole euros
    # from 1 to 2 have no tick at 1.2.
    chart = {
        "kind": "line",
        "labels": ["Jun", "Jul"],
        "series": [{"name": "Spent", "values": [1, 2]}],
        "y": {"format": "currency:EUR:1.0-0"},
    }
    show(
        browser,
        host,
        {"board": screen("board", [["div", {"data-chart": json.dumps(chart)}]])},
        "board",
    )
    ticks = wait_for(
        browser,
        f"(() => {{{FIND_CHART} return chart && chart.scales.y.ticks.map((t) => t.label); }})()",
    )
    assert ticks == ["€1", "€2"]
    # A percent steps by its own decimals, not the fraction's: 95% to 100% keeps its ticks.
    chart = {
        "kind": "line",
        "labels": ["Jun", "Jul", "Aug"],
        "series": [{"name": "Uptime", "values": [0.95, 0.97, 1]}],
        "y": {"format": "percent:1.0-0"},
    }
    show(
        browser,
        host,
        {"board": screen("board", [["div", {"data-chart": json.dumps(chart)}]])},
        "board",
    )
    ticks = wait_for(
        browser,
        f"(() => {{{FIND_CHART} return chart && chart.scales.y.ticks.map((t) => t.label); }})()",
    )
    assert len(ticks) > 2 and all(tick.endswith("%") and "." not in tick for tick in ticks)
    assert len(set(ticks)) == len(ticks)


@pytest.mark.parametrize(
    ("form", "centre"),
    [("currency:EUR:1.0-0", "€2,204,622"), ("currency:EUR", "€2,204,621.53"), ("integer", "3")],
)
def test_a_donut_writes_its_total_in_its_format_without_a_float_s_noise(
    browser: Browser, host: str, form: str, centre: str
) -> None:
    # Slices whose sum, added up as floats, has more decimals than any of them.
    values = [1102310.76352, 1102310.76352] if form.startswith("currency") else [0.1 + 0.2] * 10
    spec = {
        "kind": "donut",
        "labels": [f"Slice {n}" for n in range(len(values))],
        "series": [{"name": "Spent", "values": values}],
        "format": form,
    }
    show(
        browser,
        host,
        {"board": screen("board", [["div", {"data-chart": json.dumps(spec)}]])},
        "board",
    )
    written = f"""(() => {{
{FIND_CHART}
      if (!chart) return null;
      const written = [];
      const fillText = chart.ctx.fillText;
      chart.ctx.fillText = function (text, ...rest) {{
        written.push(text);
        return fillText.call(this, text, ...rest);
      }};
      chart.draw();
      chart.ctx.fillText = fillText;
      return written.length ? written : null;
    }})()"""
    assert wait_for(browser, written)[-2:] == [centre, "Spent"]


def test_a_chart_s_span_is_the_least_its_axis_covers(browser: Browser, host: str) -> None:
    def chart(kind: str, values: list[float], y: dict[str, Any]) -> list[Any]:
        spec = {
            "kind": kind,
            "labels": ["Jul", "Aug", "Sep"],
            "series": [{"name": "Uptime", "values": values}],
            "x": {"title": "Month"},
            "y": {"title": "Uptime", "format": "percent", **y},
        }
        return ["div", {"data-chart": json.dumps(spec)}]

    charts = {
        "within": chart("line", [0.991, 0.995, 0.999], {"min": 0.9, "max": 1}),
        "beyond": chart("line", [0.85, 0.95, 1.2], {"min": 0.9, "max": 1}),
        "bars": chart("bar", [0.3, 0.4, 0.5], {"max": 1}),
        "taller": chart("bar", [0.3, 0.4, 1.5], {"max": 1}),
    }
    show(browser, host, {"board": screen("board", list(charts.values()))}, "board", 390)
    # The value axis of each chart, in order, once all of them are drawn.
    value_axes = f"""(() => {{
      const charts = [...{DOC}.querySelectorAll('[data-chart] canvas')]
        .map((canvas) => frame.contentWindow.Chart.getChart(canvas));
      if (charts.length !== {len(charts)} || !charts.every(Boolean)) return null;
      return charts.map(({{scales: {{y}}}}) => ({{min: y.min, max: y.max}}));
    }})()"""
    spans = dict(zip(charts, wait_for(browser, value_axes), strict=True))
    # A line's axis covers the span the view set, though its values are in a narrow band...
    assert 0.85 <= spans["within"]["min"] <= 0.9 and spans["within"]["max"] >= 1
    # ...and grows to show the values beyond it.
    assert spans["beyond"]["min"] <= 0.85 and spans["beyond"]["max"] >= 1.2
    # A bar's axis still starts at zero, and reaches the max, or the tallest bar.
    assert spans["bars"]["min"] == 0 and spans["bars"]["max"] >= 1
    assert spans["taller"]["min"] == 0 and spans["taller"]["max"] >= 1.5
    # The axes are titled, and the x axis still writes the labels.
    titles = f"""(() => {{{FIND_CHART}
      return chart && [...['x', 'y'].map((id) => chart.scales[id].options.title.text),
                       chart.scales.x.ticks.map((t) => chart.scales.x.getLabelForValue(t.value))];
    }})()"""
    assert wait_for(browser, titles) == ["Month", "Uptime", ["Jul", "Aug", "Sep"]]
    assert browser.evaluate(CHART_TABLE)[0] == ["Month", "Uptime"]


# The labels each chart of the screen was drawn with, in the order the screen holds them.
CHART_LABELS = f"""(() => {{
  const charts = [...{DOC}.querySelectorAll('[data-chart] canvas')]
    .map((canvas) => frame.contentWindow.Chart.getChart(canvas));
  return charts.length === 6 && charts.every(Boolean) ? charts.map((c) => c.data.labels) : null;
}})()"""
# The x ticks of the screen's first chart, and its screen reader table's row headers.
FIRST_CHART_TICKS = f"""(() => {{{FIND_CHART}
  const x = chart && chart.scales.x;
  return x && x.ticks.map((tick) => x.getLabelForValue(tick.value));
}})()"""
FIRST_CHART_ROWS = (
    f"[...{DOC}.querySelector('[data-chart]').querySelectorAll('tbody th')]"
    ".map((th) => th.textContent)"
)


def test_a_chart_writes_its_labels_in_a_format_in_the_app_s_language(
    browser: Browser, host: str
) -> None:
    months = [str(month) for month in range(1, 13)]
    weekdays = [str(day) for day in range(1, 8)]
    days = ["2026-11-03", "2026-12-01"]
    # The labels each format is given, then what each app's language writes them as.
    given = {
        "month:short": months,
        "month:long": months,
        "weekday:short": weekdays,
        "weekday:long": weekdays,
        "date:short": days,
        "date:long": days,
    }
    written = {
        "en": [
            ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"],
            [
                "January",
                "February",
                "March",
                "April",
                "May",
                "June",
                "July",
                "August",
                "September",
                "October",
                "November",
                "December",
            ],
            ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
            ["3 Nov 2026", "1 Dec 2026"],
            ["3 November 2026", "1 December 2026"],
        ],
        # A lone month or weekday keeps the language's own casing: nov. and lundi.
        "fr": [
            [
                "janv.",
                "févr.",
                "mars",
                "avr.",
                "mai",
                "juin",
                "juil.",
                "août",
                "sept.",
                "oct.",
                "nov.",
                "déc.",
            ],
            [
                "janvier",
                "février",
                "mars",
                "avril",
                "mai",
                "juin",
                "juillet",
                "août",
                "septembre",
                "octobre",
                "novembre",
                "décembre",
            ],
            ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."],
            ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"],
            ["3 nov. 2026", "1 déc. 2026"],
            ["3 novembre 2026", "1 décembre 2026"],
        ],
    }

    def chart(labels: list[str], form: str) -> list[Any]:
        spec = {
            "kind": "bar",
            "labels": labels,
            "series": [{"name": "Spent", "values": [1] * len(labels)}],
            "x": {"title": "When", "format": form},
        }
        return ["div", {"data-chart": json.dumps(spec)}]

    body = [chart(labels, form) for form, labels in given.items()]
    for code, expected in written.items():
        show(browser, host, {"board": screen("board", body, language=code)}, "board")
        assert wait_for(browser, CHART_LABELS) == expected, code
        # The x ticks, and the screen reader's row headers, read the same labels.
        assert set(wait_for(browser, FIRST_CHART_TICKS)) <= set(expected[0]), code
        assert browser.evaluate(FIRST_CHART_ROWS) == expected[0], code


def test_charts_have_smooth_lines_rounded_stacks_and_a_donut_total(
    browser: Browser, host: str
) -> None:
    def chart(kind: str, *values: list[float]) -> list[Any]:
        names = ["Food", "Rent", "Leisure"]
        series = [{"name": n, "values": v} for n, v in zip(names, values, strict=False)]
        spec: dict[str, Any] = {"kind": kind, "labels": ["Jun", "Jul"], "series": series}
        # A donut has no axes: its values are in the chart's own format.
        spec |= {"format": "integer"} if kind == "donut" else {"y": {"format": "integer"}}
        return ["div", {"data-chart": json.dumps(spec)}]

    charts = [
        chart("line", [400, 500]),
        chart("area", [400, 500]),
        chart("stacked-bar", [400, 500], [900, 900], [100, 0]),
        chart("donut", [400, 900]),
        # A line with one label has no line to draw, only its point.
        [
            "div",
            {
                "data-chart": json.dumps(
                    {
                        "kind": "line",
                        "labels": ["Jun"],
                        "series": [{"name": "Food", "values": [400]}],
                    }
                )
            },
        ],
    ]
    show(browser, host, {"board": screen("board", charts)}, "board")
    # Each chart's own plugins, how its first series is drawn, and for the stack, the radius
    # of each bar; for the donut, the texts it writes when drawn again.
    drawn = f"""(() => {{
      const charts = [...{DOC}.querySelectorAll('[data-chart] canvas')]
        .map((canvas) => frame.contentWindow.Chart.getChart(canvas));
      if (charts.length !== 5 || !charts.every(Boolean)) return null;
      const [line, area, stack, donut, lone] = charts;
      const meta = (chart, i) => chart.getDatasetMeta(i);
      const written = [];
      const fillText = donut.ctx.fillText;
      donut.ctx.fillText = function (text, ...rest) {{
        written.push(text);
        return fillText.call(this, text, ...rest);
      }};
      donut.draw();
      donut.ctx.fillText = fillText;
      return {{
        plugins: charts.map((c) => c.config.plugins.map((p) => p.id)),
        curve: line.data.datasets[0].cubicInterpolationMode,
        points: meta(line, 0).data.map((p) => p.options.radius),
        lone: meta(lone, 0).data.map((p) => p.options.radius),
        fill: meta(area, 0).dataset.options.backgroundColor.constructor.name,
        tops: [0, 1, 2].map((i) => meta(stack, i).data.map((b) => b.options.borderRadius)),
        written,
      }};
    }})()"""
    result = wait_for(browser, drawn)
    assert result["plugins"] == [
        ["cliquetTooltipShadow", "cliquetCrosshair"],
        ["cliquetTooltipShadow", "cliquetCrosshair"],
        ["cliquetTooltipShadow", "cliquetCrosshair"],
        ["cliquetTooltipShadow", "cliquetDonutTotal"],
        ["cliquetTooltipShadow", "cliquetCrosshair"],
    ]
    # A line is a monotone curve whose points show only under the pointer, and an area fades.
    assert result["curve"] == "monotone"
    assert result["points"] == [0, 0]
    # A lone value still shows its point.
    assert result["lone"] == [3]
    assert result["fill"] == "CanvasGradient"
    # Only the top of each stack is rounded: Leisure in June, Rent in July, where Leisure is 0.
    assert result["tops"] == [[0, 0], [0, 4], [4, 0]]
    # The donut writes its total and its series' name in its middle.
    assert result["written"][-2:] == ["1,300", "Food"]
