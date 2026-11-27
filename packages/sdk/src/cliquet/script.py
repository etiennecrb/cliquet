"""Scripted clicks on a preview screen, for `cliquet preview --script`."""

import json
import time
from dataclasses import dataclass
from typing import Any

from cliquet.screenshot import MAX_HEIGHT, Browser, Theme, browser_or_none_found
from cliquet.store import StoreError
from cliquet.vocabulary import closest

_SHAPES = (
    '{"click": "<text>"}, {"fill": "<label>", "with": "<text>"}, '
    '{"choose": "<label>", "option": "<text>"}, or {"wait": <milliseconds>}'
)

# How many other texts on the screen a step that found nothing lists after the close ones.
_REST = 10

# Find what a step names, keep it as `window.cliquetFound` for the step's next move, and say
# where it is on the page, whether it is disabled, and what kind of field it is.
_FIND = r"""
function (kind, text, pickers) {
  const frame = document.getElementById("frame");
  const doc = frame.contentDocument;
  if (!doc) return {found: false, seen: [], error: "the screen's frame can't be reached"};
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  const want = norm(text);
  const bare = (s) => s.replace(/\s*(…|\.\.\.)$/, "");
  const shown = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  // The words of a label, without those of the field it wraps.
  const own = (label) => {
    const copy = label.cloneNode(true);
    for (const inner of copy.querySelectorAll("input, select, textarea")) inner.remove();
    return copy.innerText;
  };
  const names = (el) => {
    const out = [];
    for (const l of el.labels || []) out.push(own(l));
    out.push(el.getAttribute("aria-label"));
    for (const id of (el.getAttribute("aria-labelledby") || "").split(" ")) {
      const l = id && doc.getElementById(id);
      if (l) out.push(l.innerText);
    }
    // daisyUI's own pattern: a fieldset whose legend names its one field.
    const set = el.closest("fieldset");
    const legend = set && [...set.children].find((c) => c.tagName === "LEGEND");
    if (legend) out.push(legend.innerText);
    // A placeholder is found with or without the ellipsis it ends with, such as "Search…".
    out.push(bare(norm(el.getAttribute("placeholder"))));
    return out.map(norm).filter(Boolean);
  };
  const found = [], seen = [];
  if (kind === "field") {
    for (const el of doc.querySelectorAll("input, textarea, select")) {
      if (el.type === "hidden" || el.type === "radio" || el.type === "checkbox") continue;
      if (!shown(el)) continue;
      const all = names(el);
      seen.push(all[0] || "");
      if (all.includes(want) || all.includes(bare(want))) found.push(el);
    }
  } else {
    const selector = 'button, a[role="link"], [role="button"], [role="tab"], ' +
      '[role="menuitem"], summary, input[type="radio"], input[type="checkbox"], label';
    for (const el of doc.querySelectorAll(selector)) {
      if (!shown(el)) continue;
      // A label is named by its own words, not by the options of a select it wraps.
      const text = el.tagName === "LABEL" ? own(el) : el.innerText;
      const name = norm(text) || norm(el.getAttribute("aria-label"));
      seen.push(name);
      if (name === want) found.push(el);
    }
    // A label of a choice clicks its radio or checkbox; prefer the control itself.
    found.sort((a, b) => (a.tagName === "LABEL") - (b.tagName === "LABEL"));
  }
  if (!found.length) return {found: false, seen: seen.filter(Boolean).slice(0, 40)};
  const el = found[0];
  window.cliquetFound = el;
  el.scrollIntoView({block: "center"});
  const outer = frame.getBoundingClientRect(), r = el.getBoundingClientRect();
  const select = el.tagName === "SELECT";
  return {found: true, x: outer.left + r.left + r.width / 2, y: outer.top + r.top + r.height / 2,
          disabled: el.disabled === true || el.getAttribute("aria-disabled") === "true",
          picker: el.tagName === "INPUT" && pickers.includes(el.type) ? el.type : null,
          select, options: select ? [...el.options].map((o) => norm(o.text)) : []};
}
"""

# What each field that is set, not typed into, takes.
_PICKERS = {
    "date": "a day as YYYY-MM-DD",
    "month": "a month as YYYY-MM",
    "week": "a week as YYYY-Www",
    "time": "a time as HH:MM",
    "datetime-local": "a day and time as YYYY-MM-DDTHH:MM",
    "color": "a colour as #rrggbb",
    "range": "a number within its range",
}
# Set the field the step found as its picker would, and say whether it took the value.
_SET = r"""
function (value) {
  const el = window.cliquetFound;
  el.value = value;
  if (el.value !== value) return false;
  el.dispatchEvent(new Event("input", {bubbles: true}));
  el.dispatchEvent(new Event("change", {bubbles: true}));
  return true;
}
"""

# Choose an option of the select the step found, by its text, as a person picking it would:
# the select changes and says so.
_CHOOSE = r"""
function (text) {
  const select = window.cliquetFound;
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  const option = [...select.options].find((o) => norm(o.text) === norm(text));
  if (!option) return false;
  select.focus();
  select.value = option.value;
  select.dispatchEvent(new Event("input", {bubbles: true}));
  select.dispatchEvent(new Event("change", {bubbles: true}));
  return true;
}
"""


@dataclass(frozen=True)
class ScriptResult:
    """What a script on a screen gave."""

    png: bytes
    calls: list[dict[str, Any]]
    error: str | None
    # How tall the screen is: more than the picture when the picture shows only its top.
    height: int = 0


def check_steps(steps: Any) -> list[dict[str, Any]]:
    """The steps, if they are well formed. Raises `StoreError` naming the bad one."""
    if not isinstance(steps, list):
        raise StoreError(f"steps must be a list. A step is {_SHAPES}.")
    if len(steps) > 30:
        raise StoreError("A script has at most 30 steps.")
    checked = []
    for number, step in enumerate(steps, 1):
        why = f"Step {number}: a step is {_SHAPES}."
        if not isinstance(step, dict):
            raise StoreError(why)
        keys = set(step)
        if keys == {"click"} and isinstance(step["click"], str):
            checked.append({"click": step["click"]})
        elif (
            keys == {"fill", "with"}
            and isinstance(step["fill"], str)
            and isinstance(step["with"], str)
        ):
            checked.append({"fill": step["fill"], "with": step["with"]})
        elif (
            keys == {"choose", "option"}
            and isinstance(step["choose"], str)
            and isinstance(step["option"], str)
        ):
            checked.append({"choose": step["choose"], "option": step["option"]})
        elif keys == {"wait"}:
            wait = step["wait"]
            if isinstance(wait, bool) or not isinstance(wait, int) or not 0 <= wait <= 5000:
                raise StoreError(f"Step {number}: wait is a whole number from 0 to 5000. {why}")
            checked.append({"wait": wait})
        else:
            raise StoreError(why)
    return checked


def run_script(
    base_url: str,
    view: str,
    steps: list[dict[str, Any]],
    width: int = 768,
    browser_path: str | None = None,
    no_sandbox: bool = False,
    theme: Theme = "light",
) -> ScriptResult:
    """Open a view in the preview, run the steps, and return what the screen shows and did.

    The view is drawn in `theme`. `browser_path` is the browser's path, found on this machine
    when it is None, and `no_sandbox` runs it without Chrome's own sandbox.
    """
    browser = Browser(browser_or_none_found(browser_path), no_sandbox)
    try:
        browser.open(f"{base_url}/?view={view}&width={width}&theme={theme}&shot&driver", width)
        error = None
        for number, step in enumerate(check_steps(steps), 1):
            error = _run_step(browser, number, step)
            if error is None:
                error = _settle(browser, number)
            if error is not None:
                break
        # The picture shows the whole screen from its top, wherever the steps scrolled it.
        browser.evaluate(
            "window.scrollTo(0, 0); document.getElementById('frame').contentWindow.scrollTo(0, 0)"
        )
        height = browser.fit(MAX_HEIGHT)
        png = browser.capture()
        calls = browser.evaluate("window.cliquetCalls") or []
        page_error = browser.evaluate("window.cliquetError || ''") or ""
        return ScriptResult(png, list(calls), error or page_error or None, height)
    finally:
        browser.close()


def _run_step(browser: Browser, number: int, step: dict[str, Any]) -> str | None:
    if "wait" in step:
        time.sleep(step["wait"] / 1000)
        return None
    if "click" in step:
        text = step["click"]
        found = _find(browser, "click", text)
        if found.get("error"):
            return f"Step {number}: {found['error']}"
        if not found.get("found"):
            return _nothing(number, text, found.get("seen", []))
        if found.get("disabled"):
            return f'Step {number}: "{text}" is disabled. Fill what it needs first.'
        browser.click(found["x"], found["y"])
        return None
    if "fill" in step:
        label, content = step["fill"], step["with"]
        found = _find(browser, "field", label)
        if found.get("error"):
            return f"Step {number}: {found['error']}"
        if not found.get("found"):
            return _nothing(number, label, found.get("seen", []))
        if found.get("disabled"):
            return f'Step {number}: "{label}" is disabled. Fill what it needs first.'
        if found.get("select"):
            return f'Step {number}: "{label}" is a select. Choose one of its options instead.'
        browser.click(found["x"], found["y"])
        if found.get("picker"):
            # A date, a time or a colour can't be typed into: it is set, as a picker sets it.
            if not browser.evaluate(f"({_SET})({json.dumps(content)})"):
                kind = found["picker"]
                return (
                    f'Step {number}: "{label}" is a {kind} field, which takes '
                    f"{_PICKERS[kind]}, not {json.dumps(content)}."
                )
            return None
        browser.evaluate(
            "(() => { const el = document.getElementById('frame').contentDocument.activeElement; "
            "if (el && el.select) el.select(); })()"
        )
        browser.type(content)
        return None
    label, option = step["choose"], step["option"]
    found = _find(browser, "field", label)
    if found.get("error"):
        return f"Step {number}: {found['error']}"
    if not found.get("found"):
        return _nothing(number, label, found.get("seen", []))
    if found.get("disabled"):
        return f'Step {number}: "{label}" is disabled. Fill what it needs first.'
    if not found.get("select"):
        return f'Step {number}: "{label}" isn\'t a select. Fill it instead.'
    picked = browser.evaluate(f"({_CHOOSE})({json.dumps(option)})")
    if not picked:
        return (
            f'Step {number}: "{label}" has no option "{option}". It has: '
            f"{_quoted(found.get('options', []))}."
        )
    return None


def _find(browser: Browser, kind: str, text: str) -> dict[str, Any]:
    pickers = json.dumps(list(_PICKERS))
    return browser.evaluate(f"({_FIND})({json.dumps(kind)}, {json.dumps(text)}, {pickers})") or {}


def _settle(browser: Browser, number: int) -> str | None:
    # Let the browser deliver the click or change before reading cliquetBusy. Under load,
    # the event handler can start after the input command has already returned.
    browser.evaluate("new Promise(r => requestAnimationFrame(() => setTimeout(r, 50)))")
    deadline = time.monotonic() + 15
    while True:
        state = (
            browser.evaluate(
                "({busy: window.cliquetBusy, "
                "quiet: Date.now() - Math.max(window.cliquetLast, window.cliquetDrawn || 0)})"
            )
            or {}
        )
        if state.get("busy") == 0 and state.get("quiet", 0) > 500:
            return None
        if time.monotonic() > deadline:
            return f"Step {number}: the screen was still working after 15 seconds."
        time.sleep(0.1)


def _nothing(number: int, text: str, seen: list[str]) -> str:
    """Say that a step found nothing: first what comes closest to its text, then the rest."""
    said = f'Step {number}: nothing on the screen reads "{text}".'
    want = " ".join(text.split()).lower()
    seen = list(dict.fromkeys(seen))
    close = list(dict.fromkeys([one for one in seen if want in one] + closest(want, seen, n=3)))
    if not close:
        return f"{said} It has: {_quoted(seen)}."
    rest = [one for one in seen if one not in close][:_REST]
    return f"{said} Did you mean {_quoted(close)}? It also has: {_quoted(rest)}."


def _quoted(seen: list[str]) -> str:
    return ", ".join(json.dumps(item) for item in seen) or "nothing"
