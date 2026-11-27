import html
import json
import logging
import math
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
import pytest
from conftest import HEADER
from mcp import Client
from mcp.client.extension import advertise
from pydantic import BaseModel, ConfigDict, create_model
from starlette.testclient import TestClient
from test_runtime import LOG_APP, git, make_app, skip_as_root

import cliquet
from cliquet import Caller, Screen, icons, screens, views, vocabulary
from cliquet.errors import AppError, Denied
from cliquet.host import host_page
from cliquet.mcp_server import AppServer
from cliquet.preview import preview_app
from cliquet.runtime import EVERYONE, Everyone, Runtime
from cliquet.screens import (
    MAX_DEPTH,
    MAX_NODES,
    check,
    finish,
    lay_out,
    parse,
    text,
)
from cliquet.worker import WrongArguments

OWNER = Caller("sam", "owner")
READER = Caller("alex", "reader")
CLAUDE = types.Implementation(name="claude-test", version="1.0")
# What a client that draws MCP Apps says about itself when it connects.
DRAWS = advertise(views.EXTENSION, {"mimeTypes": [views.MIME_TYPE]})

BOARD = (
    HEADER
    + """\
\"\"\"Tasks on a board, kept by an agent.\"\"\"

from pathlib import Path

from cliquet import ActionContext, App, BaseModel, QueryContext, Refused, Screen

board = App("board", files=["tasks.txt"], roles=["owner", "reader"], accent="#4338ca")


class Count(BaseModel):
    tasks: int


def _tasks() -> list[str]:
    return Path("tasks.txt").read_text().splitlines()


@board.action(roles=["owner"])
def add(ctx: ActionContext, task: str) -> None:
    \"\"\"Add a task.\"\"\"
    if not task:
        raise Refused("A task needs a name")
    with open("tasks.txt", "a") as f:
        f.write(task + "\\n")


@board.query(roles=["owner", "reader"])
def tasks(ctx: QueryContext, containing: str = "") -> list[str]:
    \"\"\"The tasks, optionally only those containing some text.\"\"\"
    return [t for t in _tasks() if containing in t]


@board.query(roles=["owner", "reader"])
def count(ctx: QueryContext) -> Count:
    \"\"\"How many tasks there are.\"\"\"
    return Count(tasks=len(_tasks()))


OVERVIEW = \"\"\"
<h2>{{ total }} tasks for {{ who }}</h2>
<form class="join" data-view>
  <input class="input join-item" name="containing" value="{{ containing }}" aria-label="Find">
  <button class="btn join-item">Find</button>
</form>
<ul class="list">
  {% for task in found %}<li class="list-row">{{ task }}</li>{% endfor %}
</ul>
{% if owner %}
<form class="layout-row" data-call="add" data-then='{"containing": ""}'>
  <label class="input layout-grow">Task <input name="task" required></label>
  <button class="btn btn-primary">Add</button>
</form>
{% endif %}
\"\"\"


@board.view(roles=["owner", "reader"])
def overview(ctx: QueryContext, containing: str = "") -> Screen:
    \"\"\"The tasks, with a search box and, for the owner, a form to add one.\"\"\"
    return Screen(
        OVERVIEW,
        args={"containing": containing},
        total=count(ctx).tasks,
        who=ctx.caller.id,
        containing=containing,
        found=tasks(ctx, containing),
        owner=ctx.caller.role == "owner",
    )


@board.view(roles=["owner"])
def settings(ctx: QueryContext) -> Screen:
    \"\"\"The owner's settings.\"\"\"
    return Screen("<p>Nothing to set yet.</p>")
"""
)


def board(tmp_path: Path, source: str = BOARD, owner: str | Everyone | None = None) -> Runtime:
    return make_app(tmp_path / "board", source, {"tasks.txt": "Buy tea\nCall Sam\n"}, owner=owner)


def broken(old: str, new: str) -> str:
    assert old in BOARD
    return BOARD.replace(old, new)


def problems(runtime: Runtime, caller: Caller = OWNER, view: str = "overview") -> str:
    with pytest.raises(AppError) as error:
        runtime.view(caller, view)
    return str(error.value)


# The runtime


def test_a_view_is_laid_out_on_the_current_data_for_the_caller(tmp_path: Path) -> None:
    runtime = board(tmp_path)
    screen = runtime.view(OWNER, "overview")
    assert screen["screen"] == 1 and screen["app"] == "board" and screen["view"] == "overview"
    assert "2 tasks for sam" in text(screen["body"])
    runtime.call(OWNER, "add", {"task": "Water plants"})
    assert "3 tasks for alex" in text(runtime.view(READER, "overview")["body"])
    runtime.close()


def test_a_view_takes_its_arguments_and_says_how_to_draw_it_again(tmp_path: Path) -> None:
    runtime = board(tmp_path)
    screen = runtime.view(OWNER, "overview", {"containing": "tea"})
    assert screen["arguments"] == {"containing": "tea"}
    assert "Buy tea" in text(screen["body"]) and "Call Sam" not in text(screen["body"])
    # The arguments left at their default aren't repeated.
    assert runtime.view(OWNER, "overview")["arguments"] == {}
    # An argument the view doesn't take is refused, as for a query.
    with pytest.raises(WrongArguments, match=r"app_transaction|extra"):
        runtime.view(OWNER, "overview", {"app_transaction": "t-old"})
    runtime.close()


def test_a_screen_names_the_tools_it_calls(tmp_path: Path) -> None:
    runtime = board(tmp_path)
    called = runtime.view(OWNER, "overview")["tools"]
    assert set(called) == {"overview", "add"}
    assert called["add"]["kind"] == "action"
    assert called["add"]["input"]["required"] == ["task"]
    assert called["overview"]["kind"] == "view"
    assert set(called["overview"]["input"]["properties"]) == {"containing"}
    # The reader's screen has no form to add, and so no `add`.
    assert set(runtime.view(READER, "overview")["tools"]) == {"overview"}
    runtime.close()


def test_a_screen_carries_the_app_s_accent(tmp_path: Path) -> None:
    runtime = board(tmp_path)
    screen = runtime.view(OWNER, "overview")
    assert screen["accent"] == "#4338ca"
    runtime.close()
    plain = board(tmp_path / "plain", BOARD.replace(', accent="#4338ca"', ""))
    screen = plain.view(OWNER, "overview")
    assert screen["accent"] is None
    plain.close()


# The hue of each tone's colour in OKLCH: daisyUI's status colours, the same in both themes.
# The neutral grey has none.
TONE_HUES = {"success": 163.223, "warning": 84.429, "error": 13.428, "info": 232.661}
# A colour reads as a tone when its hue is this close to the tone's, in degrees, and it has
# at least this chroma: a grey reads as none. The renderer's palette keeps clear of every tone
# by this rule, so a category never reads as a tone.
CLOSE_HUE = 30
LEAST_CHROMA = 0.06


def oklch(color: str) -> tuple[float, float, float]:
    """A `#rrggbb` colour's lightness, chroma and hue, in degrees, in OKLCH."""

    def linear(hex_pair: str) -> float:
        c = int(hex_pair, 16) / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(color[i : i + 2]) for i in (1, 3, 5))
    lms = (
        0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b,
        0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b,
        0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b,
    )
    l_, m_, s_ = (math.cbrt(x) for x in lms)
    lightness = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    b_ = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return lightness, math.hypot(a, b_), math.degrees(math.atan2(b_, a)) % 360


def close_tones(color: str) -> list[str]:
    """The tones a `#rrggbb` colour reads as, by its hue: none for a grey."""
    _, chroma, hue = oklch(color)
    if chroma < LEAST_CHROMA:
        return []
    apart = {tone: abs(hue - tone_hue) % 360 for tone, tone_hue in TONE_HUES.items()}
    return [tone for tone, gap in apart.items() if min(gap, 360 - gap) < CLOSE_HUE]


@pytest.mark.parametrize(
    ("colour", "tones"),
    [
        ("#be123c", ["error"]),
        ("#b45309", []),
        ("#d97706", ["warning"]),
        ("#0284c7", ["info"]),
        ("#0f766e", ["success"]),
        ("#7c3aed", []),
        ("#6b7280", []),
    ],
)
def test_a_colour_reads_as_the_tones_its_hue_is_close_to(colour: str, tones: list[str]) -> None:
    assert close_tones(colour) == tones


RENDERER = Path(cliquet.__file__).parent / "renderer"


def test_no_colour_of_the_renderer_s_palette_reads_as_a_tone() -> None:
    # So a series without a tone keeps its colour in every chart: none has a colour to avoid.
    styles = (RENDERER / "cliquet.css").read_text()
    themes = re.findall(r'\[data-theme="(?:light|dark)"\] \{(.*?)\}', styles, re.S)
    assert len(themes) == 2
    for theme in themes:
        palette = re.findall(r"--s-chart-(\d): (#[0-9a-f]{6})", theme)
        assert [slot for slot, _ in palette] == [str(slot) for slot in range(1, 8)]
        for slot, colour in palette:
            assert close_tones(colour) == [], (slot, colour)
    # The tones' hues are daisyUI's, the same in both its themes.
    daisy = next(RENDERER.glob("daisyui-*.css")).read_text()
    for tone in ("success", "warning", "error", "info"):
        hues = set(re.findall(rf"--color-{tone}:oklch\([\d.%]+ [\d.]+ ([\d.]+)\)", daisy))
        assert len(hues) == 1 and TONE_HUES[tone] == float(hues.pop())


def test_the_renderer_knows_the_tones_the_check_accepts() -> None:
    # A tone the renderer didn't know would be drawn as a category, by its name.
    script = (RENDERER / "screen.js").read_text()
    tones = re.search(r"const TONES = new Set\(\[(.*?)\]\);", script)
    assert tones
    assert sorted(re.findall(r'"(\w+)"', tones[1])) == sorted(screens._CHART_TONES)


def test_a_view_lays_out_only_what_the_caller_s_role_may_use(tmp_path: Path) -> None:
    runtime = board(tmp_path, broken("{% if owner %}", "{% if True %}"))
    found = problems(runtime, READER)
    assert "the role 'reader' may open this view but not use add" in found
    assert "with {% if %}" in found
    runtime.view(OWNER, "overview")
    runtime.close()


def test_a_view_follows_roles(tmp_path: Path) -> None:
    runtime = board(tmp_path)
    with pytest.raises(Denied):
        runtime.view(READER, "settings")
    runtime.close()


@skip_as_root
def test_a_view_reads_only(tmp_path: Path) -> None:
    reading = broken(
        "    return Screen(\n        OVERVIEW,",
        '    Path("tasks.txt").write_text("")\n    return Screen(\n        OVERVIEW,',
    )
    runtime = board(tmp_path / "writes", reading)
    with pytest.raises(Exception, match=r"Permission|Read-only|denied"):
        runtime.view(OWNER, "overview")
    runtime.close()


def test_check_views_lays_out_every_view_for_every_role(tmp_path: Path) -> None:
    runtime = board(tmp_path, broken("{% if owner %}", "{% if True %}"))
    found = [str(p) for p in runtime.check_views()]
    assert any(p.startswith("view overview, as reader:") and "add" in p for p in found)
    assert not any("as owner" in p for p in found)
    runtime.close()


# Data never becomes markup


HOSTILE = '<form data-call="app_revert"><button class="btn">Undo</button></form><b>x</b>'


def test_a_value_is_always_text(tmp_path: Path) -> None:
    runtime = board(tmp_path)
    runtime.call(OWNER, "add", {"task": HOSTILE})
    screen = runtime.view(READER, "overview")
    # The note is one text node, as written: no form, no button, nothing the reader may click.
    (items,) = [n for n in screen["body"] if isinstance(n, list) and n[0] == "ul"]
    assert ["li", {"class": "list-row"}, HOSTILE] in items[2:]
    assert "app_revert" not in screen["tools"]
    shown = runtime.view(READER, "overview", {"containing": '" data-call="add'})
    (form,) = [n for n in shown["body"] if isinstance(n, list) and n[0] == "form"][:1]
    assert form[1] == {"class": "join", "data-view": ""}
    runtime.close()


def test_a_template_can_t_let_a_value_become_markup(tmp_path: Path) -> None:
    for template, wanted in [
        ("<p class={{ x }}>a</p>", "sits inside a tag, outside quotes"),
        ("<p {{ x }}>a</p>", "sits inside a tag, outside quotes"),
        ("<{{ x }}>a</p>", "a tag's name must be written out"),
        ('<form data-call="{{ x }}"><button>a</button></form>', "must name the tool itself"),
        ("{% autoescape false %}{{ x }}{% endautoescape %}", "{% autoescape %} isn't allowed"),
        ('{% include "other.html" %}', "can't include, import or extend"),
        ("<p>{{ x | safe }}</p>", "the filter safe isn't available"),
        ("<p>{{ y }}</p>", "it uses y, which the view doesn't give"),
    ]:
        with pytest.raises(AppError, match=_escaped(wanted)):
            lay_out(Screen(template, x="hello"), "v", _NO_ARGUMENTS)
    from markupsafe import Markup

    with pytest.raises(AppError, match="the value x is markup"):
        lay_out(Screen("<p>{{ x }}</p>", x=[Markup("<b>hi</b>")]), "v", _NO_ARGUMENTS)
    # In quotes, a value is escaped; tojson writes JSON safe in either quote.
    laid = lay_out(
        Screen("<p title=\"{{ x }}\" data-tip='{{ x | tojson }}'>{{ x }}</p>", x="\"'><b>"),
        "v",
        _NO_ARGUMENTS,
    )
    assert laid["body"] == [["p", {"title": "\"'><b>", "data-tip": '"\\"\'><b>"'}, "\"'><b>"]]


def test_app_code_can_t_write_a_value_out_as_html(write_app: Any) -> None:
    from cliquet.manifest import build_manifest

    path = write_app(
        """
        \"\"\"Notes.\"\"\"
        from markupsafe import Markup

        from cliquet import App, QueryContext, Screen

        notes = App("notes", files=["notes.txt"])


        class Bold:
            def __html__(self) -> str:
                return "<b>note</b>"


        @notes.view()
        def board(ctx: QueryContext) -> Screen:
            \"\"\"The notes.\"\"\"
            return Screen("<p>{{ note }}</p>", note=Markup("<b>x</b>"))
        """
    )
    with pytest.raises(AppError) as error:
        build_manifest(path, module_name=f"markup_{path.stem}")
    found = str(error.value)
    assert "import markupsafe isn't allowed" in found
    assert "__html__ isn't allowed" in found


def _escaped(text: str) -> str:
    import re

    return re.escape(text)


_NO_ARGUMENTS = create_model("v_arguments")


def test_a_screen_s_args_must_draw_it_again() -> None:
    model = create_model("v_arguments", __config__=ConfigDict(extra="forbid"), month=(str, ""))
    with pytest.raises(AppError, match=r"Screen\(args=...\) doesn't fit"):
        lay_out(Screen("<p>x</p>", args={"day": 1}), "v", model)
    with pytest.raises(AppError, match="Return Screen"):
        lay_out("<p>x</p>", "v", model)


class Line(BaseModel):
    sku: str
    quantity: int


def test_tojson_writes_models_and_numbers() -> None:
    from decimal import Decimal

    laid = lay_out(
        Screen(
            '<form data-view data-args=\'{{ {"lines": lines, "total": total} | tojson }}\'>'
            "<button>Go</button></form>",
            lines=(Line(sku="A", quantity=2),),
            total=Decimal("8.50"),
        ),
        "v",
        _NO_ARGUMENTS,
    )
    form = laid["body"][0]
    assert json.loads(form[1]["data-args"]) == {
        "lines": [{"sku": "A", "quantity": 2}],
        "total": 8.5,
    }


# The check


MANIFEST: dict[str, Any] = {
    "name": "shop",
    "accent": None,
    "roles": ["owner", "staff"],
    "actions": [
        {
            "name": "record",
            "roles": ["owner", "staff"],
            "input_schema": {
                "type": "object",
                "properties": {"receipt": {"type": "string"}, "lines": {"type": "array"}},
                "required": ["receipt", "lines"],
            },
        },
        {"name": "price", "roles": ["owner"], "input_schema": {"type": "object"}},
    ],
    "queries": [{"name": "stock", "roles": ["owner", "staff"], "input_schema": {}}],
    "views": [
        {
            "name": "till",
            "roles": ["owner", "staff"],
            "input_schema": {"type": "object", "properties": {"lines": {"type": "array"}}},
        },
        {"name": "takings", "roles": ["owner"], "input_schema": {"type": "object"}},
    ],
}


def found(html: str, role: str | None = "staff") -> str:
    """Every problem the check finds in a screen's HTML, for a role."""
    body, wrong = parse(html)
    return "\n".join(wrong + check(body, {}, MANIFEST, role, "till")[0])


_TAB = '<input type="radio" name="t" class="tab" aria-label="A" checked>'
_CONTENT = '<div class="tab-content">a</div>'


def _tabs(inside: str, classes: str = "tabs tabs-border") -> str:
    """A tablist around these tabs and contents."""
    return f'<div role="tablist" class="{classes}">{inside}</div>'


def test_radio_tabs_as_daisyui_draws_them_pass() -> None:
    second = _TAB.replace('label="A" checked', 'label="B"')
    coloured = (
        '<div class="tab-content bg-base-100 border-base-300">'
        '<div class="layout-stack">b</div></div>'
    )
    label = '<label class="tab"><input type="radio" name="v" aria-label="C" checked>C</label>'
    links = (
        '<nav role="tablist" class="tabs tabs-border"><a role="tab" class="tab tab-active" '
        'data-view="takings">Takings</a></nav>'
    )
    # Text between a tab and its content is no element, so daisyUI's `+` skips it.
    first = _tabs(f"{_TAB}text{_CONTENT}{second}{coloured}", "tabs tabs-box layout-stack")
    assert found(first + _tabs(label + _CONTENT, "tabs tabs-lift") + links, "owner") == ""


def test_a_class_mix_that_reads_in_both_themes_passes() -> None:
    allowed = (
        '<span class="badge badge-soft">9:00</span><span class="badge badge-warning badge-soft">'
        'Low</span><span class="badge badge-neutral">New</span><span class="badge badge-ghost">'
        'Old</span><p class="text-base-content/70">a</p><p class="text-warning">b</p>'
        '<form data-view><button class="btn btn-soft btn-neutral">Show</button>'
        '<button class="btn btn-outline">Back</button></form>'
        '<div class="bg-neutral layout-stack"><p class="text-neutral-content">c</p></div>'
        '<span class="badge badge-error"><span class="text-error-content/80">d</span></span>'
        '<div class="alert alert-info" role="status"><span class="text-info-content">e</span>'
        "</div>"
    )
    assert found(allowed) == ""


_PICKER = (
    '<fieldset class="fieldset"><legend class="fieldset-legend">Lines</legend>'
    '<select class="select" name="lines"><option>a</option></select></fieldset>'
)


def test_a_view_form_whose_one_field_is_a_select_needs_no_button() -> None:
    assert found(f"<form data-view>{_PICKER}</form>") == ""
    assert "has no submit button" in found(f'<form data-call="record">{_PICKER}</form>')
    two = '<input class="input" name="receipt" aria-label="r">'
    assert "has no submit button" in found(f"<form data-view>{_PICKER}{two}</form>")
    unnamed = _PICKER.replace(' name="lines"', "")
    assert "has no submit button" in found(f"<form data-view>{unnamed}</form>")


def test_a_clean_screen_passes() -> None:
    assert (
        found(
            '<section class="card card-border"><div class="card-body layout-stack">'
            '<h3 class="card-title">Receipt</h3>'
            '<form class="layout-stack layout-gap-3" data-call="record" '
            "data-args='{\"lines\": []}' "
            "data-then='{\"lines\": []}'>"
            '<fieldset class="fieldset"><legend class="fieldset-legend">Receipt</legend>'
            '<input class="input" name="receipt" required></fieldset>'
            '<button class="btn btn-primary">Record</button></form>'
            '<a class="link" href="https://example.com/help">Help</a>'
            "</div></section>"
        )
        == ""
    )


@pytest.mark.parametrize(
    ("html", "wanted"),
    [
        ("<script>alert(1)</script>", "<script> isn't allowed"),
        ('<img src="x">', "<img> isn't allowed"),
        ("<svg></svg>", "<svg> isn't allowed"),
        ('<p onclick="x()">a</p>', "onclick isn't allowed"),
        ('<p style="color:red">a</p>', "style isn't allowed"),
        ('<a href="javascript:alert(1)">a</a>', "a link opens a web or mail address"),
        ('<a href="/apps/x">a</a>', "a link opens a web or mail address"),
        ('<p class="flex gap-4">a</p>', "Use layout-row (a row) or layout-stack"),
        ('<p class="mt-2">a</p>', "has no margins or padding"),
        ('<p class="lg:card-horizontal">a</p>', "never applies"),
        ('<p class="modal">a</p>', "doesn't work on a screen"),
        ('<p class="btn-primry">a</p>', "Did you mean 'btn-primary'?"),
        ('<p data-foo="1">a</p>', "data-foo isn't one of Cliquet's"),
        ("<button>a</button>", "is outside any form, so it does nothing"),
        ('<input name="x" aria-label="x">', "is outside any form"),
        (
            '<form data-call="record"><input name="receipt"><button>a</button></form>',
            "has no label",
        ),
        (
            '<form data-call="record" data-args=\'{"lines": []}\'><input name="qty" aria-label="q">'
            "<button>a</button></form>",
            "the field qty isn't an argument of record",
        ),
        ('<form data-call="record"><button>a</button></form>', "record needs lines and receipt"),
        ('<form data-call="price"><button>a</button></form>', "may open this view but not use"),
        ('<form data-call="nope"><button>a</button></form>', "the app has no action nope"),
        (
            '<form data-call="app_history"><button>a</button></form>',
            "the actions of its own app only",
        ),
        ('<form data-call="stock"><button>a</button></form>', "stock is a query"),
        (
            '<form data-call="takings"><button>a</button></form>',
            "is a view. Open it with data-view",
        ),
        ('<form data-view="takings"><button>a</button></form>', "may not open takings"),
        ('<form data-view data-args="{oops"><button>a</button></form>', "data-args isn't JSON"),
        (
            '<form data-call="record" data-args=\'{"lines": [], "receipt": "1"}\' '
            "data-then='{\"month\": 1}'><button>a</button></form>",
            "data-then sets month, which till doesn't take",
        ),
        (
            '<form data-view><button class="btn btn-primary">a</button>'
            '<button class="btn btn-primary">b</button></form>',
            "a form has one main button",
        ),
        ('<form data-view><input type="hidden" name="lines"></form>', "has no submit button"),
        ('<form data-view><button type="button">a</button></form>', "does nothing"),
        ("<form><button>a</button></form>", "a form needs data-call"),
        (
            '<form data-view><input type="file" name="lines" aria-label="f"><button>a</button>'
            "</form>",
            "an input of type 'file' isn't allowed",
        ),
        ("<table><tr><td>1</td><td>2</td><td>3</td><td>4</td></tr></table>", "a phone shows 3"),
        ('<p id="a">1</p><p id="a">2</p>', 'id="a" is given to 2 elements'),
        ("<div><p>a</div>", "</div> closes <div> while <p>"),
        ("<div>a", "is never closed"),
        ("<div/>", "write <div></div>"),
        ('<div data-chart=\'{"kind": "pie"}\'></div>', "a chart's kind is one of"),
        ('<p class="s-muted">a</p>', "isn't daisyUI's or Cliquet's"),
        ('<p class="text-right">a</p>', "Use layout-end"),
        ('<ul class="overflow-y-auto max-h-64">a</ul>', "Use layout-scroll,"),
        ('<p class="max-h-64">a</p>', "use layout-scroll to keep a long one to 20rem"),
        ('<p class="overflow-x-auto">a</p>', "Use layout-scroll-x"),
        ('<p class="truncate">a</p>', "Use layout-truncate."),
        ('<p class="w-64">a</p>', "takes the width of its column"),
        ('<p class="whitespace-pre-line">a</p>', "Use layout-lines."),
        ('<p class="whitespace-pre">a</p>', "Use <pre>, which keeps every space"),
        ('<p class="overflow-hidden">a</p>', "leave the class out, or use layout-truncate"),
        ('<p class="hidden sm:block">a</p>', "use layout-only-wide"),
        ('<p class="sm:layout-only-wide">a</p>', "Write layout-only-wide alone"),
        ('<p class="sr-only">a</p>', "Name it with aria-label instead"),
        ('<p class="absolute top-0">a</p>', "A screen places nothing by position"),
        ('<p class="hover:text-accent">a</p>', "takes no hover: variant"),
        (_tabs(_TAB + _CONTENT, "layout-row"), "is a tab outside a tablist with class tabs"),
        (_tabs(_TAB + "<div>a</div>"), "isn't followed straight by its"),
        (_tabs(_TAB) + _CONTENT, "isn't straight after its tab, inside a tablist"),
        (_tabs(_TAB + "<p>a</p>" + _CONTENT), "holds only each tab and its"),
        (
            _tabs(_TAB + _CONTENT + _TAB.replace('"t"', '"u"').replace(" checked", "") + _CONTENT),
            "don't share one name",
        ),
        (_tabs(_TAB + _CONTENT) + _tabs(_TAB + _CONTENT), '2 tablists share the radio name "t"'),
        (_tabs(_TAB.replace(" checked", "") + _CONTENT), "no tabs of <div"),
        (_tabs(_TAB + _CONTENT + _TAB + _CONTENT), "2 tabs of <div"),
        (
            _tabs(_TAB + '<div class="tab-content card md:layout-stack">a</div>'),
            "card and md:layout-stack set its display",
        ),
        (
            '<span class="badge badge-soft badge-neutral">9:00</span>',
            '<span class="badge badge-soft badge-neutral">: badge-soft with badge-neutral '
            "colours its text with daisyUI's neutral, near-black in both themes, so it can't be "
            'read in the dark theme. Write class="badge badge-soft" instead',
        ),
        ('<p class="md:badge-outline badge-neutral">a</p>', 'Write class="badge badge-ghost"'),
        ('<p class="text-neutral/70">a</p>', 'Write class="text-base-content/70"'),
        ('<a class="link link-neutral" href="https://example.com">a</a>', 'class="link" instead'),
        (
            '<p class="text-primary-content">a</p>',
            "text-primary-content is the colour for text on primary, so on another background "
            "it can't be read in one theme. Use it only inside bg-primary or a solid btn-primary",
        ),
        (
            '<div class="bg-info"><span class="badge badge-soft badge-error">'
            '<span class="sm:text-error-content/80">a</span></span></div>',
            "sm:text-error-content/80 is the colour for text on error",
        ),
        (
            '<form data-view><button class="btn btn-primary btn-link">'
            '<span class="text-primary-content">a</span></button></form>',
            "text-primary-content is the colour for text on primary",
        ),
    ],
)
def test_the_check_refuses_what_an_agent_gets_wrong(html: str, wanted: str) -> None:
    assert wanted in found(html)


def test_a_note_keeps_its_line_breaks_only_where_the_screen_asks() -> None:
    note = "Leave at   the door.\nCall before 9."
    body, _ = parse(f'<p class="layout-lines">\n  {note}\n</p><p>{note}</p>')
    assert body == [
        ["p", {"class": "layout-lines"}, "Leave at the door.\nCall before 9."],
        ["p", {}, "Leave at the door. Call before 9."],
    ]
    # The template's own line breaks, between the blocks of a note, draw no empty line.
    body, _ = parse('<div class="layout-lines">\n  <p>a</p>\n  <p>b</p>\n</div>')
    assert body == [["div", {"class": "layout-lines"}, ["p", {}, "a"], " ", ["p", {}, "b"]]]


def test_a_non_breaking_space_stays_as_the_template_wrote_it() -> None:
    body, _ = parse("<td>12&nbsp;kg</td><p>&nbsp;Total</p>")
    assert body == [["td", {}, "12\xa0kg"], ["p", {}, "\xa0Total"]]


def test_a_tool_that_takes_no_arguments_is_called_without_any() -> None:
    owner = "owner"
    assert found('<a data-view="takings">Takings</a>', owner) == ""
    assert found('<form data-call="price"><button>a</button></form>', owner) == ""
    form = '<form data-view="takings"><input name="x" aria-label="x"><button>a</button></form>'
    assert "which takes no arguments" in found(form, owner)


def test_a_field_is_labelled_in_any_of_the_usual_ways() -> None:
    form = "<form data-view>{}<button>Go</button></form>"
    for field in [
        '<label for="l">Lines</label><input id="l" name="lines">',
        '<label class="input">Lines <input name="lines"></label>',
        '<input name="lines" aria-label="Lines">',
        '<fieldset class="fieldset"><legend class="fieldset-legend">Lines</legend>'
        '<input class="input" name="lines"></fieldset>',
    ]:
        assert found(form.format(field)) == "", field
    assert "has no label" in found(form.format('<input name="lines" placeholder="Lines">'))


def test_a_chart_is_held_to_cliquet_s_format() -> None:
    def chart(**changes: Any) -> str:
        value = {
            "kind": "stacked-bar",
            "labels": ["Jun", "Jul"],
            "series": [{"name": "Food", "values": [1, 2.5]}, {"name": "Rent", "values": [9, 9]}],
            "y": {"format": "currency:EUR"},
            **changes,
        }
        return found(f"<div data-chart='{json.dumps(value)}'></div>")

    assert chart() == ""
    assert "one value per label" in chart(series=[{"name": "Food", "values": [1]}])
    assert "a donut has one series" in chart(kind="donut", y=None)
    assert "isn't a finite number" in chart(series=[{"name": "Food", "values": [1, "2"]}])
    assert "the y axis has the format 'euros'" in chart(y={"format": "euros"})
    assert "takes kind, labels, series" in chart(colors=["red"])
    assert 'and a "tone"' in chart(series=[{"name": "Food", "values": [1, 2], "color": "red"}])
    nan = '{"kind": "bar", "labels": ["a"], "series": [{"name": "s", "values": [NaN]}]}'
    assert "data-chart isn't JSON" in found(f"<div data-chart='{nan}'></div>")
    huge = nan.replace("NaN", "1e999")
    assert "isn't a finite number" in found(f"<div data-chart='{huge}'></div>")


def test_a_series_can_say_what_its_colour_means() -> None:
    def chart(kind: str, *tones: object) -> str:
        series = [{"name": f"S{n}", "values": [1], "tone": t} for n, t in enumerate(tones)]
        value = {"kind": kind, "labels": ["Jun"], "series": series}
        return found(f"<div data-chart='{json.dumps(value)}'></div>")

    assert chart("bar", "success", "error", "warning", "info", "neutral") == ""
    assert chart("line", "success", None) == ""
    wrong = chart("bar", "success", "red")
    assert "the series 'S1' has the tone 'red'" in wrong
    assert "success, error, warning, info, neutral" in wrong
    assert "has the tone ['error']" in chart("area", ["error"])
    assert "give its series no tone" in chart("donut", "success")
    assert chart("bar", None, None) == ""
    assert "two series of a chart have the tone 'error'" in chart("bar", "error", "error")


def test_a_line_can_be_drawn_over_bars_against_a_second_axis() -> None:
    def chart(kind: str, *series: dict[str, Any], **axes: Any) -> str:
        lines = [{"name": f"S{n}", "values": [1, 2], **one} for n, one in enumerate(series)]
        value = {"kind": kind, "labels": ["Jun", "Jul"], "series": lines, **axes}
        return found(f"<div data-chart='{json.dumps(value)}'></div>")

    margin = {"kind": "line", "axis": "y2"}
    rate = {"y": {"format": "currency:EUR"}, "y2": {"format": "percent"}}
    assert chart("bar", {}, margin, **rate) == ""
    assert chart("stacked-bar", {}, {}, {"kind": "line"}, margin, **rate) == ""
    assert chart("line", {}, {"axis": "y2"}, y2={"title": "Orders", "format": "integer"}) == ""
    # A y2 without a format of its own is a second scale in the same format.
    assert chart("line", {"axis": "y1"}, {"axis": "y2"}, y2={"min": 0.5, "max": 3}) == ""
    assert chart("bar", {}, margin) == ""
    assert "a series' kind is line" in chart("bar", {}, {"kind": "area"})
    assert "is a line already" in chart("line", {}, {"kind": "line"})
    assert "make the chart a bar chart" in chart("donut", {"kind": "line"})
    wrong = chart("bar", {}, {"axis": "y2"}, **rate)
    assert "the series 'S1' is on y2, a second axis that only a line takes" in wrong
    assert 'give it "kind": "line"' in wrong
    assert "leave its axis out." in chart("area", {}, {"axis": "y2"}, **rate)
    assert "the axis 'right'" in chart("line", {}, {"axis": "right"})
    # A format or an axis of None is none.
    assert chart("line", {"format": None}, {"axis": None}) == ""
    assert chart("bar", {"format": None, "axis": None, "tone": None}) == ""
    # A series' format goes where its chart takes one.
    wrong = chart("bar", {}, {"kind": "line", "format": "percent"})
    assert "the series 'S1' has a format: a series is in the format of its axis" in wrong
    assert '"axis": "y2", and give the chart "y2": {"format": ...}' in wrong
    wrong = chart("line", {"format": "percent"})
    assert '"axis": "y2", and give the chart "y2": {"format": ...}' in wrong
    for kind in ("bar", "stacked-bar", "area"):
        wrong = chart(kind, {"format": "percent"})
        assert 'the format of its axis: give the chart "y": {"format": ...}.' in wrong
        assert "y2" not in wrong
    wrong = chart("donut", {"format": "percent"})
    assert "a donut takes its format at the top of the chart" in wrong
    assert "every series of the chart is on y2" in chart("line", {"axis": "y2"})
    assert "a y2 axis and no series on it" in chart("bar", {}, **rate)
    assert "a chart's y2 is its axis" in chart("bar", {}, margin, y2="percent")
    assert "the y2 axis has the format 'euros'" in chart("bar", {}, margin, y2={"format": "euros"})
    assert '"kind": "line"' in chart("bar", {}, {"type": "line"})


def grid_html(value: object) -> str:
    """An element with this grid in its data-grid, as tojson writes it in a template."""
    return f'<div data-grid="{html.escape(json.dumps(value))}"></div>'


def test_a_grid_is_held_to_cliquet_s_format() -> None:
    columns = [
        {"field": "day", "header": "Day", "format": "date", "total": "max"},
        {"field": "paid_to", "header": "Paid to"},
        {"field": "amount", "format": "currency:EUR", "total": "sum"},
        {"field": "share", "format": "percent", "total": "avg"},
        {"field": "units", "format": "integer", "total": "min"},
        {"field": "note", "format": "text", "total": "count"},
    ]
    row = {"day": "2026-11-09", "paid_to": "Green Basket", "amount": 41.3, "share": 0.25}

    def grid(**changes: Any) -> str:
        value = {"columns": columns, "rows": [row, {"paid_to": True, "note": 7}], **changes}
        return found(grid_html(value))

    assert grid() == ""
    assert grid(rows=[]) == ""
    assert grid(rows=[{"day": None, "amount": None, "note": ""}]) == ""
    # The grid as a whole.
    assert "data-grid isn't JSON" in found("<div data-grid='{'></div>")
    assert "data-grid must be a JSON object" in found("<div data-grid='[]'></div>")
    assert (
        "a grid takes columns and rows, and optional tools, density and group_by, not search, "
        in (grid(title="a", search=True))
    )
    # Its tools and its density.
    assert grid(tools=[], density="normal") == ""
    assert grid(tools=["sort", "search", "filters", "pages"], density="compact") == ""
    for tools in ("sort", ["sort", 2], {"sort": True}):
        assert 'a grid\'s tools are a list of names, such as ["sort"]' in grid(tools=tools)
    unknown = grid(tools=["sort", "filter"])
    assert "a grid has no tool 'filter': give any of sort, search, filters and pages" in unknown
    assert "a grid's density is normal or compact, not 'dense'" in grid(density="dense")
    assert "not 1" in grid(density=1)
    assert "from 1 to 30 columns" in grid(columns=[])
    assert "from 1 to 30 columns" in grid(columns={"field": "day"})
    many = [{"field": f"c{n}"} for n in range(31)]
    assert "from 1 to 30 columns" in grid(columns=many, rows=[])
    assert grid(columns=many[:30], rows=[]) == ""
    # Its columns.
    assert '"header", "format", "total", and "tone" or "tones"' in grid(columns=[{"header": "Day"}])
    assert '"header", "format", "total", and "tone" or "tones"' in grid(
        columns=[{"field": "a", "width": 80}]
    )
    assert '"header", "format", "total", and "tone" or "tones"' in grid(columns=["day"])
    assert "letters, digits and _, such as paid_to, not 'paid to'" in grid(
        columns=[{"field": "paid to"}]
    )
    assert "not '1st'" in grid(columns=[{"field": "1st"}])
    assert "two columns of a grid show the field 'day'" in grid(
        columns=[{"field": "day"}, {"field": "day", "format": "date"}]
    )
    assert "header that isn't text" in grid(columns=[{"field": "day", "header": 3}], rows=[])
    assert "header that isn't text" in grid(columns=[{"field": "day", "header": " "}], rows=[])
    euros = grid(columns=[{"field": "a", "format": "euros"}], rows=[])
    assert "the column 'a' has the format 'euros'" in euros and "currency:EUR" in euros
    assert "format 'currency:euro'" in grid(columns=[{"field": "a", "format": "currency:euro"}])
    # A number format may fix its digits, as Angular's digitsInfo writes them.
    for form in ("currency:EUR:1.0-0", "number:1.2-2", "percent:1.1-1", "number:9.6-6"):
        assert grid(columns=[{"field": "a", "format": form, "total": "sum"}], rows=[]) == ""
    for form in (
        "currency:EUR:0-0",
        "number:1.0",
        "number:0.0-0",
        "percent:1.3-2",
        "number:1.7-7",
        "number:1.0-0 ",
        "number:",
        "currency:EUR:2",
    ):
        unread = grid(columns=[{"field": "a", "format": form}], rows=[])
        assert f"the column 'a' has the format {form!r}, whose digits aren't readable" in unread
        assert "such as" in unread and ":1.0-0 for whole numbers" in unread
    # A number column colours its text by its sign, and a text column its states as badges.
    tones = {
        "Paid": "success",
        "Late": "error",
        "Draft": "neutral",
        "Due": "warning",
        "Sent": "info",
    }
    for tone in ("signed", "negative"):
        assert grid(columns=[{"field": "a", "format": "number", "tone": tone}], rows=[]) == ""
    assert grid(columns=[{"field": "a", "tones": tones}], rows=[]) == ""
    assert grid(columns=[{"field": "a", "format": "text", "tones": {}}], rows=[]) == ""
    both = grid(columns=[{"field": "a", "format": "number", "tone": "signed", "tones": {}}])
    assert "the column 'a' has a tone and tones" in both and "not both" in both
    for form in ("text", "date"):
        toned = grid(columns=[{"field": "a", "format": form, "tone": "signed"}], rows=[])
        assert "and only a number column takes a tone: colour a state with tones" in toned
    assert "the column 'a' holds text, and only" in grid(columns=[{"field": "a", "tone": "x"}])
    red = grid(columns=[{"field": "a", "format": "integer", "tone": "red"}], rows=[])
    assert "the column 'a' has the tone 'red': give signed" in red
    toned = grid(columns=[{"field": "a", "format": "currency:EUR", "tones": tones}], rows=[])
    assert "and only a text column takes tones: colour an amount by its sign" in toned
    toned = grid(columns=[{"field": "a", "format": "month", "tones": tones}], rows=[])
    assert "holds dates, and only a text column takes tones: give them to a text column" in toned
    for wrong in (["success"], "success", None):
        assert "tones that aren't an object" in grid(columns=[{"field": "a", "tones": wrong}])
    unknown = grid(columns=[{"field": "a", "tones": {"Late": "red"}}], rows=[])
    assert (
        "the column 'a' gives 'Late' the tone 'red': give one of success, error, warning, info, "
        "neutral." in unknown
    )
    integer = grid(columns=[{"field": "a", "format": "integer:1.0-0"}], rows=[])
    assert "integer takes no digits: it is number:1.0-0 already" in integer
    assert "the total 'total': give sum, avg" in grid(
        columns=[{"field": "a", "format": "number", "total": "total"}]
    )
    assert "holds text, and only a number column takes the total sum" in grid(
        columns=[{"field": "a", "total": "sum"}], rows=[]
    )
    assert "holds dates, and only a number column takes the total avg" in grid(
        columns=[{"field": "a", "format": "date", "total": "avg"}], rows=[]
    )
    assert "only a number or date column takes the total max" in grid(
        columns=[{"field": "a", "total": "max"}], rows=[]
    )
    # Its rows.
    assert "a grid's rows are a list of objects" in grid(rows={"day": "2026-11-09"})
    assert "a grid's rows are a list of objects" in grid(rows=None)
    assert grid(rows=[{}] * 5000) == ""
    assert "a grid has 5,001 rows, and shows 5,000 at most" in grid(rows=[{}] * 5001)
    assert "the row 2 of a grid isn't an object" in grid(rows=[row, ["Green Basket"]])
    assert "the row 1 of a grid has the field 'colour', which no column shows" in grid(
        rows=[{"colour": "red"}]
    )
    assert "the column 'amount' holds numbers, and the row 1 has '41.30'" in grid(
        rows=[{"amount": "41.30"}]
    )
    assert "holds numbers, and the row 2 has True" in grid(rows=[row, {"units": True}])
    assert "the column 'day' holds dates, and the row 1 has '9 Nov'" in grid(
        rows=[{"day": "9 Nov"}]
    )
    assert "as YYYY-MM-DD" in grid(rows=[{"day": "2026-02-30"}])
    assert "as YYYY-MM-DD" in grid(rows=[{"day": 20261109}])
    assert "a cell is text, a number, true, false or null" in grid(rows=[{"note": [1]}])
    assert "a cell is text, a number" in grid(rows=[{"paid_to": {"name": "Basket"}}])
    huge = '{"columns": [{"field": "a", "format": "number"}], "rows": [{"a": 1e999}]}'
    assert "isn't a finite number" in found(f"<div data-grid='{huge}'></div>")
    nan = huge.replace("1e999", "NaN")
    assert "data-grid isn't JSON" in found(f"<div data-grid='{nan}'></div>")
    # The element it is on.
    full = grid_html({"columns": columns, "rows": []}).replace("></div>", "><p>Rows</p></div>")
    assert "an element with data-grid must be empty" in found(full)
    assert "data-grid on a div" in found('<span data-grid="{}"></span>')
    both = grid_html({"columns": columns, "rows": []}).replace(
        "<div ", """<div data-chart='{"kind": "bar"}' """
    )
    assert "data-chart or data-grid, not both" in found(both)


def test_a_grid_groups_its_rows_by_a_field_no_column_shows() -> None:
    columns = [{"field": "line"}, {"field": "amount", "format": "currency:EUR", "total": "sum"}]
    rows = [
        {"section": "Income", "line": "Salary", "amount": 3000},
        {"section": "Living costs", "line": "Essentials", "amount": None},
        {"section": "Living costs", "line": "Rent", "amount": 900, "indent": 1},
        {"section": "Living costs", "line": "Tea", "amount": 12, "indent": 2},
        {"section": None, "line": "Gift", "amount": 50, "indent": 0},
        {"line": "Refund", "amount": 20, "indent": None},
    ]

    def grid(**changes: Any) -> str:
        value = {"columns": columns, "rows": rows, "group_by": "section", **changes}
        return found(grid_html(value))

    # The group field needs no column, and a row with none, or null, is of no group.
    assert grid() == ""
    assert grid(rows=[]) == ""
    assert grid(group_by=None) != ""
    assert "the grid groups its rows by 'part', which no row has" in grid(group_by="part")
    assert "group_by is the field of its rows that names their group" in grid(group_by=["a"])
    assert "not 'a b'" in grid(group_by="a b")
    wrong = grid(columns=[*columns, {"field": "section"}])
    assert "the grid groups its rows by 'section' and shows it as a column too" in wrong
    # A group's title is text.
    assert "the row 1 has 2026 in section, the field the grid groups by" in grid(
        rows=[{"section": 2026}]
    )
    assert "give the title of its group as text" in grid(rows=[{"section": True}])
    # An indent is 0, 1 or 2, in a grouped grid only, which keeps the field for it.
    for indent in (3, -1, True, 1.0, "1"):
        wrong = grid(rows=[{"section": "Income", "indent": indent}])
        assert f"the row 1 has the indent {indent!r}: give 1 or 2" in wrong
    assert 'a grouped grid keeps "indent"' in grid(group_by="indent")
    assert 'a grouped grid keeps "indent"' in grid(columns=[*columns, {"field": "indent"}])
    ungrouped = found(grid_html({"columns": columns, "rows": [{"line": "Rent", "indent": 1}]}))
    assert "the row 1 of a grid has an indent, which only a grid with group_by takes" in ungrouped
    # A grid that groups nothing keeps a column of indents it had.
    shown = [{"field": "indent", "format": "integer"}]
    assert found(grid_html({"columns": shown, "rows": [{"indent": 4}]})) == ""


def test_a_format_belongs_to_the_axis_it_describes() -> None:
    def chart(kind: str = "bar", **changes: Any) -> str:
        value = {"kind": kind, "labels": ["Jun", "Jul"], **changes}
        value["series"] = [{"name": "Spent", "values": [1, 2]}]
        if kind == "scatter":
            del value["labels"]
            value["series"] = [{"name": "Spent", "points": [{"x": 1, "y": 2}]}]
        return found(f"<div data-chart='{json.dumps(value)}'></div>")

    assert chart(y={"format": "percent"}) == ""
    assert chart(y1={"title": "Spent", "format": "currency:EUR", "max": 10}) == ""
    assert chart("donut", format="currency:EUR") == ""
    assert chart("donut", format="currency:EUR:1.0-0") == ""
    assert chart(y1={"format": "percent:1.1-1"}) == ""
    assert "whose digits aren't readable" in chart("donut", format="currency:EUR:1.2-0")
    assert "the y axis has the format 'number:1-2', whose digits" in chart(
        y={"format": "number:1-2"}
    )
    assert "the donut has the format 'euros'" in chart("donut", format="euros")
    wrong = chart(format="percent")
    assert "the chart has a format at the top, which only a donut takes" in wrong
    assert 'as "y": {"format": "percent"}.' in wrong
    assert 'or "x": {"format": "integer"}.' in chart("scatter", format="integer")
    # At the top of a chart with labels, a label's format belongs to its x axis, which holds
    # them, and a scatter has no labels at all.
    wrong = chart(format="month:short")
    assert 'as "x": {"format": "month:short"}.' in wrong
    assert "a scatter's points are numbers" in chart("scatter", format="date:long")
    both = chart(y={"format": "percent"}, y1={"format": "percent"})
    assert "both y and y1, which are the same axis: give one" in both
    assert "a bar chart's y1 axis starts at zero" in chart(y1={"min": 1})
    assert "a scatter has one y axis: leave its y2 out" in chart("scatter", y2={})
    assert "a donut has no axes: leave its y2 out" in chart("donut", y2={})


def test_a_scatter_draws_points_each_with_its_own_x_and_y() -> None:
    def chart(**changes: Any) -> str:
        value = {
            "kind": "scatter",
            "x": {"title": "Units sold a month", "format": "integer"},
            "y": {"title": "Margin", "min": 0, "format": "percent"},
            "series": [
                {"name": "Green tea", "points": [{"x": 140, "y": 0.42, "label": "Sencha"}]},
                {"name": "Black tea", "points": [{"x": 90, "y": 0.3}], "tone": "neutral"},
            ],
            **changes,
        }
        return found(f"<div data-chart='{json.dumps(value)}'></div>")

    def series(**one: Any) -> list[dict[str, Any]]:
        return [{"name": "Green tea", "points": [{"x": 1, "y": 2}], **one}]

    assert chart() == ""
    assert chart(series=series(points=[])) == ""
    assert "a scatter has no labels" in chart(labels=["Jun"])
    assert "from 1 to 12 series" in chart(series=[])
    wrong = chart(series=[{"name": "Green tea", "values": [1, 2]}])
    assert "has values, and a scatter's series have points instead" in wrong
    assert "every series of a scatter is drawn as points" in chart(series=series(kind="line"))
    assert "in the formats of its axes" in chart(series=series(format="percent"))
    assert "takes name, points and tone, not axis" in chart(series=series(axis="y2"))
    assert "takes name, points and tone, not colour" in chart(series=series(colour="red"))
    assert "has no points" in chart(series=[{"name": "Green tea"}])
    assert "keeps its colour across the app" in chart(series=[{"points": []}])
    for point in [{"x": 1}, {"x": 1, "y": "2"}, {"x": 1, "y": True}, [1, 2]]:
        assert "x and y are finite numbers" in chart(series=series(points=[point])), point
    assert "x and y are finite numbers" in chart(
        series=series(points=[{"x": 1, "y": 2, "label": 3}])
    )
    assert "x and y are finite numbers" in chart(
        series=series(points=[{"x": 1, "y": 2, "size": 3}])
    )
    many = [{"x": n, "y": n} for n in range(600)]
    two = [{"name": "A", "points": many}, {"name": "B", "points": many}]
    assert "at most 1,000 points in all, and this one has 1,200" in chart(series=two)
    assert "have the same name" in chart(series=[*series(), *series()])
    assert "has the tone 'red'" in chart(series=series(tone="red"))


def test_a_chart_s_axes_take_a_title_and_a_span() -> None:
    def chart(kind: str = "line", **axes: Any) -> str:
        value: dict[str, Any] = {"kind": kind, "labels": ["Jun", "Jul"], **axes}
        value["series"] = [{"name": "Uptime", "values": [0.99, 0.995]}]
        if kind == "scatter":
            del value["labels"]
            value["series"] = [{"name": "Uptime", "points": [{"x": 1, "y": 0.99}]}]
        return found(f"<div data-chart='{json.dumps(value)}'></div>")

    assert chart(x={"title": "Month"}, y={"title": "Uptime", "min": 0.98, "max": 1}) == ""
    assert chart("bar", y={"min": -100, "max": 100}) == ""
    assert chart("bar", y={"min": 0}) == ""
    assert chart("scatter", x={"min": 0, "max": 10, "format": "currency:EUR"}) == ""
    assert "a donut has no axes" in chart("donut", y={"title": "Spent"})
    assert "a chart's y is its axis" in chart(y=[0, 1])
    assert "takes a title and a label's format: leave out its min" in chart(x={"min": 0})
    assert "takes a title and a label's format" in chart(x={"format": "integer", "min": 0})
    assert chart(y={"format": "integer"}) == ""
    assert "takes title, min, max and format, not colour" in chart(y={"colour": "red"})
    assert "takes title, min, max and format, not step" in chart("scatter", x={"step": 1})
    assert "the y axis's title is text" in chart(y={"title": 3})
    assert "min and max are finite numbers" in chart(y={"min": "0"})
    wrong = chart(y={"min": 1, "max": 0.5})
    assert "the y axis has a min of 1 and a max of 0.5: give a min below the max" in wrong
    assert "a min below the max" in chart("scatter", x={"min": 2, "max": 2})
    for kind in ("bar", "stacked-bar", "area"):
        wrong = chart(kind, y={"min": 0.9})
        assert f"a {kind} chart's y axis starts at zero" in wrong
        assert "or draw a line chart" in wrong
    assert "the x axis has the format 'euros'" in chart("scatter", x={"format": "euros"})


def test_a_chart_writes_its_labels_in_a_format() -> None:
    def chart(labels: list[str], **axes: Any) -> str:
        value: dict[str, Any] = {
            "kind": "bar",
            "labels": labels,
            "series": [{"name": "Spent", "values": [1] * len(labels)}],
            **axes,
        }
        return found(f"<div data-chart='{json.dumps(value)}'></div>")

    months = [str(month) for month in range(1, 13)]
    weekdays = [str(day) for day in range(1, 8)]
    days = ["2026-11-03", "2026-12-01"]
    # Each kind of label, and each style, takes the labels it reads. A bare name is short.
    for form in ("month", "month:short", "month:long"):
        assert chart(months, x={"title": "Month", "format": form}) == "", form
    for form in ("weekday", "weekday:short", "weekday:long"):
        assert chart(weekdays, x={"format": form}) == "", form
    for form in ("date", "date:short", "date:long"):
        assert chart(days, x={"format": form}) == "", form
    # A leading zero is the month it writes, and its number is in plain digits: another
    # script's, as Arabic-Indic ones, is refused.
    assert chart(["03", "04"], x={"format": "month:short"}) == ""
    assert "isn't a month" in chart(["\u0661\u0661"], x={"format": "month:short"})
    # Without a format of its own, a label reads as the view wrote it.
    assert chart(["Jun", "Jul"]) == ""
    # A label the format doesn't read is refused, naming the format and the label.
    wrong = chart(["Jun"], x={"format": "month:short"})
    assert "the label 'Jun' isn't a month" in wrong
    assert "the x axis's format is 'month:short'" in wrong
    assert "a whole number from 1 to 12, such as 11 for November" in wrong
    assert "the label '13' isn't a month" in chart(["1", "13"], x={"format": "month:short"})
    assert "the label '0' isn't a month" in chart(["0"], x={"format": "month:long"})
    assert "the label '8' isn't a weekday" in chart(["1", "8"], x={"format": "weekday"})
    # A day the calendar has no 30th of isn't a date.
    wrong = chart(["2026-02-30"], x={"format": "date:short"})
    assert "the label '2026-02-30' isn't a date" in wrong
    assert "a date as YYYY-MM-DD, such as 2026-11-03" in wrong
    # The labels are checked before the series, so the format and the label are named.
    assert "isn't a month" in chart(["Jun"], series=[], x={"format": "month:short"})
    # A chart with labels has an x for its labels: a title, and their format.
    wrong = chart(["Jun"], x={"format": "integer"})
    assert "the x axis has the format 'integer'" in wrong
    assert "give the format of its labels, date, month or weekday" in wrong
    assert "leave out its min" in chart(["Jun"], x={"min": 0})
    assert "leave out its max" in chart(["Jun"], x={"max": 3})
    # A scatter's x holds numbers, and so does every y: a label's format isn't a number's.
    wrong = chart(["Jun"], y={"format": "weekday:long"})
    assert "the y axis holds numbers" in wrong
    assert "'weekday:long' is the format of a label" in wrong
    scatter = {
        "kind": "scatter",
        "x": {"title": "Units sold", "format": "month:short"},
        "series": [{"name": "Green tea", "points": [{"x": 1, "y": 2}]}],
    }
    wrong = found(f"<div data-chart='{json.dumps(scatter)}'></div>")
    assert "the x axis holds numbers" in wrong
    assert "'month:short' is the format of a label" in wrong


def test_a_grid_column_writes_its_dates_in_a_label_s_format() -> None:
    row = {"day": "2026-11-03"}

    def grid(columns: list[dict[str, Any]], rows: list[dict[str, Any]] | None = None) -> str:
        return found(grid_html({"columns": columns, "rows": [row] if rows is None else rows}))

    # Each of a label's formats is a date column: its cells are the dates they are.
    for form in ("date:short", "date:long", "month", "month:short", "month:long", "weekday"):
        assert grid([{"field": "day", "format": form}]) == "", form
    # A month column holds dates, so it sorts, filters and totals by them.
    assert grid([{"field": "day", "format": "month:short", "total": "max"}]) == ""
    assert "holds dates, and only a number column takes the total avg" in grid(
        [{"field": "day", "format": "weekday:long", "total": "avg"}]
    )
    assert "the column 'day' holds dates" in grid(
        [{"field": "day", "format": "weekday:long"}], [{"day": "Mon"}]
    )
    wrong = grid([{"field": "day", "format": "month:short"}], [{"day": "2026-02-30"}])
    assert "as YYYY-MM-DD" in wrong
    # A format that is neither a label's nor a number's is refused, with what fits.
    wrong = grid([{"field": "day", "format": "monthly"}])
    assert "the column 'day' has the format 'monthly'" in wrong
    assert "a label's format, such as date, month:short or weekday:long" in wrong


def test_a_wide_table_scrolls_with_its_rows_names_pinned() -> None:
    head = "<thead><tr><th></th>" + "<th>Jan</th>" * 6 + "</tr></thead>"
    row = "<tr><th>Food</th>" + "<td>1</td>" * 6 + "</tr>"
    table = f'<table class="table table-pin-cols">{head}<tbody>{row}</tbody></table>'
    scrolls = '<div class="layout-scroll-x">{}</div>'
    assert found(scrolls.format(table)) == ""
    # A long and wide table scrolls both ways, its header and its names pinned.
    pinned = table.replace("table-pin-cols", "table-pin-cols table-pin-rows")
    assert found(f'<div class="layout-scroll layout-scroll-x">{pinned}</div>') == ""
    alone = found(table)
    assert "a table has 7 columns, and a phone shows 3" in alone
    assert '<div class="layout-scroll-x">' in alone
    assert "table-pin-cols" not in alone
    assert "keep it to a name and one or two values" in alone
    unpinned = table.replace(" table-pin-cols", "")
    assert "give it daisyUI's table-pin-cols" in found(scrolls.format(unpinned))
    unnamed = table.replace("<th>Food</th>", "<td>Food</td>")
    assert "start each row with a <th>" in found(scrolls.format(unnamed))


VIEW_FORM = '<form data-view class="layout-row">{}</form>'


def test_an_icon_is_a_name_from_cliquet_s_set() -> None:
    clean = [
        '<button class="btn btn-primary"><span data-icon="plus"></span> Ajouter</button>',
        '<button class="btn btn-square btn-ghost" aria-label="Close">'
        '<span data-icon="x"></span></button>',
        '<a class="btn" data-view><span data-icon="refresh-cw"></span> Refresh</a>',
    ]
    assert found(VIEW_FORM.format("".join(clean))) == ""
    assert found('<h2><span data-icon="package"></span> Stock</h2>') == ""
    row = '<li class="list-row"><span data-icon="tag"></span>Tea</li>'
    assert found(f'<ul class="list">{row}</ul>') == ""
    assert "Did you mean plus" in found('<h2><span data-icon="plsu"></span> Add</h2>')
    assert "Find one with find_icons" in found('<span data-icon=""></span>')
    assert "must be empty" in found('<span data-icon="plus">+</span>')
    assert "data-icon on a span" in found('<i data-icon="plus"></i>')


def test_a_screen_of_wrong_icon_names_stays_quick_to_check() -> None:
    names = [f"{name}z" for name in list(icons.tags())[:40]]
    spans = "".join(f'<span data-icon="{name}"></span>' for name in names)
    problems = found(f"<p>{spans}{spans}</p>")
    assert problems.count("isn't one of Cliquet's icons") == 40
    assert problems.count("Did you mean") == 20


def test_an_icon_alone_in_a_button_or_a_link_needs_a_name() -> None:
    alone = found(VIEW_FORM.format('<button class="btn btn-square"><span data-icon="x"></span>'))
    assert "shows only an icon" in alone and 'aria-label="Close"' in alone
    link = found('<a class="btn" data-view><span data-icon="refresh-cw"></span></a>')
    assert "shows only an icon" in link
    named = '<button class="btn" aria-labelledby="t"><span data-icon="x"></span></button>'
    assert found(VIEW_FORM.format(named) + '<p id="t">Close</p>') == ""
    # A button with words of its own, or with no icon, has its name already.
    assert found(VIEW_FORM.format('<button class="btn">Go</button>')) == ""


def test_an_icon_named_by_a_value_is_checked_when_the_view_is_laid_out() -> None:
    template = '<h2><span data-icon="{{ icon }}"></span> {{ title }}</h2>'
    arguments = create_model("Nothing")
    laid = lay_out(Screen(template, icon="banana", title="Fruit"), "till", arguments)
    assert check(laid["body"], {}, MANIFEST, "staff", "till")[0] == []
    laid = lay_out(Screen(template, icon="bananna", title="Fruit"), "till", arguments)
    (problem,) = check(laid["body"], {}, MANIFEST, "staff", "till")[0]
    assert 'data-icon="bananna"' in problem and "banana" in problem


def test_a_screen_s_icons_are_drawn_from_cliquet_s_copy() -> None:
    body, _ = parse('<h2><span data-icon="plus"></span> Add</h2><p><span data-icon="x"></span></p>')
    # A sandbox may answer drawings of its own, and an <svg>: neither is ever drawn.
    made_up = {"arguments": {}, "body": body, "icons": {"plus": [["image", {"href": "x"}]]}}
    screen = finish(made_up, MANIFEST, "staff", "till")
    assert screen["icons"] == {"plus": icons.drawings()["plus"], "x": icons.drawings()["x"]}
    assert screen["icons"]["plus"] == [["path", {"d": "M5 12h14"}], ["path", {"d": "M12 5v14"}]]
    assert finish({"arguments": {}, "body": ["Hi"]}, MANIFEST, "staff", "till")["icons"] == {}
    svg = {"arguments": {}, "body": [["svg", {}, ["path", {"d": "M0 0"}]]]}
    with pytest.raises(AppError, match="<svg> isn't allowed: a screen draws no SVG of its own"):
        finish(svg, MANIFEST, "staff", "till")


def test_a_screen_with_a_grid_gets_the_icons_its_grids_draw() -> None:
    grid = grid_html({"columns": [{"field": "day"}], "rows": [{"day": "Monday"}]})
    body, _ = parse(f'<section><span data-icon="plus"></span>{grid}</section>')
    screen = finish({"arguments": {}, "body": body}, MANIFEST, "staff", "till")
    assert sorted(screen["icons"]) == ["funnel", "plus", "search", "x"]
    assert screen["icons"]["funnel"] == icons.drawings()["funnel"]
    # A screen without a grid gets only the icons it names.
    body, _ = parse('<p><span data-icon="plus"></span></p>')
    assert list(finish({"arguments": {}, "body": body}, MANIFEST, "staff", "till")["icons"]) == [
        "plus"
    ]


def test_the_renderer_page_holds_no_icon() -> None:
    page = views.renderer_html()
    # One drawing of Lucide's would be a path of its own: none is in the page.
    assert "m6 6 12 12" not in page and "M5 12h14" not in page
    # Lucide's drawings alone weigh 450 kB: the page stays well short of holding them.
    assert len(page) < 1_100_000
    assert '"icon_parts":{"path":["d","fill"]' in page


def test_every_drawing_is_made_of_what_the_renderer_builds() -> None:
    assert set(icons.drawings()) == set(icons.tags())
    for drawing in icons.drawings().values():
        for element, attributes in drawing:
            assert set(attributes) <= icons.DRAWN[element] | icons.SHARED
            assert attributes.get("fill", "currentColor") == "currentColor"


def test_tojson_refuses_what_a_browser_can_t_read() -> None:
    with pytest.raises(AppError, match="tojson can't write a number that isn't finite"):
        lay_out(Screen("<p data-tip='{{ x | tojson }}'>a</p>", x=float("nan")), "v", _NO_ARGUMENTS)


def test_a_raw_block_holds_no_value() -> None:
    from cliquet.screens import template_problems

    assert template_problems('<code {% raw %}title="{{ x }}"{% endraw %}>a</code>') == ()
    assert template_problems("<p class={{ x }}>{% raw %}{{ y }}{% endraw %}</p>")


def test_only_a_few_unknown_classes_get_a_hint() -> None:
    # A hint searches every class: a screen with thousands of mistakes stays quick to check.
    classes = " ".join(f"btn-primarx{number}" for number in range(40))
    problems = found(f'<p class="{classes}">a</p>')
    assert problems.count("Did you mean") == 20
    assert problems.count("isn't daisyUI's or Cliquet's") == 40


def test_the_server_checks_what_a_sandbox_answered() -> None:
    def deep(depth: int) -> list[Any]:
        node: list[Any] = ["div", {}]
        for _ in range(depth):
            node = ["div", {}, node]
        return [node]

    with pytest.raises(AppError, match="nests more than"):
        finish({"arguments": {}, "body": deep(MAX_DEPTH + 5)}, MANIFEST, "staff", "till")
    wide = [["p", {}, "x"] for _ in range(MAX_NODES)]
    with pytest.raises(AppError, match="more than 20,000 parts"):
        finish({"arguments": {}, "body": wide}, MANIFEST, "staff", "till")
    with pytest.raises(AppError, match=r"isn't \[tag"):
        finish({"arguments": {}, "body": [["p", {"class": 1}]]}, MANIFEST, "staff", "till")
    with pytest.raises(AppError, match="onerror isn't allowed"):
        finish({"arguments": {}, "body": [["p", {"onerror": "x"}]]}, MANIFEST, "staff", "till")
    # Only the body and the arguments are read: the rest is the server's own.
    made_up = {
        "arguments": {},
        "body": ["Hello"],
        "tools": {"price": {"kind": "action"}},
        "accent": "#ff0000",
        "language": "fr",
        "relay": {"x": {}},
        "transaction": {"id": "t-1"},
    }
    screen = finish(made_up, MANIFEST, "staff", "till")
    assert screen["tools"] == {
        "till": {"kind": "view", "input": MANIFEST["views"][0]["input_schema"]}
    }
    assert (screen["accent"], screen["relay"], screen["transaction"]) == (None, None, None)
    # The language is the manifest's, and English when it names none Cliquet knows.
    assert screen["language"] == "en"
    for named, language in [("fr", "fr"), ("de", "en"), (["fr"], "en")]:
        screen = finish(made_up, {**MANIFEST, "language": named}, "staff", "till")
        assert screen["language"] == language


def test_the_vocabulary_comes_from_daisyui_s_stylesheet() -> None:
    names = vocabulary.classes()
    assert {"card", "card-border", "stats", "stat-value", "btn-primary", "list-row"} <= names
    assert {
        "sm:stats-horizontal",
        "md:layout-cols-3",
        "layout-stack",
        "text-base-content/60",
    } <= names
    # Cliquet's prefix is its own: no class of daisyUI's starts with it.
    assert not any(n.rpartition(":")[2].startswith("layout-") for n in vocabulary.daisyui_classes())
    assert not any(name.startswith(("lg:", "xl:", "2xl:")) for name in names)
    assert "modal" not in names and "toast" not in names and "stack" in names
    css = vocabulary.daisyui_css()
    assert "@media (width>=480px)" in css and "(width>=1024px)" not in css


# Screens as MCP Apps


def result_text(result: types.CallToolResult) -> str:
    (content,) = result.content
    assert isinstance(content, types.TextContent)
    return content.text


def test_a_view_is_a_tool_drawn_by_the_renderer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = board(tmp_path)

    async def scenario() -> None:
        server = AppServer(runtime, OWNER).server
        async with Client(server, extensions=[DRAWS], client_info=CLAUDE) as client:
            capabilities = client.server_capabilities
            assert capabilities is not None and capabilities.extensions == {views.EXTENSION: {}}
            tools = {t.name: t for t in (await client.list_tools()).tools}
            overview = tools["overview"]
            assert overview.meta == {"ui": {"resourceUri": views.renderer_uri()}}
            assert (overview.description or "").startswith("The tasks, with a search")
            assert overview.annotations is not None and overview.annotations.read_only_hint
            assert set(overview.input_schema["properties"]) == {"containing", "app_transaction"}
            assert tools["tasks"].meta is None

            (resource,) = (await client.list_resources()).resources
            assert (str(resource.uri), resource.mime_type) == (
                views.renderer_uri(),
                views.MIME_TYPE,
            )
            (page,) = (await client.read_resource(views.renderer_uri())).contents
            assert isinstance(page, types.TextResourceContents)
            assert page.mime_type == views.MIME_TYPE and page.meta is None

            shown = await client.call_tool("overview", {"containing": "tea"})
            assert not shown.is_error
            assert shown.structured_content is not None
            assert "Buy tea" in text(shown.structured_content["body"])
            # The agent is told the person sees it, so it doesn't repeat the screen.
            assert result_text(shown).startswith(
                "The person now sees the screen 'overview', laid out with "
                '{"containing":"tea"}: The tasks'
            )
            assert "Don't repeat what it shows" in result_text(shown)

    with caplog.at_level(logging.INFO, logger="cliquet"):
        anyio.run(scenario)
    assert "board: connected to claude-test 1.0, which draws screens." in caplog.text
    runtime.close()


def test_the_renderer_page_loads_nothing() -> None:
    page = views.renderer_html()
    assert page.startswith("<!doctype html>")
    assert "daisyUI 5.8.1" in page and "window.CLIQUET_VOCABULARY" in page
    assert "<script src" not in page and "<link" not in page and "@import" not in page
    # The vocabulary, Chart.js, the bridge to the host and the renderer's own script.
    assert page.count("</script>") == 4
    assert views.renderer_uri().startswith(
        "ui://cliquet/screen-"
    ) and views.renderer_uri().endswith(".html")


def test_a_trusted_page_inlines_everything_and_closes_no_tag_early() -> None:
    page = views.trusted_page(
        "A <b> & c",
        ["p{color:red}", "/* </style> */"],
        '<main id="m"></main>\n',
        {"SETTING": {"text": "</script><script>alert(1)"}},
        ["let a = 1;", "// </SCRIPT> in a comment"],
    )
    assert "<title>A &lt;b&gt; &amp; c</title>" in page
    assert page.count("</style>") == 1 and "<\\/style> */" in page
    # The settings first, then each script in order, none closed by what it holds.
    assert page.lower().count("</script>") == 3
    assert page.index("window.SETTING = ") < page.index("let a = 1;")
    assert page.index("let a = 1;") < page.index("// <\\/SCRIPT> in a comment")
    # A setting keeps no `<` at all, so it can neither close its tag nor open a comment.
    assert '{"text":"\\u003c/script>\\u003cscript>alert(1)"}' in page
    # Its name follows its content.
    assert views.page_uri("demo", page).startswith("ui://cliquet/demo-")
    assert views.page_uri("demo", page) != views.page_uri("demo", page + " ")


def test_a_client_that_doesn_t_draw_screens_gets_a_line_of_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The view is broken for the reader, but it isn't laid out: there is no screen to send.
    runtime = board(tmp_path, broken("{% if owner %}", "{% if True %}"))

    async def scenario() -> None:
        async with Client(AppServer(runtime, READER).server) as client:
            assert "overview" in {t.name for t in (await client.list_tools()).tools}
            answer = await client.call_tool("overview")
            assert not answer.is_error and answer.structured_content is None
            assert result_text(answer).startswith(
                "The screen 'overview' can't be shown here: this client doesn't draw MCP Apps. "
                "It shows: The tasks, with a search box"
            )
            assert "Answer from the app's queries instead" in result_text(answer)

    with caplog.at_level(logging.INFO, logger="cliquet"):
        anyio.run(scenario)
    assert "which doesn't say it draws screens." in caplog.text
    runtime.close()


def test_views_follow_the_caller_s_role(tmp_path: Path) -> None:
    runtime = board(tmp_path)
    owner = {t.name for t in AppServer(runtime, OWNER).tools}
    reader = {t.name for t in AppServer(runtime, READER).tools}
    assert {"overview", "settings"} <= owner
    assert "overview" in reader and "settings" not in reader
    runtime.close()


def test_a_view_with_problems_tells_the_agent(tmp_path: Path) -> None:
    # The view's problems are an error in the app's code: the owner's to read.
    runtime = board(tmp_path, broken("{% if owner %}", "{% if True %}"), owner=EVERYONE)
    result = AppServer(runtime, READER).call("overview", {}, draws_screens=True)
    assert result.is_error
    shown = result_text(result)
    assert shown.startswith("The screen 'overview' can't be shown. Its view has problems:")
    assert "may open this view but not use add" in shown
    runtime.close()


def test_a_view_s_tool_has_the_title_its_app_gave_it(tmp_path: Path) -> None:
    titled = broken(
        '@board.view(roles=["owner"])', '@board.view(roles=["owner"], title="Réglages")'
    )
    runtime = board(tmp_path, titled)
    tools = {tool.name: tool for tool in AppServer(runtime, OWNER).tools}
    # The name stays the function's: the title is only how a tab or a client words it.
    assert tools["settings"].title == "Réglages"
    assert tools["overview"].title is None
    runtime.close()


def test_an_app_without_views_serves_no_screen(tmp_path: Path) -> None:
    runtime = make_app(tmp_path / "log", LOG_APP, {"log.txt": "start\n"})
    server = AppServer(runtime, OWNER)

    async def scenario() -> None:
        async with Client(server.server) as client:
            capabilities = client.server_capabilities
            assert capabilities is not None
            assert capabilities.extensions is None and capabilities.resources is None

    anyio.run(scenario)
    assert all(tool.meta is None for tool in server.tools)
    runtime.close()


def test_a_screen_in_a_transaction_keeps_its_calls_in_it(tmp_path: Path) -> None:
    runtime = board(tmp_path)
    transaction = runtime.begin(OWNER)
    screen = runtime.view(OWNER, "overview", {}, transaction)
    assert screen["transaction"] == transaction
    assert screen["relay"] is None
    runtime.close()


def test_a_relayed_screen_names_the_tool_that_relays_each_call() -> None:
    screen = {"tools": {"add": {"kind": "action"}, "overview": {"kind": "view"}}, "body": []}
    routes = {"action": ("write_app_data", "tool"), "view": ("show_app_view", "view")}
    relayed = views.relay_calls(screen, routes, {"app": "sam/board", "role": "owner"})
    assert relayed["relay"] == {
        "routes": {
            "action": {"tool": "write_app_data", "key": "tool"},
            "view": {"tool": "show_app_view", "key": "view"},
        },
        "fixed": {"app": "sam/board", "role": "owner"},
    }
    assert relayed["body"] is screen["body"] and "relay" not in screen


# The preview


def test_the_preview_serves_the_view_and_its_tools(tmp_path: Path) -> None:
    runtime = board(tmp_path)
    client = TestClient(preview_app(AppServer(runtime, OWNER)))

    def rpc(method: str, params: dict[str, Any] | None = None) -> Any:
        return client.post("/rpc", json={"method": method, "params": params})

    assert "cliquet.dev preview" in client.get("/").text
    tools = {t["name"]: t for t in rpc("tools/list").json()["tools"]}
    assert tools["overview"]["_meta"] == {"ui": {"resourceUri": views.renderer_uri()}}
    shown = rpc("tools/call", {"name": "overview", "arguments": {"containing": "Sam"}}).json()
    assert shown["structuredContent"]["arguments"] == {"containing": "Sam"}
    (page,) = rpc("resources/read", {"uri": views.renderer_uri()}).json()["contents"]
    assert page["mimeType"] == views.MIME_TYPE

    added = rpc("tools/call", {"name": "add", "arguments": {"task": "Water plants"}}).json()
    assert "isError" not in added or added["isError"] is False
    found = rpc("tools/call", {"name": "tasks", "arguments": {"containing": "Water"}}).json()
    assert found["structuredContent"]["result"] == ["Water plants"]
    refused = rpc("tools/call", {"name": "add", "arguments": {"task": ""}}).json()
    assert refused["isError"] and "A task needs a name" in refused["content"][0]["text"]

    assert rpc("resources/read", {"uri": "ui://other"}).status_code == 500
    assert rpc("prompts/list").status_code == 400
    runtime.close()


def test_the_host_page_calls_the_address_next_to_it() -> None:
    page = host_page()
    assert 'fetch("rpc"' in page
    assert 'fetch("/rpc"' not in page


def test_the_screen_opens_links_through_the_host_page() -> None:
    page = host_page()
    # The frame keeps its sandbox: it may not open a tab, so the page opens its links. It may
    # not send a form either, as in a host that gives no more than the spec asks: the screen's
    # script sends them.
    assert '<iframe id="frame" sandbox="allow-scripts">' in page
    assert "allow-forms" not in page
    assert "allow-popups" not in page
    assert 'method === "ui/open-link"' in page


def test_the_host_page_can_be_embedded() -> None:
    page = host_page()
    assert "body.embed #bar" in page
    assert "tellParent" in page
    assert 'cliquet: "height"' in page


def cliquet(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLIQUET_IN_APP_ENV": "1"}
    command = [sys.executable, "-m", "cliquet.cli", *args]
    return subprocess.run(command, capture_output=True, text=True, env=env)


def test_check_lays_out_the_views(tmp_path: Path) -> None:
    board(tmp_path).close()
    result = cliquet("check", str(tmp_path / "board"))
    assert result.returncode == 0, result.stderr
    assert "the views pass every check" in result.stdout

    source = broken("{% if owner %}", "{% if True %}")
    board(tmp_path / "2", source).close()
    result = cliquet("check", str(tmp_path / "2" / "board"))
    assert result.returncode == 1
    assert "the app's views have problems" in result.stderr
    assert 'view overview, as reader: data-call="add"' in result.stderr


def test_preview_needs_a_view(tmp_path: Path) -> None:
    make_app(tmp_path / "log", LOG_APP, {"log.txt": "start\n"}).close()
    result = cliquet("preview", str(tmp_path / "log"), "--role", "reader")
    assert result.returncode == 1
    assert "log has no view for the role reader" in result.stderr


def test_preview_works_on_a_copy_of_the_data(tmp_path: Path) -> None:
    board(tmp_path).close()
    app = tmp_path / "board"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    command = [sys.executable, "-m", "cliquet.cli", "preview", str(app), "--role", "owner"]
    process = subprocess.Popen(
        [*command, "--port", str(port)],
        env={**os.environ, "CLIQUET_IN_APP_ENV": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)

        def rpc(name: str, arguments: dict[str, Any]) -> Any:
            body = {"method": "tools/call", "params": {"name": name, "arguments": arguments}}
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/rpc",
                data=json.dumps(body).encode(),
                headers={"content-type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)

        assert not rpc("add", {"task": "Only in the preview"}).get("isError")
        assert rpc("tasks", {})["structuredContent"]["result"][-1] == "Only in the preview"
        # The preview's caller is the app's owner: they read an error of the app in full.
        # There was no data in 2020, so the query fails on it.
        asked = {"query": "tasks", "since": "2020-01-01T00:00:00+00:00"}
        failed = rpc("app_changes_since", asked)
        assert failed["isError"]
        assert "It failed with: FileNotFoundError: " in failed["content"][0]["text"]
    finally:
        process.terminate()
        _, stderr = process.communicate(timeout=10)
    assert f"http://127.0.0.1:{port}/?view=overview" in stderr
    # The app's own data and history are untouched.
    assert (app / "data" / "tasks.txt").read_text() == "Buy tea\nCall Sam\n"
    assert git(app, "log", "--format=%s").splitlines() == ["start"]
