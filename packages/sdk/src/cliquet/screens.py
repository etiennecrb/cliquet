"""Screens: what a view returns, how it is laid out, and how it is checked.

A view returns a `Screen`: an HTML template written with Jinja against daisyUI, its values,
and the arguments that draw it again. The worker renders it with Cliquet's own Jinja
environment, where every value is escaped, and parses the HTML into a tree: lists of
`[tag, {attributes}, ...children]` and strings. The browser builds that tree element by
element and never parses HTML. `check` holds the tree to the vocabulary and to the caller's
role: the worker runs it, and the runtime runs it again on what the sandbox answered, which
it doesn't trust. See the builder's views guide and the Screens principles in ARCHITECTURE.md.
"""

import dataclasses
import json
import math
import re
from collections.abc import Iterator, Mapping
from datetime import date, datetime, time
from decimal import Decimal
from functools import lru_cache
from html.parser import HTMLParser
from typing import Any

from jinja2 import (
    Environment,
    StrictUndefined,
    TemplateError,
    TemplateRuntimeError,
    TemplateSyntaxError,
    nodes,
)
from pydantic import BaseModel, ValidationError

from cliquet import icons, tools, vocabulary
from cliquet.errors import AppError, Problem

# The version of a screen's format, which the renderer page draws.
FORMAT = 1
# A screen's size, checked before anything walks it: a sandbox may answer anything.
MAX_DEPTH = 48
MAX_NODES = 20_000
MAX_TEXT = 1_000_000
# The columns a phone shows at once: a wider table scrolls, its rows' names pinned.
PHONE_COLUMNS = 3
_CHART_KINDS = frozenset({"bar", "stacked-bar", "line", "area", "donut", "scatter"})
# A number format, and the digits it may take after a colon, as Angular's digitsInfo writes them:
# {minIntegerDigits}.{minFractionDigits}-{maxFractionDigits}, such as currency:EUR:1.0-0.
_NUMBER_FORMAT = re.compile(r"(number|integer|percent|currency:[A-Z]{3})(?::(.*))?", re.DOTALL)
_DIGITS = re.compile(r"([1-9])\.([0-6])-([0-6])")
# The number formats, as a refusal names them.
_NUMBER_FORMATS = (
    "number, integer, percent or currency: and a currency's code, such as currency:EUR, each "
    "with optional digits, such as currency:EUR:1.0-0"
)
# A label's format: the kind of label a chart's x axis or a grid's column shows, and how much of
# it, short unless it says long. A bare name is short, so month is month:short.
_LABEL_FORMAT = re.compile(r"^(date|month|weekday)(?::(short|long))?$")
# A month or a weekday label: a whole number, which may keep a leading zero, as 03 for March.
_LABEL_NUMBER = re.compile(r"[0-9]{1,2}")
_CHART_KEYS = frozenset({"kind", "labels", "series", "format", "title", "x", "y", "y1", "y2"})
_SERIES_KEYS = frozenset({"name", "values", "tone", "kind", "axis", "format"})
_SCATTER_SERIES_KEYS = frozenset({"name", "points", "tone"})
# What a series of a chart with labels is, as the check says when one isn't.
_SERIES_SHAPE = (
    'each series of a chart is {"name": "Food", "values": [...]}, and a "tone" when its colour '
    'means something. A line over bars also takes "kind": "line", and "axis": "y2" to draw it '
    "against a second axis."
)
# What a series' colour can mean: daisyUI's status colours, and a grey, from cliquet.css.
_CHART_TONES = ("success", "error", "warning", "info", "neutral")
# A grid's columns: a field each, shown in the charts' formats, as text or as a date.
_GRID_KEYS = frozenset({"columns", "rows", "tools", "density", "group_by"})
# The tools a grid can show, and how close its rows sit.
_GRID_TOOLS = ("sort", "search", "filters", "pages")
_GRID_DENSITIES = ("normal", "compact")
_COLUMN_KEYS = frozenset({"field", "header", "format", "total", "tone", "tones"})
# What a column of each kind holds, as a refusal says it.
_HOLDS = {"number": "numbers", "date": "dates", "text": "text"}
# How a number column colours its cells by their sign: both signs, or the negatives only.
_CELL_TONES = ("signed", "negative")
_GRID_COLUMNS = 30
_GRID_ROWS = 5000
# The field of a grouped grid's rows that sits a row in under the one above it, and how far.
_INDENT = "indent"
_INDENTS = (0, 1, 2)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# The totals a column's footer can show, by the kinds of column each fits.
_TOTALS = {
    "sum": ("number",),
    "avg": ("number",),
    "min": ("number", "date"),
    "max": ("number", "date"),
    "count": ("number", "date", "text"),
}
# The fields a person fills, as the check counts them.
_FIELDS = frozenset({"input", "select", "textarea"})
# Where whitespace between elements means nothing, so the tree leaves it out.
_NO_TEXT = frozenset(
    {"table", "thead", "tbody", "tfoot", "tr", "ul", "ol", "dl", "select", "optgroup"}
)
_KEEP_SPACES = frozenset({"pre", "textarea"})
# The whitespace HTML collapses: not a non-breaking space, which Python's `\s` matches.
_WHITESPACE = " \t\n\r\f"
# The filters of Jinja's that would write a value out as HTML: Cliquet's environment has none.
_MARKUP_FILTERS = ("safe", "xmlattr", "urlize")
_ACCENT = re.compile(r"#[0-9a-f]{6}")


class Screen:
    """What a view returns: an HTML template, its values, and the arguments that draw it again.

    `template` is HTML written with Jinja against daisyUI's classes and Cliquet's `layout-`
    classes. Every value is escaped, so data never becomes markup. `args` are the view's
    arguments that draw this screen again exactly as it is now, such as the lines of a receipt
    being built: after a click, the screen is laid out again with them. Leave out an argument
    that asks for something to happen, such as a line to add, or it would happen again.
    """

    __slots__ = ("args", "template", "values")

    def __init__(
        self, template: str, /, *, args: Mapping[str, Any] | None = None, **values: Any
    ) -> None:
        self.template = template
        self.args = dict(args or {})
        self.values = values

    def __repr__(self) -> str:
        return f"Screen({len(self.template)} characters, args={self.args!r})"


def lay_out(screen: object, view: str, arguments: type[BaseModel]) -> dict[str, Any]:
    """Render what a view returned, and parse it into a tree. Raises `AppError`.

    Returns `{"arguments": ..., "body": ...}`: the arguments that draw it again, as JSON, and
    the tree. `arguments` is the model of the view's arguments, which `args` must fit.
    """
    where = f"view {view}"
    if not isinstance(screen, Screen):
        found = type(screen).__name__
        message = f"it returned {found}. Return Screen(TEMPLATE, args={{...}}, **values)."
        raise AppError([Problem(where, message)])
    if not isinstance(screen.template, str):
        raise AppError([Problem(where, "Screen's template must be a string of HTML")])
    problems = list(template_problems(screen.template))
    problems += _markup_problems(screen.values)
    try:
        given = arguments.model_validate(screen.args)
    except ValidationError as error:
        lines = "; ".join(_validation_lines(error))
        problems.append(
            f"Screen(args=...) doesn't fit the view's own arguments, so the screen couldn't be "
            f"laid out again: {lines}"
        )
        given = None
    if problems:
        raise AppError([Problem(where, p) for p in problems])
    assert given is not None
    html = _render(screen, where)
    body, parsed = parse(html)
    if parsed:
        raise AppError([Problem(where, p) for p in parsed])
    dumped = given.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    return {"arguments": dumped, "body": body}


# The environment


def _jsonable(value: Any, depth: int = 0) -> Any:
    """`value` as plain JSON data: models by their aliases, a Decimal as a number, a date as
    ISO text."""
    if depth > 64:
        raise ValueError("the value is nested too deep for tojson")
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="python", by_alias=True), depth + 1)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}
        return _jsonable(fields, depth + 1)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(item, depth + 1) for item in value]
    raise TypeError(f"tojson can't write a {type(value).__name__}")


def _tojson(value: Any) -> str:
    """`value` as JSON text, which the template escapes like any other value, so it is safe
    inside an attribute with either quote. A number that isn't finite, such as NaN, is
    refused: a browser can't read it."""
    try:
        return json.dumps(
            _jsonable(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except ValueError:
        raise TemplateRuntimeError(
            "tojson can't write a number that isn't finite, such as NaN or infinity: a browser "
            "can't read it. Give a number, or None."
        ) from None
    except TypeError as error:
        raise TemplateRuntimeError(str(error)) from None


def _finalize(value: Any) -> Any:
    """What `{{ value }}` shows: nothing for None, rather than the word."""
    return "" if value is None else value


def _environment() -> Environment:
    """Cliquet's Jinja environment: every value escaped, a misspelled name an error, and no way
    to write a value out as HTML."""
    env = Environment(
        autoescape=True,
        undefined=StrictUndefined,
        finalize=_finalize,
        trim_blocks=True,
        lstrip_blocks=True,
        auto_reload=False,
    )
    for name in _MARKUP_FILTERS:
        env.filters.pop(name, None)
    env.filters["tojson"] = _tojson
    return env


_ENV = _environment()


@lru_cache(maxsize=256)
def _compiled(template: str) -> Any:
    return _ENV.from_string(template)


def _render(screen: Screen, where: str) -> str:
    """The HTML a screen's template writes. Raises `AppError` for a mistake in the template;
    anything else the app's code raised goes on, as the app's failure."""
    try:
        return _compiled(screen.template).render(**screen.values)
    except TemplateError as error:
        raise AppError([Problem(where, _template_error(error, screen))]) from None


def _template_error(error: TemplateError, screen: Screen) -> str:
    """A template's mistake, in words that say what to change."""
    line = getattr(error, "lineno", None)
    at = f"line {line} of the template: " if line else "the template: "
    text = str(error.message or error)
    missing = re.fullmatch(r"'([^']+)' is undefined", text)
    if missing:
        given = ", ".join(sorted(screen.values)) or "nothing"
        return (
            f"{at}it uses {missing.group(1)}, which the view doesn't give. Pass it to Screen(...), "
            f"which gives {given}."
        )
    if "no filter named" in text.lower():
        name = re.search(r"'([^']+)'", text)
        if name and name.group(1) in _MARKUP_FILTERS:
            return (
                f"{at}the filter {name.group(1)} isn't available: a screen writes every value as "
                "text, so data never becomes markup. Write the HTML in the template itself."
            )
    return f"{at}{text}"


# The template's own text


@lru_cache(maxsize=256)
def template_problems(template: str) -> tuple[str, ...]:
    """What in a template could let a value become markup, or reach outside the screen."""
    try:
        tree = _ENV.parse(template)
    except TemplateSyntaxError as error:
        return (f"line {error.lineno} of the template: {error.message}",)
    problems: list[str] = []
    if any(tree.find_all((nodes.EvalContextModifier, nodes.ScopedEvalContextModifier))):
        problems.append(
            "{% autoescape %} isn't allowed: a screen escapes every value, so data never "
            "becomes markup."
        )
    if any(tree.find_all((nodes.Include, nodes.Import, nodes.FromImport, nodes.Extends))):
        problems.append(
            "a template can't include, import or extend another: write it as one string. For a "
            "part used by several views, add a Python constant to each template, such as "
            "PARTS + TILL, and define a {% macro %} in it."
        )
    return tuple(problems + _tag_problems(template))


_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.DOTALL)
_ATTRIBUTE = re.compile(r"[A-Za-z][A-Za-z0-9_:.-]*")
# A `{% raw %}` block: what it holds is written out as it is, `{{ }}` included.
_RAW = re.compile(r"\{%-?\s*raw\s*-?%\}")
_END_RAW = re.compile(r"\{%-?\s*endraw\s*-?%\}")
_LITERAL_ONLY = frozenset({"data-call", "data-view"})


def _tag_problems(template: str) -> list[str]:
    """`{{ }}` where a value would be read as markup: in a tag, outside a quoted attribute
    value, or as a tag's name; and a tool named by a value."""
    problems: list[str] = []
    state = "text"  # text, tag, quoted, comment
    quote = ""
    attribute = ""
    raw = False
    index = 0

    def at() -> str:
        """Where the scan is, as the person reading the problem finds it."""
        return f"line {template.count(chr(10), 0, index) + 1} of the template"

    while index < len(template):
        if raw:
            # Literal text: the HTML is scanned, but nothing in it is a value.
            end = _END_RAW.match(template, index)
            if end is not None:
                raw, index = False, end.end()
                continue
            found = None
        else:
            found = _JINJA.match(template, index)
        if found is not None and _RAW.fullmatch(found.group(0)):
            raw, index = True, found.end()
            continue
        if found is not None:
            marker = found.group(0)
            if marker.startswith("{{") and state == "tag":
                problems.append(
                    f"{at()}: {marker} sits inside a tag, outside quotes, "
                    "where a value could add attributes. Put it inside a quoted attribute "
                    'value, such as class="{{ ... }}".'
                )
            if not marker.startswith("{#") and state == "quoted" and attribute in _LITERAL_ONLY:
                problems.append(
                    f"{at()}: {attribute} must name the tool itself, such "
                    f'as {attribute}="record_sale", not with {marker}. Lay out one form for '
                    "each tool, with {% if %} around those a role may not use."
                )
            index = found.end()
            continue
        char = template[index]
        if state == "text":
            if template.startswith("<!--", index):
                state, index = "comment", index + 4
                continue
            if char == "<" and index + 1 < len(template):
                after = template[index + 1]
                if after == "{":
                    problems.append(
                        f"{at()}: a tag's name must be written out, not "
                        "made by Jinja: write <div>, <span> and so on."
                    )
                elif after.isalpha() or after == "/":
                    state, attribute = "tag", ""
        elif state == "comment":
            if template.startswith("-->", index):
                state, index = "text", index + 3
                continue
        elif state == "tag":
            if char == ">":
                state = "text"
            elif char in "\"'":
                state, quote = "quoted", char
            elif char.isalpha() or char == "-":
                name = _ATTRIBUTE.match(template, index)
                if name is not None:
                    attribute = name.group(0).lower()
                    index = name.end()
                    continue
        elif state == "quoted" and char == quote:
            state = "tag"
        index += 1
    return list(dict.fromkeys(problems))


def _markup_problems(values: Mapping[str, Any]) -> list[str]:
    """A value that would be written out as HTML, such as `Markup(...)`: refused, so data never
    becomes markup."""
    seen = 0
    stack: list[tuple[str, Any]] = list(values.items())
    while stack and seen < 50_000:
        name, value = stack.pop()
        seen += 1
        if hasattr(value, "__html__"):
            return [
                f"the value {name} is markup ({type(value).__name__}). Pass plain text: a screen "
                "writes every value as text, and the template holds the HTML."
            ]
        if isinstance(value, str | bytes | int | float | Decimal | date | time):
            continue
        if isinstance(value, Mapping):
            stack += [(name, item) for item in value.values()]
        elif isinstance(value, list | tuple | set | frozenset):
            stack += [(name, item) for item in value]
        elif isinstance(value, BaseModel):
            stack += [(name, item) for item in value.__dict__.values()]
        elif dataclasses.is_dataclass(value) and not isinstance(value, type):
            stack += [(name, getattr(value, f.name)) for f in dataclasses.fields(value)]
    return []


def _validation_lines(error: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(part) for part in one['loc']) or 'args'}: {one['msg']}"
        for one in error.errors()
    ]


# The HTML, parsed


# What runs of whitespace in an element's text become one space: all of them, or all but
# line breaks in an element that shows them, as a note a person typed. None keeps them all.
# Either way, the whitespace at either end of a text, the template's own, becomes one space.
_SPACES = re.compile(f"[{_WHITESPACE}]+")
_SPACES_BUT_LINES = re.compile(f"[{_WHITESPACE.replace(chr(10), '')}]+")


def _spaces(
    tag: str, attributes: dict[str, str], parent: re.Pattern[str] | None
) -> re.Pattern[str] | None:
    """The whitespace an element's text loses, from its own tag and class, else its parent's."""
    if parent is None or tag in _KEEP_SPACES:
        return None
    if any(name.endswith("layout-lines") for name in attributes.get("class", "").split()):
        return _SPACES_BUT_LINES
    return parent


class _Parser(HTMLParser):
    """HTML to the tree, strictly: every element closed, in order, so that what the check
    reads and what the browser builds are the same."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: list[Any] = []
        # Each open element: its tag, its node, its line, and the whitespace its text loses.
        self.open: list[tuple[str, list[Any], int, re.Pattern[str] | None]] = [
            ("", self.root, 0, _SPACES)
        ]
        self.problems: list[str] = []

    def _add(self, node: Any) -> None:
        self.open[-1][1].append(node)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = [name for name, _ in attrs]
        twice = sorted({name for name in names if names.count(name) > 1})
        if twice:
            self.problems.append(f"<{tag}> has {', '.join(twice)} twice: give each attribute once.")
        node: list[Any] = [tag, {name: value or "" for name, value in attrs}]
        self._add(node)
        if tag not in vocabulary.VOID:
            spaces = _spaces(tag, node[1], self.open[-1][3])
            self.open.append((tag, node, self.getpos()[0], spaces))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in vocabulary.VOID:
            self.problems.append(
                f"write <{tag}></{tag}>, not <{tag}/>: only void elements close themselves."
            )
        self.handle_starttag(tag, attrs)
        if tag not in vocabulary.VOID:
            self.open.pop()

    def handle_endtag(self, tag: str) -> None:
        if tag in vocabulary.VOID:
            self.problems.append(f"</{tag}> closes nothing: <{tag}> has no end tag.")
            return
        top, _, line, _ = self.open[-1]
        if top == tag:
            self.open.pop()
            return
        if any(name == tag for name, _, _, _ in self.open[1:]):
            self.problems.append(
                f"</{tag}> closes <{tag}> while <{top}>, opened on line {line} of the screen's "
                f"HTML, is still open. Close <{top}> first."
            )
            while self.open[-1][0] != tag:
                self.open.pop()
            self.open.pop()
        else:
            self.problems.append(f"</{tag}> closes nothing: no <{tag}> is open.")

    def handle_data(self, data: str) -> None:
        spaces = self.open[-1][3]
        if spaces is not None:
            middle = data.strip(_WHITESPACE)
            if not middle:
                data = " " if data else ""
            else:
                start = " " * (data[0] in _WHITESPACE)
                end = " " * (data[-1] in _WHITESPACE)
                data = start + spaces.sub(" ", middle) + end
        siblings = self.open[-1][1]
        if siblings and isinstance(siblings[-1], str):
            siblings[-1] += data
        else:
            siblings.append(data)

    def handle_decl(self, decl: str) -> None:
        self.problems.append("leave out <!doctype>: write only what goes inside the screen.")

    def handle_pi(self, data: str) -> None:
        self.problems.append(f"<?{data[:20]}> isn't HTML a screen may use.")

    def unknown_decl(self, data: str) -> None:
        self.problems.append(f"<![{data[:20]}> isn't HTML a screen may use.")


def parse(html: str) -> tuple[list[Any], list[str]]:
    """The tree of a screen's HTML, and what is wrong with how it is written."""
    parser = _Parser()
    parser.feed(html)
    parser.close()
    for tag, _, line, _ in parser.open[1:]:
        parser.problems.append(
            f"<{tag}>, opened on line {line} of the screen's HTML, is never closed."
        )
    return _tidy(parser.root, ""), list(dict.fromkeys(parser.problems))


def _tidy(children: list[Any], parent: str) -> list[Any]:
    """Children without the whitespace that means nothing: at either end, and between the rows
    of a table or the items of a list."""
    kept: list[Any] = []
    for child in children:
        if isinstance(child, list):
            child[2:] = _tidy(child[2:], child[0])
            kept.append(child)
        elif (
            parent in _KEEP_SPACES
            or child.strip(_WHITESPACE)
            or (parent and parent not in _NO_TEXT)
        ):
            kept.append(child)
    if parent not in _KEEP_SPACES:
        if kept and isinstance(kept[0], str):
            kept[0] = kept[0].lstrip(_WHITESPACE)
        if kept and isinstance(kept[-1], str):
            kept[-1] = kept[-1].rstrip(_WHITESPACE)
        kept = [child for child in kept if child != ""]
    return kept


# The check


def text(body: list[Any]) -> str:
    """The text a screen shows, its words separated by spaces: for tests and for logs."""
    words: list[str] = []
    stack: list[Any] = list(reversed(body))
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            words.append(node)
        elif isinstance(node, list):
            stack += reversed(node[2:])
    return re.sub(r"\s+", " ", " ".join(words)).strip()


def _shape_problem(body: object) -> str | None:
    """What makes a tree something other than a screen, or too big to walk: checked first,
    without recursion, on whatever a sandbox answered."""
    if not isinstance(body, list):
        return f"the screen's body is {type(body).__name__}, not a list"
    stack: list[tuple[Any, int]] = [(node, 1) for node in body]
    count = 0
    size = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_NODES:
            return f"the screen has more than {MAX_NODES:,} parts: show less, or page through it"
        if depth > MAX_DEPTH:
            return f"the screen nests more than {MAX_DEPTH} levels deep"
        if isinstance(node, str):
            size += len(node)
        elif (
            isinstance(node, list)
            and len(node) >= 2
            and isinstance(node[0], str)
            and isinstance(node[1], dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in node[1].items())
        ):
            size += sum(len(k) + len(v) for k, v in node[1].items())
            stack += [(child, depth + 1) for child in node[2:]]
        else:
            return "a part of the screen isn't [tag, {attributes}, ...children] or text"
        if size > MAX_TEXT:
            return f"the screen holds more than {MAX_TEXT:,} characters: show less"
    return None


def _elements(body: list[Any]) -> Iterator[tuple[list[Any], list[list[Any]]]]:
    """Each element of a tree, in document order, with the elements around it, outermost first."""
    stack: list[tuple[Any, list[list[Any]]]] = [(node, []) for node in reversed(body)]
    while stack:
        node, around = stack.pop()
        if isinstance(node, list):
            yield node, around
            inside = [*around, node]
            stack += [(child, inside) for child in reversed(node[2:])]


def _describe(node: list[Any]) -> str:
    """An element as the person reading a problem finds it in their template."""
    attributes = node[1]
    shown = [
        f'{key}="{attributes[key]}"'
        for key in ("data-call", "data-view", "name", "id", "type", "class", "href")
        if key in attributes
    ][:2]
    return "<" + " ".join([node[0], *shown]) + ">"


def _not_json(constant: str) -> Any:
    """Refuse NaN and infinity, which Python reads as JSON but a browser doesn't."""
    raise ValueError(f"{constant} isn't JSON")


def _json_object(value: str, what: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        found = json.loads(value, parse_constant=_not_json)
    except ValueError:
        return None, (
            f"{what} isn't JSON. Write it with tojson, such as "
            f"{what}='{{{{ {{\"lines\": lines}} | tojson }}}}'."
        )
    if not isinstance(found, dict):
        return None, f'{what} must be a JSON object, such as {{"lines": []}}.'
    return found, None


def chart_problem(value: str) -> str | None:
    """What is wrong with a chart's `data-chart`, by Cliquet's chart format, or None."""
    chart, wrong = _json_object(value, "data-chart")
    if chart is None:
        return wrong
    unknown = sorted(set(chart) - _CHART_KEYS)
    if unknown:
        return (
            "a chart takes kind, labels, series, format, title, x, y (or y1) and y2, not "
            f"{', '.join(unknown)}."
        )
    kind = chart.get("kind")
    if kind not in _CHART_KINDS:
        return f"a chart's kind is one of {', '.join(sorted(_CHART_KINDS))}, not {kind!r}."
    if "format" in chart and (wrong := _top_format_problem(chart["format"], kind)):
        return wrong
    if chart.get("y") is not None and chart.get("y1") is not None:
        return "the chart has both y and y1, which are the same axis: give one."
    for key in ("x", "y", "y1", "y2"):
        if wrong := _axis_problem(chart.get(key), key, kind):
            return wrong
    wrong = _scatter_problem(chart) if kind == "scatter" else _series_problem(chart, kind)
    if wrong:
        return wrong
    title = chart.get("title")
    if title is not None and not isinstance(title, str):
        return "a chart's title is text."
    return None


def _top_format_problem(form: object, kind: str) -> str | None:
    """What is wrong with a format at the top of a chart, which only a donut takes, or None."""
    if kind == "donut":
        return _format_problem(form, "the donut")
    shown = json.dumps(form)
    if _label_kind(form):
        # A label's format belongs to a chart's x axis, which holds its labels, and a scatter
        # has none.
        if kind == "scatter":
            return (
                f"the chart has the format {shown} at the top, which a scatter doesn't take: "
                "a scatter's points are numbers, so give its axes a number format."
            )
        axes = f'"x": {{"format": {shown}}}'
    else:
        axes = f'"y": {{"format": {shown}}}'
        if kind == "scatter":
            axes += f' or "x": {{"format": {shown}}}'
    return (
        "the chart has a format at the top, which only a donut takes: give it to the axis it "
        f"describes, as {axes}."
    )


def _series_problem(chart: dict[str, Any], kind: str) -> str | None:
    """What is wrong with the labels and series of a chart other than a scatter, or None."""
    labels = chart.get("labels")
    if not isinstance(labels, list) or not all(isinstance(one, str) for one in labels):
        return 'a chart\'s labels are a list of text, such as ["Jun", "Jul"].'
    if not labels or len(labels) > 400:
        return "a chart has from 1 to 400 labels."
    if wrong := _chart_labels_problem(labels, chart.get("x")):
        return wrong
    series, wrong = _series_of(chart, "chart", '{"name": ..., "values": [...]}')
    if series is None:
        return wrong
    names: set[str] = set()
    tones: set[str] = set()
    for one in series:
        if not isinstance(one, dict):
            return _SERIES_SHAPE
        if wrong := _one_series_problem(one, kind, names, tones):
            return wrong
        if not {"name", "values"} <= set(one) <= _SERIES_KEYS:
            return _SERIES_SHAPE
        name, values = one["name"], one["values"]
        if not isinstance(values, list) or len(values) != len(labels):
            return (
                f"the series {name!r} has {len(values) if isinstance(values, list) else 'no'} "
                f"values for {len(labels)} labels: give one value per label, 0 where there is "
                "none."
            )
        if not all(v is None or _is_number(v) for v in values):
            return f"the series {name!r} has a value that isn't a finite number."
        line = one.get("kind")
        if line is not None:
            if line != "line":
                return f"the series {name!r} has the kind {line!r}: a series' kind is line."
            if kind == "line":
                return (
                    f"the series {name!r} is a line already, in a line chart: leave its kind out."
                )
            if kind not in ("bar", "stacked-bar"):
                return (
                    f"the series {name!r} has a kind, which only a bar chart's series take: to "
                    'draw bars with a line, make the chart a bar chart and give the line "kind": '
                    '"line".'
                )
        # An axis of None is no axis, as a tone of None is no tone.
        axis = "y" if one.get("axis") is None else one["axis"]
        if axis not in ("y", "y1", "y2"):
            return (
                f'the series {name!r} has the axis {axis!r}: give "y2" to draw it against the '
                "second axis, or leave its axis out."
            )
        if axis == "y2" and kind != "line" and line != "line":
            fix = (
                'give it "kind": "line", or leave its axis out'
                if kind in ("bar", "stacked-bar")
                else "leave its axis out"
            )
            return f"the series {name!r} is on y2, a second axis that only a line takes: {fix}."
    if kind == "donut" and len(series) != 1:
        return "a donut has one series: its labels are the slices."
    on_y2 = sum(one.get("axis") == "y2" for one in series)
    if on_y2 == len(series):
        return "every series of the chart is on y2: leave their axis out to draw them on y."
    if chart.get("y2") is not None and not on_y2:
        return (
            'the chart has a y2 axis and no series on it: give a line "axis": "y2", or leave '
            "y2 out."
        )
    return None


def grid_problem(value: str) -> str | None:
    """What is wrong with a grid's `data-grid`, by Cliquet's grid format, or None."""
    grid, wrong = _json_object(value, "data-grid")
    if grid is None:
        return wrong
    unknown = sorted(set(grid) - _GRID_KEYS)
    if unknown:
        return (
            f"a grid takes columns and rows, and optional tools, density and group_by, not "
            f"{', '.join(unknown)}."
        )
    if wrong := _tools_problem(grid):
        return wrong
    columns = grid.get("columns")
    if not isinstance(columns, list) or not 1 <= len(columns) <= _GRID_COLUMNS:
        return (
            f"a grid has from 1 to {_GRID_COLUMNS} columns, each "
            '{"field": "day", "header": "Day", "format": "date"}.'
        )
    kinds: dict[str, str] = {}
    for column in columns:
        if wrong := _column_problem(column, kinds):
            return wrong
    group = grid.get("group_by")
    if wrong := _group_problem(group, kinds):
        return wrong
    rows = grid.get("rows")
    if not isinstance(rows, list):
        return (
            'a grid\'s rows are a list of objects, one per record, such as {"day": "2026-11-09"}.'
        )
    if len(rows) > _GRID_ROWS:
        return (
            f"a grid has {len(rows):,} rows, and shows {_GRID_ROWS:,} at most: narrow them in the "
            "view, such as to a month, with an argument to choose another."
        )
    if (
        group is not None
        and rows
        and not any(isinstance(row, dict) and group in row for row in rows)
    ):
        return (
            f"the grid groups its rows by {group!r}, which no row has: give each row the title "
            f'of its group, such as {{"{group}": "Income"}}, or group by a field the rows have.'
        )
    for number, row in enumerate(rows, 1):
        if wrong := _row_problem(row, number, kinds, group):
            return wrong
    return None


def _group_problem(group: object, kinds: dict[str, str]) -> str | None:
    """What is wrong with the field a grid groups its rows by, or None."""
    if group is None:
        return None
    if not isinstance(group, str) or not _IDENTIFIER.fullmatch(group):
        return (
            f"a grid's group_by is the field of its rows that names their group, such as "
            f"section, not {group!r}."
        )
    if _INDENT in (group, *kinds):
        return (
            'a grouped grid keeps "indent" for how far a row sits in: give its group or its '
            "column another field."
        )
    if group in kinds:
        return (
            f"the grid groups its rows by {group!r} and shows it as a column too: drop that "
            "column, as each group's title shows its value."
        )
    return None


def _tools_problem(grid: dict[str, object]) -> str | None:
    """What is wrong with a grid's tools or its density, or None."""
    tools = grid.get("tools", [])
    if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
        return (
            'a grid\'s tools are a list of names, such as ["sort"], or [] for none: any of '
            "sort, search, filters and pages."
        )
    unknown = [tool for tool in tools if tool not in _GRID_TOOLS]
    if unknown:
        return (
            f"a grid has no tool {unknown[0]!r}: give any of sort, search, filters and pages, "
            "or [] for none."
        )
    density = grid.get("density", "normal")
    if density not in _GRID_DENSITIES:
        return f"a grid's density is normal or compact, not {density!r}."
    return None


def _column_problem(column: object, kinds: dict[str, str]) -> str | None:
    """What is wrong with one column of a grid, or None. Adds its field and its kind to
    `kinds`: number, date or text."""
    if not isinstance(column, dict) or "field" not in column or set(column) - _COLUMN_KEYS:
        return (
            'each column of a grid is {"field": "amount"}, with an optional "header", '
            '"format", "total", and "tone" or "tones", and nothing else.'
        )
    field = column["field"]
    if not isinstance(field, str) or not _IDENTIFIER.fullmatch(field):
        return f"a grid's field is a name of letters, digits and _, such as paid_to, not {field!r}."
    if field in kinds:
        return f"two columns of a grid show the field {field!r}: give each field one column."
    header = column.get("header", field)
    if not isinstance(header, str) or not header.strip():
        return f"the column {field!r} has a header that isn't text: give it the words to show."
    form = column.get("format", "text")
    if form == "text":
        kind = "text"
    elif _label_kind(form):
        # A date column, however much of the date it shows: a month column reads its cells as
        # the dates they are, so it sorts and filters by them.
        kind = "date"
    elif found := _number_format(form):
        if wrong := _digits_problem(found, f"the column {field!r}"):
            return wrong
        kind = "number"
    else:
        return (
            f"the column {field!r} has the format {form!r}: give text, {_NUMBER_FORMATS}, or a "
            "label's format, such as date, month:short or weekday:long."
        )
    total = column.get("total")
    if total is not None:
        if total not in _TOTALS:
            return (
                f"the column {field!r} has the total {total!r}: give sum, avg, min, max or "
                "count, or no total."
            )
        if kind not in _TOTALS[total]:
            fits = "a number column" if total in ("sum", "avg") else "a number or date column"
            return (
                f"the column {field!r} holds {_HOLDS[kind]}, and only {fits} takes the total "
                f"{total}."
            )
    if wrong := _tone_problem(column, field, kind):
        return wrong
    kinds[field] = kind
    return None


def _tone_problem(column: dict[str, object], field: str, kind: str) -> str | None:
    """What is wrong with how a column of a grid colours its cells, or None: a number column's
    tone, by their sign, or a text column's tones, by their value."""
    holds = _HOLDS[kind]
    if "tone" in column and "tones" in column:
        return (
            f"the column {field!r} has a tone and tones: give an amount a tone, such as "
            '"signed", or a state its tones, such as {"Paid": "success"}, not both.'
        )
    if "tone" in column:
        if kind != "number":
            return (
                f"the column {field!r} holds {holds}, and only a number column takes a tone: "
                'colour a state with tones, such as {"Paid": "success", "Late": "error"}.'
            )
        if column["tone"] not in _CELL_TONES:
            return (
                f"the column {field!r} has the tone {column['tone']!r}: give signed, for positives "
                "in success and negatives in error, or negative, for the negatives only."
            )
    if "tones" in column:
        if kind != "text":
            # A number is coloured by its sign, and a date by neither.
            instead = (
                'colour an amount by its sign with "tone": "signed" or "negative"'
                if kind == "number"
                else "give them to a text column of states"
            )
            return (
                f"the column {field!r} holds {holds}, and only a text column takes tones: "
                f"{instead}."
            )
        tones = column["tones"]
        if not isinstance(tones, dict):
            return (
                f"the column {field!r} has tones that aren't an object: map each value to its "
                'tone, such as {"Paid": "success", "Late": "error"}.'
            )
        for value, tone in tones.items():
            if tone not in _CHART_TONES:
                return (
                    f"the column {field!r} gives {value!r} the tone {tone!r}: give one of "
                    f"{', '.join(_CHART_TONES)}."
                )
    return None


def _row_problem(row: object, number: int, kinds: dict[str, str], group: str | None) -> str | None:
    """What is wrong with the row `number` of a grid, counted from 1, or None. `group` is the
    field the grid groups its rows by, if any."""
    if not isinstance(row, dict):
        return (
            f"the row {number} of a grid isn't an object: give each row its cells by field, "
            'such as {"day": "2026-11-09", "amount": 12.5}.'
        )
    unknown = set(row) - set(kinds)
    if group is not None:
        if wrong := _grouped_problem(row, number, group):
            return wrong
        unknown -= {group, _INDENT}
    elif _INDENT in unknown:
        return (
            f"the row {number} of a grid has an indent, which only a grid with group_by takes: "
            "group the rows, or leave indent out."
        )
    if unknown:
        return (
            f"the row {number} of a grid has the field {min(unknown)!r}, which no column shows: "
            "add a column for it, or leave it out of the rows."
        )
    for field, cell in row.items():
        if cell is None or field not in kinds:
            continue
        kind = kinds[field]
        numeric = isinstance(cell, int | float) and not isinstance(cell, bool)
        if numeric and not math.isfinite(cell):
            return f"the row {number} has {field} {cell!r}, which isn't a finite number."
        if kind == "number" and not numeric:
            return (
                f"the column {field!r} holds numbers, and the row {number} has {cell!r}: give a "
                "number, or null where there is none."
            )
        if kind == "date" and not (isinstance(cell, str) and _iso_date(cell)):
            return (
                f"the column {field!r} holds dates, and the row {number} has {cell!r}: give it as "
                "YYYY-MM-DD, as tojson writes a date, or null where there is none."
            )
        if not (numeric or isinstance(cell, str | bool)):
            return (
                f"the row {number} has a {type(cell).__name__} in {field}: a cell is text, a "
                "number, true, false or null."
            )
    return None


def _grouped_problem(row: dict[str, object], number: int, group: str) -> str | None:
    """What is wrong with the group or the indent of the row `number` of a grouped grid, or
    None."""
    title = row.get(group)
    if title is not None and not isinstance(title, str):
        return (
            f"the row {number} has {title!r} in {group}, the field the grid groups by: give the "
            "title of its group as text, or null for the rows of no group."
        )
    indent = row.get(_INDENT)
    if indent is not None and (type(indent) is not int or indent not in _INDENTS):
        return (
            f"the row {number} has the indent {indent!r}: give 1 or 2 to sit it in under the row "
            "above it, or 0 for none."
        )
    return None


def _iso_date(text: str) -> bool:
    """Whether a text is a date written as YYYY-MM-DD."""
    if not _ISO_DATE.fullmatch(text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _chart_labels_problem(labels: list[str], axis: object) -> str | None:
    """What is wrong with a chart's labels in the format its x axis gives them, or None."""
    form = axis.get("format") if isinstance(axis, dict) else None
    kind = _label_kind(form)
    # No format is no rule, and another format is the axis's own problem.
    if kind is None or not isinstance(form, str):
        return None
    for label in labels:
        if not _label_fits(label, kind):
            return _label_refusal(label, form, kind)
    return None


def _label_kind(form: object) -> str | None:
    """The kind of label a format names, such as date or month, or None for another format."""
    found = _LABEL_FORMAT.fullmatch(form) if isinstance(form, str) else None
    return found.group(1) if found else None


# What a label of each kind is, as the check says when one isn't: what it names, the numbers it
# runs from and to (None when a label isn't one), and what the x axis's format reads.
_LABEL_RULES = {
    "date": ("a date", None, "a date as YYYY-MM-DD, such as 2026-11-03"),
    "month": ("a month", (1, 12), "a whole number from 1 to 12, such as 11 for November"),
    "weekday": ("a weekday", (1, 7), "a whole number from 1 to 7, where 1 is Monday"),
}


def _label_fits(label: str, kind: str) -> bool:
    """Whether a label is what its kind reads: a date, a month or a weekday."""
    _, bounds, _ = _LABEL_RULES[kind]
    if bounds is None:
        return _iso_date(label)
    low, high = bounds
    return bool(_LABEL_NUMBER.fullmatch(label)) and low <= int(label) <= high


def _label_refusal(label: str, form: str, kind: str) -> str:
    """What to say of a label the x axis's format doesn't read."""
    named, _, reads = _LABEL_RULES[kind]
    return (
        f"the label {label!r} isn't {named}: the x axis's format is {form!r}, which takes {reads}."
    )


def _scatter_problem(chart: dict[str, Any]) -> str | None:
    """What is wrong with the series of a scatter, or None."""
    if "labels" in chart:
        return "a scatter has no labels: each point has its own x and y. Leave labels out."
    example = '{"name": ..., "points": [{"x": 140, "y": 0.42}]}'
    series, wrong = _series_of(chart, "scatter", example)
    if series is None:
        return wrong
    names: set[str] = set()
    tones: set[str] = set()
    count = 0
    for one in series:
        if not isinstance(one, dict):
            return 'each series of a scatter is {"name": "Green tea", "points": [...]}.'
        if wrong := _one_series_problem(one, "scatter", names, tones):
            return wrong
        name = one["name"]
        if "values" in one:
            return (
                f"the series {name!r} has values, and a scatter's series have points instead, "
                'such as "points": [{"x": 140, "y": 0.42}].'
            )
        if "kind" in one:
            return f"the series {name!r} has a kind: every series of a scatter is drawn as points."
        if not set(one) <= _SCATTER_SERIES_KEYS:
            unknown = ", ".join(sorted(set(one) - _SCATTER_SERIES_KEYS))
            return f"the series {name!r} takes name, points and tone, not {unknown}."
        points = one.get("points")
        if not isinstance(points, list):
            return f'the series {name!r} has no points: give "points": [{{"x": 140, "y": 0.42}}].'
        count += len(points)
        if count > 1000:
            total = sum(
                len(other["points"])
                for other in series
                if isinstance(other, dict) and isinstance(other.get("points"), list)
            )
            return (
                f"a scatter has at most 1,000 points in all, and this one has {total:,}: show "
                "fewer, such as the largest or the latest."
            )
        for point in points:
            if (
                not isinstance(point, dict)
                or not {"x", "y"} <= set(point) <= {"x", "y", "label"}
                or not (_is_number(point["x"]) and _is_number(point["y"]))
                or not isinstance(point.get("label", ""), str)
            ):
                return (
                    f'a point of the series {name!r} isn\'t {{"x": 140, "y": 0.42}}: its x and '
                    'y are finite numbers, and it may have a "label" of text.'
                )
    return None


def _series_of(
    chart: dict[str, Any], what: str, example: str
) -> tuple[list[Any], None] | tuple[None, str]:
    """A chart's list of 1 to 12 series, or what is wrong with it."""
    series = chart.get("series")
    if isinstance(series, list) and 1 <= len(series) <= 12:
        return series, None
    return None, f"a {what} has from 1 to 12 series, each {example}."


def _one_series_problem(
    series: dict[str, Any], kind: str, names: set[str], tones: set[str]
) -> str | None:
    """What is wrong with a series' name or tone, given the series before it, or None."""
    name = series.get("name")
    if not isinstance(name, str) or not name:
        return "each series of a chart has a name, which keeps its colour across the app."
    if name in names:
        return "two series of a chart have the same name."
    names.add(name)
    if series.get("format") is not None:
        return _series_format_problem(name, kind, series.get("kind") == "line")
    tone = series.get("tone")
    if tone is None:
        return None
    if kind == "donut":
        return "a donut's slices are coloured by their labels: give its series no tone."
    if tone not in _CHART_TONES:
        return (
            f"the series {name!r} has the tone {tone!r}: give one of "
            f"{', '.join(_CHART_TONES)}, or no tone to colour it by its name."
        )
    if tone in tones:
        return (
            f"two series of a chart have the tone {tone!r}, and the same colour: "
            "give one of them another tone, or none."
        )
    tones.add(tone)
    return None


def _series_format_problem(name: str, kind: str, line: bool) -> str:
    """Where a series' format goes instead, by the kind of chart and of series it is."""
    wrong = f"the series {name!r} has a format: "
    if kind == "scatter":
        return wrong + (
            'a scatter\'s points are in the formats of its axes, such as "y": {"format": '
            '"percent"}.'
        )
    if kind == "donut":
        return wrong + (
            'a donut takes its format at the top of the chart, such as "format": "currency:EUR".'
        )
    if kind == "line" or line:
        return wrong + (
            "a series is in the format of its axis. To give a line a format of its own, put it "
            'on a second axis with "axis": "y2", and give the chart "y2": {"format": ...}.'
        )
    return wrong + ('a series is in the format of its axis: give the chart "y": {"format": ...}.')


def _axis_problem(axis: object, key: str, kind: str) -> str | None:
    """What is wrong with a chart's `x`, `y` (or `y1`) or `y2` axis, or None."""
    if axis is None:
        return None
    if kind == "donut":
        return f"a donut has no axes: leave its {key} out."
    if kind == "scatter" and key == "y2":
        return "a scatter has one y axis: leave its y2 out."
    if not isinstance(axis, dict):
        return f'a chart\'s {key} is its axis, such as {{"title": "Margin", "min": 0}}.'
    # The x axis of a chart with labels holds them; every other axis is a span of numbers.
    labels = key == "x" and kind != "scatter"
    if labels:
        if unknown := sorted(set(axis) - {"title", "format"}):
            return (
                f"the x axis of a {kind} chart holds its labels, so it takes a title and a "
                f"label's format: leave out its {', '.join(unknown)}."
            )
    elif unknown := sorted(set(axis) - {"title", "min", "max", "format"}):
        return f"a chart's {key} takes title, min, max and format, not {', '.join(unknown)}."
    title = axis.get("title")
    if title is not None and (not isinstance(title, str) or not title):
        return f'the {key} axis\'s title is text, such as "Margin".'
    low, high = axis.get("min"), axis.get("max")
    if not all(bound is None or _is_number(bound) for bound in (low, high)):
        return f"the {key} axis's min and max are finite numbers."
    if low is not None and high is not None and low >= high:
        return f"the {key} axis has a min of {low} and a max of {high}: give a min below the max."
    first = key in ("y", "y1")
    if first and low is not None and low > 0 and kind in ("bar", "stacked-bar", "area"):
        return (
            f"a {kind} chart's {key} axis starts at zero, since a bar or an area cut short "
            "misrepresents its value: leave its min out, or draw a line chart."
        )
    if "format" in axis:
        if labels:
            return _label_problem(axis["format"])
        return _format_problem(axis["format"], f"the {key} axis")
    return None


def _label_problem(form: object) -> str | None:
    """What is wrong with the format of a chart's labels, which its x axis gives them, or None."""
    if _label_kind(form):
        return None
    return (
        f"the x axis has the format {form!r}: give the format of its labels, date, month or "
        "weekday, with :short or :long, such as month:short."
    )


def _is_number(value: object) -> bool:
    """Whether a value is a finite number in JSON, and not a boolean."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _number_format(form: object) -> re.Match[str] | None:
    """A number format, such as currency:EUR, read as its base and its digits, or None."""
    return _NUMBER_FORMAT.fullmatch(form) if isinstance(form, str) else None


def _digits_problem(found: re.Match[str], what: str) -> str | None:
    """What is wrong with the digits a number format takes after its colon, or None."""
    form = found[0]
    base, digits = found.groups()
    if digits is None:
        return None
    if base == "integer":
        return (
            f"{what} has the format {form!r}, and integer takes no digits: it is number:1.0-0 "
            "already, so write integer, or number with the digits you want."
        )
    read = _DIGITS.fullmatch(digits)
    if read is None or int(read[2]) > int(read[3]):
        return (
            f"{what} has the format {form!r}, whose digits aren't readable: write the fewest "
            "integer digits (1 to 9), then the fewest and most decimals (0 to 6), such as "
            f"{base}:1.0-0 for whole numbers or {base}:1.2-2 for two decimals."
        )
    return None


def _format_problem(form: object, what: str) -> str | None:
    """What is wrong with an axis's or a donut's number format, or None."""
    if found := _number_format(form):
        return _digits_problem(found, what)
    if _label_kind(form):
        return (
            f"{what} holds numbers, and {form!r} is the format of a label: give {_NUMBER_FORMATS}."
        )
    return f"{what} has the format {form!r}: give {_NUMBER_FORMATS}."


def _schema(spec: dict[str, Any] | None) -> tuple[set[str], set[str]]:
    """The arguments an input schema takes, and those it requires."""
    schema = spec or {}
    return set(schema.get("properties", {})), set(schema.get("required", []))


# How many unknown classes of one screen get a "did you mean": the hint costs a search of every
# class, and a screen a sandbox made up may hold thousands.
_HINTS = 20


class _Checking:
    """One check of one screen for one role: what it found, and the tools it calls."""

    def __init__(self, manifest: dict[str, Any], role: str | None, view: str) -> None:
        self.role = role
        self.view = view
        self.usable = tools.access(manifest, role)
        self.views = {spec["name"] for spec in manifest["views"]}
        self.actions = {spec["name"] for spec in manifest["actions"]}
        self.queries = {spec["name"] for spec in manifest["queries"]}
        self.problems: list[str] = []
        self.called: dict[str, dict[str, Any]] = {}
        self._classes: dict[str, str | None] = {}
        self._icons: dict[str, str] = {}
        self._hinted = 0
        self._fieldsets: dict[int, bool] = {}

    def action(self, name: str) -> dict[str, Any] | None:
        """The action `name` as a form may call it, or None with a problem. A form calls
        actions only: what a query answers, the view itself lays out."""
        if name in self.usable.actions:
            entry = {"kind": "action", "input": self.usable.actions[name]["input_schema"]}
            self.called[name] = entry
            return entry
        if name in self.views:
            why = f'{name} is a view. Open it with data-view="{name}".'
        elif name in self.queries:
            why = (
                f"{name} is a query, and what a form calls shows nobody its answer. Call the "
                "query in the view and lay its answer out; a form with data-view lays the view "
                "out again with the arguments it needs."
            )
        elif name in tools.BUILT_IN:
            why = (
                f"a form calls the actions of its own app only. {name} is one of the tools "
                "every app gives an assistant."
            )
        elif name in self.actions and self.role is None:
            why = f"the app's callers can't use {name}. Take the form out."
        elif name in self.actions:
            why = (
                f"the role {self.role!r} may open this view but not use {name}. Lay the form "
                "out only for the roles that may, with {% if %}."
            )
        else:
            choices = tools.listed(sorted(self.usable.actions)) or "no action"
            why = f"the app has no action {name}. This role may call {choices}."
        self.problems.append(f'data-call="{name}": {why}')
        return None

    def open_view(self, name: str) -> dict[str, Any] | None:
        """The view `name` as a form or a link may lay it out, or None with a problem."""
        if name in self.usable.views:
            entry = {"kind": "view", "input": self.usable.views[name]["input_schema"]}
            self.called[name] = entry
            return entry
        if name in self.views:
            self.problems.append(
                f'data-view="{name}": the role {self.role!r} may not open {name}. Lay the link '
                "out only for the roles that may."
            )
        else:
            self.problems.append(
                f'data-view="{name}": the app has no view {name}. Its views are '
                f"{tools.listed(sorted(self.views))}."
            )
        return None

    def class_problem(self, name: str) -> str | None:
        """Why a class can't be used, worked out once per class of the screen."""
        if name not in self._classes:
            problem = vocabulary.class_problem(name, hint=self._hinted < _HINTS)
            self._hinted += problem is not None
            self._classes[name] = problem
        return self._classes[name]

    def icon_problem(self, name: str) -> str:
        """Why `name` isn't one of Cliquet's icons, worked out once per name of the screen."""
        if name not in self._icons:
            hint = icons.suggestion(name) if self._hinted < _HINTS else ""
            self._hinted += 1
            self._icons[name] = (
                f"data-icon=\"{name[:60]}\" isn't one of Cliquet's icons.{hint} Find one with "
                "find_icons, by a word for what it stands for, such as delete, plus or money."
            )
        return self._icons[name]

    def labelled_by_its_fieldset(self, fieldset: list[Any]) -> bool:
        """Whether a fieldset names its one field, as daisyUI's legend does: worked out once per
        fieldset."""
        key = id(fieldset)
        if key not in self._fieldsets:
            legend = any(isinstance(c, list) and c[0] == "legend" for c in fieldset[2:])
            fields = {
                one[1].get("name") or str(id(one))
                for one, _ in _elements(fieldset[2:])
                if one[0] in _FIELDS
            }
            self._fieldsets[key] = legend and len(fields) == 1
        return self._fieldsets[key]


def check(
    body: object, arguments: object, manifest: dict[str, Any], role: str | None, view: str
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """What breaks a screen's rules for a caller with `role`, and the tools it calls.

    The tools are each action a form calls and each view a form or a link lays out, the view
    itself included, each with its kind and its input schema.
    """
    shape = _shape_problem(body)
    if shape is not None:
        return [shape], {}
    assert isinstance(body, list)
    if not isinstance(arguments, dict):
        return ["the screen's arguments aren't a JSON object"], {}
    checking = _Checking(manifest, role, view)
    problems = checking.problems
    checking.open_view(view)
    ids: dict[str, int] = {}
    labelled_for: set[str] = set()
    fields: list[tuple[list[Any], list[list[Any]]]] = []
    for node, around in _elements(body):
        tag, attributes = node[0], node[1]
        problem = vocabulary.element_problem(tag)
        if problem is not None:
            problems.append(problem)
            continue
        problems += [
            _attribute_problem(node, name)
            for name in attributes
            if not vocabulary.allows(tag, name)
        ]
        for name in attributes.get("class", "").split():
            problem = checking.class_problem(name)
            if problem is not None:
                problems.append(problem)
        if "class" in attributes:
            around_classes = (one[1].get("class", "") for one in around)
            problems += [
                f"{_describe(node)}: {why}"
                for why in vocabulary.unreadable(attributes["class"], around_classes)
            ]
        if "id" in attributes:
            ids[attributes["id"]] = ids.get(attributes["id"], 0) + 1
        if tag == "label" and "for" in attributes:
            labelled_for.add(attributes["for"])
        in_form = any(one[0] == "form" for one in around)
        if tag == "input" and attributes.get("type", "text") not in vocabulary.INPUT_TYPES:
            problems.append(
                f"{_describe(node)}: an input of type {attributes['type']!r} isn't allowed. Use "
                f"one of {tools.listed(sorted(vocabulary.INPUT_TYPES))}."
            )
        if tag == "button":
            kind = attributes.get("type", "submit")
            if kind not in vocabulary.BUTTON_TYPES:
                problems.append(
                    f"{_describe(node)}: a button of type {kind!r} does nothing, since a screen "
                    "runs no script of its own. Use a form with data-call or data-view, and a "
                    "submit button."
                )
            elif not in_form:
                problems.append(
                    f"{_describe(node)} is outside any form, so it does nothing. Put it in a "
                    "<form> with data-call or data-view."
                )
        if tag in _FIELDS:
            choice = tag == "input" and attributes.get("type") in ("radio", "checkbox")
            if not in_form and not choice:
                problems.append(
                    f"{_describe(node)} is outside any form, so what is typed in it goes "
                    "nowhere. Put it in a <form> with data-call or data-view."
                )
            fields.append((node, around))
        if tag == "form":
            if in_form:
                problems.append("a form is inside another form: close the first one before.")
            _form_problems(node, checking)
        if tag == "a":
            _link_problems(node, checking)
        if tag == "table":
            problem = _wide_table_problem(node, around)
            if problem is not None:
                problems.append(problem)
        if tag in ("button", "a") and not _named(node):
            problems.append(
                f"{_describe(node)} shows only an icon, so a screen reader has no name to read. "
                'Give it aria-label, such as aria-label="Close", or words beside the icon.'
            )
        if "data-icon" in attributes and vocabulary.allows(tag, "data-icon"):
            problems += _icon_problems(node, checking)
        if "data-chart" in attributes:
            wrong = chart_problem(attributes["data-chart"])
            if wrong is not None:
                problems.append(wrong)
            if len(node) > 2:
                problems.append(
                    "an element with data-chart must be empty: the renderer draws the chart, and "
                    "a table of its numbers for screen readers, in it."
                )
        if "data-grid" in attributes and vocabulary.allows(tag, "data-grid"):
            if "data-chart" in attributes:
                problems.append(
                    "an element takes data-chart or data-grid, not both: put the chart and the "
                    "grid in a <div> each."
                )
            wrong = grid_problem(attributes["data-grid"])
            if wrong is not None:
                problems.append(wrong)
            if len(node) > 2:
                problems.append(
                    "an element with data-grid must be empty: the renderer draws the table, its "
                    "search, filters and pages in it."
                )
    problems += _tab_problems(body)
    problems += [
        f'id="{name}" is given to {count} elements: an id names one element.'
        for name, count in ids.items()
        if count > 1
    ]
    problems += [
        f'<label for="{name}"> names no element: give the field id="{name}".'
        for name in sorted(labelled_for - set(ids))
    ]
    for node, around in fields:
        if not _labelled(node, around, labelled_for, checking):
            problems.append(
                f'{_describe(node)} has no label. Put a <label for="…"> before it and give it '
                "that id, or wrap it in a <label>, or give it aria-label. A placeholder isn't a "
                "label: it goes away as soon as the person types."
            )
    return list(dict.fromkeys(problems)), checking.called


def _attribute_problem(node: list[Any], name: str) -> str:
    tag = node[0]
    if name.startswith("on"):
        return f"{_describe(node)}: {name} isn't allowed: a screen runs no script of its own."
    if name == "style":
        return (
            f"{_describe(node)}: style isn't allowed: use daisyUI's classes and Cliquet's "
            "layout- classes."
        )
    if name in ("src", "srcset", "action", "formaction", "background", "poster"):
        return f"{_describe(node)}: {name} isn't allowed: a screen loads nothing."
    if name == "href":
        return f"{_describe(node)}: href is only for <a>, to a web or mail address."
    if name.startswith("data-"):
        return (
            f"{_describe(node)}: {name} isn't one of Cliquet's here. A screen understands "
            "data-call, data-view, data-args and data-then on a form, data-view and data-args "
            "on a link, data-icon on a span, data-chart, data-grid on a div, and data-tip."
        )
    return f"{_describe(node)}: <{tag}> doesn't take {name} on a screen."


def _given(attributes: dict[str, str], checking: _Checking) -> dict[str, Any]:
    """The fixed arguments an element's `data-args` gives, with a problem if it can't be read."""
    if "data-args" not in attributes:
        return {}
    found, wrong = _json_object(attributes["data-args"], "data-args")
    if wrong:
        checking.problems.append(wrong)
    return found or {}


def _lay_out_problems(
    target: str, given: dict[str, Any], names: set[str], checking: _Checking
) -> None:
    """What is wrong with laying out the view `target` with these fixed arguments and fields:
    a form's or a link's `data-view`."""
    entry = checking.open_view(target or checking.view)
    if entry is not None:
        takes, _ = _schema(entry["input"])
        where = f'data-view="{target}"'
        checking.problems += _argument_problems(
            where, target or checking.view, takes, set(), given, names
        )


def _form_problems(form: list[Any], checking: _Checking) -> None:
    """What is wrong with one form: what it calls, its arguments and its buttons."""
    attributes = form[1]
    problems = checking.problems
    calls, opens = "data-call" in attributes, "data-view" in attributes
    if calls == opens:
        problems.append(
            "a form needs data-call (to call an action) or data-view (to lay a view out "
            "again), and only one of them."
        )
        return
    given = _given(attributes, checking)
    names: set[str] = set()
    buttons: list[list[Any]] = []
    fields: list[str] = []
    for node, _ in _elements(form[2:]):
        if node[0] == "button":
            buttons.append(node)
        if node[0] in _FIELDS:
            fields.append(node[0] if node[1].get("name") else "")
        if node[0] in (*_FIELDS, "button") and node[1].get("name"):
            names.add(node[1]["name"])
    where = _describe(form)
    # A data-view form whose one field is a named select is sent as soon as the select changes.
    # Only a select: a text field changes when it loses focus, too late or too soon.
    picker = opens and fields == ["select"]
    if not picker and not any(b[1].get("type", "submit") == "submit" for b in buttons):
        problems.append(
            f"{where} has no submit button, so it can never be sent. Add one, or, in a "
            "data-view form, make a named <select> its one field: it is sent as it changes."
        )
    main = sum(1 for b in buttons if _has_class(b, "btn-primary"))
    if main > 1:
        problems.append(
            f"{where} has {main} btn-primary buttons: a form has one main button. Make the "
            "others btn-outline or btn-ghost."
        )
    if opens:
        if "data-then" in attributes:
            problems.append(
                "data-then is for a form with data-call: data-view lays the view out "
                "with the form's own fields."
            )
        _lay_out_problems(attributes["data-view"], given, names, checking)
        return
    tool = attributes["data-call"]
    entry = checking.action(tool)
    if "data-then" in attributes:
        then, wrong = _json_object(attributes["data-then"], "data-then")
        if wrong:
            problems.append(wrong)
        # data-then changes this view's arguments, after the call.
        this = checking.called.get(checking.view)
        if then and this is not None:
            takes, _ = _schema(this["input"])
            problems += _argument_problems(
                f'data-call="{tool}"', checking.view, takes, set(), then, set(), "data-then sets"
            )
    if entry is not None:
        takes, required = _schema(entry["input"])
        upload = checking.usable.actions[tool].get("file_argument")
        if upload is not None and (upload in required or upload in names | set(given)):
            problems.append(
                f'data-call="{tool}": its argument {upload} is a file the person uploads, '
                "and a form can't send one. Leave the action to agents, who ask for the file "
                "with app_ask_for_file."
            )
            required = required - {upload}
        problems += _argument_problems(
            f'data-call="{tool}"', tool, takes | {tools.REMARK}, required, given, names
        )


def _argument_problems(
    where: str,
    tool: str,
    takes: set[str],
    required: set[str],
    given: dict[str, Any],
    names: set[str],
    giving: str = "data-args gives",
) -> list[str]:
    """Fields and fixed arguments a tool doesn't take, and arguments it needs that nothing
    gives. `giving` says where the fixed arguments come from."""
    problems: list[str] = []
    takes_listed = tools.listed(sorted(takes)) or "no arguments"
    wrong_fields = sorted(names - takes)
    if wrong_fields:
        problems.append(
            f"{where}: the field {tools.listed(wrong_fields)} isn't an argument of {tool}, "
            f"which takes {takes_listed}. Name each field after an argument."
        )
    wrong_args = sorted(set(given) - takes)
    if wrong_args:
        problems.append(
            f"{where}: {giving} {tools.listed(wrong_args)}, which {tool} doesn't take. It "
            f"takes {takes_listed}."
        )
    missing = sorted(required - names - set(given))
    if missing:
        problems.append(
            f"{where}: {tool} needs {tools.listed(missing)}, which neither a field nor "
            "data-args gives."
        )
    return problems


def _link_problems(link: list[Any], checking: _Checking) -> None:
    attributes = link[1]
    problems = checking.problems
    if "href" in attributes and "data-view" in attributes:
        problems.append("a link has an href or a data-view, not both.")
        return
    if "href" in attributes:
        href = attributes["href"].strip()
        if not href.lower().startswith(vocabulary.LINK_SCHEMES):
            problems.append(
                f'<a href="{href[:60]}">: a link opens a web or mail address, starting with '
                "https://, http:// or mailto:. To open one of the app's views, write "
                'data-view="its_name".'
            )
    if "data-view" in attributes:
        _lay_out_problems(attributes["data-view"], _given(attributes, checking), set(), checking)
    elif "data-args" in attributes:
        problems.append("data-args on a link needs data-view: the view it lays out.")


def _wide_table_problem(table: list[Any], around: list[list[Any]]) -> str | None:
    """What a table wider than a phone lacks to scroll with its rows' names in sight, or None."""
    columns = _columns(table)
    if columns <= PHONE_COLUMNS:
        return None
    missing = []
    if not around or not _has_class(around[-1], "layout-scroll-x"):
        missing.append('put it in <div class="layout-scroll-x">')
    if not _has_class(table, "table-pin-cols"):
        missing.append("give it daisyUI's table-pin-cols")
    if not all([c[0] for c in _cells(row)][:1] == ["th"] for row in _body_rows(table)):
        missing.append("start each row with a <th> that names it")
    if not missing:
        return None
    return (
        f"a table has {columns} columns, and a phone shows {PHONE_COLUMNS}: {', '.join(missing)}"
        ", so it scrolls sideways with its rows' names in sight. Or keep it to a name and one or "
        "two values, or lay the rows out as a daisyUI list."
    )


_TABS = (
    '<div role="tablist" class="tabs tabs-border"><input type="radio" name="tabs" class="tab" '
    'aria-label="First" checked><div class="tab-content">…</div>…</div>'
)


def _radio(node: list[Any]) -> list[Any] | None:
    """A radio tab's radio: the `<input type="radio" class="tab">` itself, or the radio a
    `<label class="tab">` holds. None for anything else."""
    if not _has_class(node, "tab"):
        return None
    if node[0] == "input" and node[1].get("type") == "radio":
        return node
    if node[0] == "label":
        for one, _ in _elements(node[2:]):
            if one[0] == "input" and one[1].get("type") == "radio":
                return one
    return None


def _tab_problems(body: list[Any]) -> list[str]:
    """What breaks daisyUI's radio tabs, which show a `tab-content` only straight after its
    checked tab, inside a `tabs` tablist, while no class of the content sets its display."""
    problems = []
    tablists: dict[str, int] = {}
    for parent in [["body", {}, *body], *(node for node, _ in _elements(body))]:
        children = [child for child in parent[2:] if isinstance(child, list)]
        if not children:
            continue
        radios = [radio for child in children if (radio := _radio(child)) is not None]
        tablist = _has_class(parent, "tabs")
        befores, afters = [None, *children[:-1]], [*children[1:], None]
        for before, child, after in zip(befores, children, afters, strict=True):
            if _radio(child) is not None:
                if tablist and (after is None or not _has_class(after, "tab-content")):
                    problems.append(
                        f"{_describe(child)} isn't followed straight by its "
                        '<div class="tab-content">, so its content never shows. Put each '
                        f"tab's content right after it, as in {_TABS}."
                    )
            elif _has_class(child, "tab-content"):
                if not tablist or before is None or not _has_class(before, "tab"):
                    problems.append(
                        f"{_describe(child)} isn't straight after its tab, inside a tablist "
                        f"with class tabs, so it never shows. Put it right after its tab, as in "
                        f"{_TABS}."
                    )
                shown = [
                    name
                    for name in child[1]["class"].split()
                    if name.split(":")[-1] in vocabulary.SETS_DISPLAY
                ]
                if shown:
                    listed = tools.listed(shown)
                    sets = "sets" if len(shown) == 1 else "set"
                    problems.append(
                        f"{_describe(child)}: {listed} {sets} its display, so it shows even while "
                        f'its tab isn\'t checked. Keep it to class="tab-content", with colours '
                        f"such as bg-base-100, and put {listed} on a <div> inside it."
                    )
            elif radios and tablist:
                problems.append(
                    f"{_describe(child)} is inside a tablist of radio tabs, which holds only "
                    'each tab and its <div class="tab-content"> after it. Move it into a tab\'s '
                    "content, or out of the tablist."
                )
        if not radios:
            continue
        if not tablist:
            problems.append(
                f"{_describe(radios[0])} is a tab outside a tablist with class tabs, so its "
                f"content never shows. Put the tabs and their contents straight in one, as in "
                f"{_TABS}."
            )
            continue
        names = sorted({radio[1].get("name", "") for radio in radios})
        if len(names) > 1 or names == [""]:
            problems.append(
                f"the radio tabs of {_describe(parent)} don't share one name, so several of "
                'their contents show at once. Give them all the same name, such as name="tabs".'
            )
        checked = sum("checked" in radio[1] for radio in radios)
        if checked != 1:
            problems.append(
                f"{checked or 'no'} tabs of {_describe(parent)} are checked, so "
                + ("no content shows before a click" if checked == 0 else "only the last shows")
                + ". Give checked to one tab only, the one shown first."
            )
        for name in filter(None, names):
            tablists[name] = tablists.get(name, 0) + 1
    problems += [
        f'{count} tablists share the radio name "{name}", so a click in one empties the '
        "others. Give each tablist's radios a name of its own."
        for name, count in tablists.items()
        if count > 1
    ]
    return problems


def _body_rows(table: list[Any]) -> Iterator[list[Any]]:
    """A table's rows outside its <thead>: its own, and those of its <tbody> and <tfoot>."""
    for child in table[2:]:
        if not isinstance(child, list):
            continue
        if child[0] == "tr":
            yield child
        elif child[0] in ("tbody", "tfoot"):
            yield from (row for row in child[2:] if isinstance(row, list) and row[0] == "tr")


def _cells(row: list[Any]) -> list[list[Any]]:
    """A table row's cells, th and td."""
    return [c for c in row[2:] if isinstance(c, list) and c[0] in ("th", "td")]


def _has_class(node: list[Any], name: str) -> bool:
    """Whether an element has this class."""
    return name in node[1].get("class", "").split()


def _columns(table: list[Any]) -> int:
    """How many columns a table's first row spans."""
    for node, _ in _elements(table[2:]):
        if node[0] == "tr":
            total = 0
            for cell in _cells(node):
                span = cell[1].get("colspan", "1")
                total += int(span) if span.isdigit() else 1
            return total
    return 0


def _labelled(
    node: list[Any], around: list[list[Any]], labelled_for: set[str], checking: _Checking
) -> bool:
    """Whether a field has a label a person and a screen reader see."""
    attributes = node[1]
    if node[0] == "input" and attributes.get("type") == "hidden":
        return True
    if _aria_named(attributes):
        return True
    if attributes.get("id") in labelled_for:
        return True
    if any(one[0] == "label" for one in around):
        return True
    fieldsets = [one for one in around if one[0] == "fieldset"]
    return bool(fieldsets) and checking.labelled_by_its_fieldset(fieldsets[-1])


def _icon_problems(span: list[Any], checking: _Checking) -> list[str]:
    """What is wrong with an icon: a name that isn't in Cliquet's set, or something inside it."""
    name = span[1]["data-icon"]
    problems = []
    if name not in icons.tags():
        problems.append(checking.icon_problem(name))
    if len(span) > 2:
        problems.append(
            f'<span data-icon="{name[:60]}"> must be empty: the renderer draws the icon in it. '
            "Put the words beside it, in the button or the heading."
        )
    return problems


def _aria_named(attributes: dict[str, str]) -> bool:
    """Whether an element names itself for screen readers, with aria-label or aria-labelledby."""
    return bool(attributes.get("aria-label", "").strip() or attributes.get("aria-labelledby"))


def _named(node: list[Any]) -> bool:
    """Whether a button or a link has a name a screen reader reads: its words, its
    `aria-label`, or no icon in place of the words."""
    if _aria_named(node[1]) or text(node[2:]):
        return True
    return not any("data-icon" in one[1] for one, _ in _elements(node[2:]))


# The icons a data grid draws itself: its search's magnifier, its Filters button's funnel, and
# the cross that clears its search or removes one of its filters.
_GRID_ICONS = frozenset({"funnel", "search", "x"})


def _drawings(body: list[Any]) -> dict[str, list[list[Any]]]:
    """The drawings of the icons a checked tree names, and of those its grids draw, from
    Cliquet's own copy."""
    drawings = icons.drawings()
    names: set[str] = set()
    for one, _ in _elements(body):
        if "data-icon" in one[1]:
            names.add(one[1]["data-icon"])
        if "data-grid" in one[1]:
            names |= _GRID_ICONS
    return {name: drawings[name] for name in sorted(names) if name in drawings}


def finish(raw: object, manifest: dict[str, Any], role: str | None, view: str) -> dict[str, Any]:
    """The screen a view laid out, checked for a caller with `role`, with what the app's own
    manifest says: the tools it calls, its accent and language, and the drawings of its icons,
    from Cliquet's own copy. Raises `AppError`.

    `raw` is what the engine answered, `{"arguments", "body"}`, which a sandbox may have made up:
    only those two are read from it.
    """
    where = f"view {view}"
    if not isinstance(raw, dict):
        raise AppError([Problem(where, "the screen isn't a JSON object")])
    body, arguments = raw.get("body"), raw.get("arguments")
    problems, called = check(body, arguments, manifest, role, view)
    if problems:
        raise AppError([Problem(where, p) for p in problems])
    assert isinstance(body, list)
    accent = manifest.get("accent")
    language = manifest.get("language")
    return {
        "screen": FORMAT,
        "app": manifest["name"],
        "view": view,
        "arguments": arguments,
        "body": body,
        "tools": called,
        "icons": _drawings(body),
        "accent": accent if isinstance(accent, str) and _ACCENT.fullmatch(accent) else None,
        "language": language if language in vocabulary.LANGUAGES else "en",
        "transaction": None,
        "relay": None,
    }
