"""What a screen may be made of: its elements, attributes and classes.

This is the one list. The check of a screen reads it, and the renderer page is given it, so
the browser builds only what the check allowed. The classes are daisyUI's, read from the
stylesheet the renderer draws with, and Cliquet's own layout classes, which start with `layout-`.
See the builder's views guide and the Screens principles in ARCHITECTURE.md.
"""

import difflib
import re
from collections.abc import Iterable
from functools import cache
from importlib.resources import files
from typing import Any

DAISYUI = "5.8.1"
CHART_JS = "4.6.1"
# The frame a screen is drawn in is about 380 to 700 pixels wide. The vendored stylesheet has
# `sm` at 480 pixels and `md` at 640, and nothing wider.
BREAKPOINTS = {"sm": 480, "md": 640}
NEVER_REACHED = ("lg", "xl", "2xl")
# The languages an app's screens may be written in. The renderer writes its own words, such as
# a grid's Search and Filters, and its numbers and dates, in the app's: screen.js has them.
LANGUAGES = ("en", "fr")

# Every element a screen may use, with its own attributes. Any element also takes `GLOBAL`
# and `aria-*`.
ELEMENTS: dict[str, frozenset[str]] = {
    name: frozenset(attributes)
    for name, attributes in {
        # Sections and text
        "div": {"data-chart", "data-grid"},
        "span": {"data-icon"},
        "p": set(),
        "section": set(),
        "article": set(),
        "header": set(),
        "footer": set(),
        "nav": set(),
        "aside": set(),
        "figure": {"data-chart"},
        "figcaption": set(),
        "h1": set(),
        "h2": set(),
        "h3": set(),
        "h4": set(),
        "strong": set(),
        "em": set(),
        "b": set(),
        "i": set(),
        "s": set(),
        "u": set(),
        "small": set(),
        "mark": set(),
        "sub": set(),
        "sup": set(),
        "abbr": set(),
        "code": set(),
        "kbd": set(),
        "pre": set(),
        "blockquote": set(),
        "q": set(),
        "time": {"datetime"},
        "br": set(),
        "hr": set(),
        "wbr": set(),
        # Lists and tables
        "ul": set(),
        "ol": {"start", "reversed"},
        "li": {"value"},
        "dl": set(),
        "dt": set(),
        "dd": set(),
        "table": set(),
        "caption": set(),
        "thead": set(),
        "tbody": set(),
        "tfoot": set(),
        "tr": set(),
        "th": {"colspan", "rowspan", "scope", "abbr"},
        "td": {"colspan", "rowspan"},
        # Forms
        "form": {"data-call", "data-view", "data-args", "data-then", "novalidate"},
        "fieldset": {"disabled"},
        "legend": set(),
        "label": {"for"},
        "input": {
            "type",
            "name",
            "value",
            "placeholder",
            "min",
            "max",
            "step",
            "minlength",
            "maxlength",
            "pattern",
            "required",
            "disabled",
            "readonly",
            "checked",
            "autocomplete",
            "inputmode",
            "size",
        },
        "select": {"name", "required", "disabled", "multiple", "size", "autocomplete"},
        "option": {"value", "selected", "disabled", "label"},
        "optgroup": {"label", "disabled"},
        "textarea": {
            "name",
            "placeholder",
            "rows",
            "minlength",
            "maxlength",
            "required",
            "disabled",
            "readonly",
        },
        "button": {"type", "name", "value", "disabled"},
        # Disclosure, links and measures
        "details": {"open", "name"},
        "summary": set(),
        "a": {"href", "data-view", "data-args"},
        "progress": {"value", "max"},
        "meter": {"value", "min", "max", "low", "high", "optimum"},
    }.items()
}
GLOBAL = frozenset({"class", "id", "title", "role", "hidden", "lang", "dir", "data-tip"})
# The elements that have no end tag.
VOID = frozenset({"br", "hr", "input", "wbr"})
# What an `<input type=...>` may be. A file, a password or an image button isn't.
INPUT_TYPES = frozenset(
    {
        "text",
        "number",
        "date",
        "month",
        "week",
        "time",
        "datetime-local",
        "email",
        "tel",
        "url",
        "search",
        "checkbox",
        "radio",
        "hidden",
        "range",
        "color",
    }
)
BUTTON_TYPES = frozenset({"submit", "reset"})
# The addresses a link opens, through the assistant: never one that runs in the page.
LINK_SCHEMES = ("http://", "https://", "mailto:")

# Why an element a person might reach for isn't on the list.
NOT_ELEMENTS = {
    "script": "a screen runs no JavaScript of its own: Cliquet's renderer handles every form "
    "with data-call and data-view",
    "style": "a screen keeps Cliquet's look: use daisyUI's classes and Cliquet's layout- classes",
    "link": "a screen loads nothing: everything it needs is in Cliquet's renderer",
    "img": "a screen loads nothing from the network: show text, a badge or a chart",
    "svg": 'a screen draws no SVG of its own: write an icon as <span data-icon="plus"></span>, '
    "or use text, a badge, a status or a chart",
    "iframe": "a screen embeds nothing",
    "canvas": "draw a chart with data-chart: Cliquet's renderer makes the canvas",
    "dialog": "a dialog needs JavaScript: use a collapse, a details or another view",
    "template": "write the HTML itself, with Jinja for what repeats",
    "html": "write only what goes inside the screen, without <html>, <head> or <body>",
    "head": "write only what goes inside the screen, without <html>, <head> or <body>",
    "body": "write only what goes inside the screen, without <html>, <head> or <body>",
    "main": "the screen is already the main part of the page: use a div or a section",
    "object": "a screen embeds nothing",
    "embed": "a screen embeds nothing",
    "video": "a screen loads nothing from the network",
    "audio": "a screen loads nothing from the network",
    "datalist": "use a select",
    "output": "show results as text, laid out again by the view",
    "center": "use layout-center",
    "font": "use daisyUI's text colours, <small> and <strong>",
}

# daisyUI components that can't work on a screen, by the start of their classes, and why.
NOT_ON_SCREENS = {
    "modal": "a modal needs JavaScript and covers a frame that grows with its content: use a "
    "collapse, a details or another view",
    "drawer": "a drawer lays out a whole page: a screen is one part of a conversation",
    "dock": "a dock is fixed to the bottom of a page: give each job its own view instead",
    "toast": "a toast is fixed to the corner of a page, out of sight: a refusal shows in its "
    "form by itself",
    "fab": "a floating button is fixed to a page: put the button in its form",
    "theme-controller": "a screen follows the person's light or dark: the accent is set on "
    "App(..., accent=...)",
    "mockup": "a mockup draws a device or a browser: show the data itself",
    "countdown": "a countdown needs a style attribute: show the number as text",
    "radial-progress": "a radial progress needs a style attribute: use a progress",
    "diff": "a diff compares two images: a screen shows no image",
    "hover-3d": "a screen shows no image",
    "hover-gallery": "a screen shows no image",
    "file-input": "a screen takes no files: a person's agent brings data in",
    "calendar": "a calendar needs a JavaScript library: use an input of type date",
    "cally": "a calendar needs a JavaScript library: use an input of type date",
    "pika": "a calendar needs a JavaScript library: use an input of type date",
    "react-day-picker": "a calendar needs a JavaScript library: use an input of type date",
}

# Cliquet's own layout classes. daisyUI has components but no layout: these fill the gap. They
# start with `layout-`, which no class of daisyUI's does, so none clashes with daisyUI's (it has
# its own `stack`). Cliquet's text classes say where text goes, never how it looks: daisyUI
# colours it, and HTML has <small> and <strong>.
PREFIX = "layout-"
_GAPS = {"0": "0", "1": ".25rem", "2": ".5rem", "3": ".75rem", "4": "1rem", "6": "1.5rem"}
LAYOUT: dict[str, str] = {
    "layout-stack": "display:flex;flex-direction:column",
    "layout-row": "display:flex;flex-direction:row;flex-wrap:wrap;align-items:center",
    "layout-grid": "display:grid;grid-template-columns:minmax(0,1fr)",
    # Each makes a grid by itself: `layout-cols-2` alone is a grid of two columns.
    "layout-cols-2": "display:grid;grid-template-columns:repeat(2,minmax(0,1fr))",
    "layout-cols-3": "display:grid;grid-template-columns:repeat(3,minmax(0,1fr))",
    "layout-cols-4": "display:grid;grid-template-columns:repeat(4,minmax(0,1fr))",
    "layout-fit": "display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,12rem),1fr))",
    "layout-span-2": "grid-column:span 2/span 2",
    "layout-span-all": "grid-column:1/-1",
    **{f"layout-gap-{name}": f"gap:{size}" for name, size in _GAPS.items()},
    "layout-between": "justify-content:space-between",
    # In a row, its items gather at the end or the middle; in a cell or a line, its text does.
    "layout-center": "justify-content:center;text-align:center",
    "layout-end": "justify-content:flex-end;text-align:end",
    "layout-top": "align-items:flex-start",
    "layout-baseline": "align-items:baseline",
    "layout-bottom": "align-items:flex-end",
    "layout-nowrap": "flex-wrap:nowrap;white-space:nowrap",
    "layout-grow": "flex:1 1 0%;min-width:0",
    "layout-fill": "width:100%",
    # A long part, such as a history, keeps to 20rem and scrolls: the frame grows with its
    # content, so nothing else bounds it. A wide one, such as many steps, scrolls sideways.
    "layout-scroll": "max-height:20rem;overflow-y:auto",
    "layout-scroll-x": "max-width:100%;overflow-x:auto;overscroll-behavior-x:contain",
    "layout-truncate": "min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap",
    # A note a person typed keeps its line breaks.
    "layout-lines": "white-space:pre-line",
}
# The classes that set `display`, and so show a daisyUI radio tab's content even while its tab
# isn't checked: the layout classes that set it, which are unlayered and so beat daisyUI's, and
# these daisyUI classes, which a browser test measures against its stylesheet.
DAISYUI_DISPLAY = frozenset(
    {
        *("card", "card-actions", "card-body", "card-title", "carousel", "carousel-item"),
        *("chat", "chat-bubble", "chat-footer", "chat-header", "checkbox", "collapse"),
        *("divider", "hero", "hero-content", "image-full", "input", "join", "list"),
        *("megamenu", "menu", "menu-horizontal", "menu-vertical", "navbar", "navbar-center"),
        *("navbar-end", "navbar-start", "rating", "stack", "stat", "stats", "steps-horizontal"),
        *("swap", "tab", "toggle", "vc"),
    }
)
SETS_DISPLAY = DAISYUI_DISPLAY | {name for name, body in LAYOUT.items() if "display:" in body}
# daisyUI's colours, each with a `-content` colour for text on it.
COLOURS = ("neutral", "primary", "secondary", "accent", "info", "success", "warning", "error")
# The mixes of daisyUI's classes whose text can't be read in the dark theme, with the classes to
# write instead. daisyUI's neutral is near-black in both themes, a colour for a background:
# these write text in it. A browser test measures each mix below 1.5:1 in the dark theme and
# its replacement at least 4.5:1 in both, and finds no other mix of a badge, a button, an alert
# or a text colour below 1.5:1 on a base background. The light theme's pale status colours, such
# as text-warning at 1.7:1, read poorly but stay allowed.
UNREADABLE: dict[frozenset[str], str] = {
    frozenset({"badge-soft", "badge-neutral"}): "badge badge-soft",
    frozenset({"badge-outline", "badge-neutral"}): "badge badge-ghost",
    frozenset({"badge-dash", "badge-neutral"}): "badge badge-ghost",
    frozenset({"btn-outline", "btn-neutral"}): "btn btn-outline",
    frozenset({"btn-dash", "btn-neutral"}): "btn btn-dash",
    frozenset({"btn-ghost", "btn-neutral"}): "btn btn-ghost",
    frozenset({"btn-link", "btn-neutral"}): "btn btn-link",
    frozenset({"link-neutral"}): "link",
    **{
        frozenset({name}): "text-base-content/70"
        for name in ("text-neutral", *(f"text-neutral/{n}" for n in range(10, 100, 10)))
    },
}
# A `text-X-content` colour reads only on X's own background, which these components give when
# solid, as `btn-X` alone does: bg-X has no style, and the others lose it to these styles.
ON_COLOUR = ("bg", "btn", "badge", "alert", "chat-bubble")
NOT_SOLID = ("soft", "outline", "dash", "ghost", "link")
_CONTENT = re.compile(rf"text-({'|'.join(COLOURS)})-content(/\d+)?")
# The classes that show a part only below the `sm` breakpoint, or only from it, by the width
# where they hide it. They only hide, so a part keeps its own display where it shows: a
# daisyUI component's, a `layout-` container's or a cell's. So they take no `sm:` or `md:`.
SHOWN_BY_WIDTH = {
    "layout-only-wide": f"width<{BREAKPOINTS['sm']}px",
    "layout-only-narrow": f"width>={BREAKPOINTS['sm']}px",
}
# The space a container leaves between its parts, unless a `layout-gap-N` says otherwise: it
# is given with no specificity, so any `layout-gap-N` wins, an `sm:` or `md:` container's too.
DEFAULT_GAPS = {
    "layout-stack": "1rem",
    "layout-row": ".75rem",
    "layout-grid": "1rem",
    "layout-cols-2": "1rem",
    "layout-cols-3": "1rem",
    "layout-cols-4": "1rem",
    "layout-fit": "1rem",
}

# What a Tailwind utility an agent reaches for is, here.
_MUTED = "daisyUI's text-base-content/60"
_OWN_DISPLAY = (
    f"nothing: a part keeps its own display. To show it only from {BREAKPOINTS['sm']} pixels "
    "wide, use layout-only-wide"
)
_OWN_WIDTH = "a layout-row around it, so that it keeps its own width"
_PRE = "<pre>, which keeps every space, or layout-lines, which keeps the line breaks only"
_TAILWIND = {
    "flex": "layout-row (a row) or layout-stack (a column)",
    "flex-row": "layout-row",
    "flex-col": "layout-stack",
    "flex-wrap": "layout-row, which wraps already",
    "grid": "layout-grid",
    "inline-flex": "layout-row",
    "items-center": "layout-row, which centres its items already",
    "items-start": "layout-top",
    "items-baseline": "layout-baseline",
    "items-end": "layout-bottom",
    "justify-between": "layout-between",
    "justify-end": "layout-end",
    "justify-center": "layout-center",
    "flex-1": "layout-grow",
    "grow": "layout-grow",
    "w-full": "layout-fill",
    "text-right": "layout-end, which aligns a cell's text to the end",
    "text-end": "layout-end, which aligns a cell's text to the end",
    "text-center": "layout-center",
    "tabular-nums": "nothing: a table's figures line up already",
    "whitespace-nowrap": "layout-nowrap",
    "font-bold": "<strong>",
    "font-semibold": "<strong>",
    "font-medium": "<strong>",
    "text-sm": "<small>",
    "text-xs": "<small>",
    "text-lg": "a heading such as <h3>",
    "text-xl": "a heading such as <h2>",
    "text-2xl": "a heading such as <h2>",
    "text-3xl": "daisyUI's stat-value, in a stat",
    "opacity-70": _MUTED,
    "opacity-60": _MUTED,
    "text-gray-500": _MUTED,
    "text-muted": _MUTED,
    "text-muted-foreground": _MUTED,
    "hidden": "the hidden attribute, or layout-only-wide or layout-only-narrow to hide it on a "
    f"phone or from {BREAKPOINTS['sm']} pixels wide",
    "block": _OWN_DISPLAY,
    "inline": _OWN_DISPLAY,
    "inline-block": _OWN_DISPLAY,
    "table-cell": _OWN_DISPLAY,
    "truncate": "layout-truncate",
    "text-ellipsis": "layout-truncate",
    "whitespace-pre-line": "layout-lines",
    "whitespace-pre-wrap": _PRE,
    "whitespace-pre": _PRE,
    "w-fit": _OWN_WIDTH,
    "self-start": _OWN_WIDTH,
    "self-end": "a layout-row layout-end around it",
    "self-center": "a layout-row layout-center around it",
    "sticky": "daisyUI's table-pin-rows in a layout-scroll, to keep a table's header in sight",
    "break-words": "nothing: a long word wraps already",
    "break-all": "nothing: a long word wraps already",
    "sr-only": "!Name it with aria-label instead: a screen reader reads that, and nobody sees it.",
}
_TAILWIND_PATTERNS = [
    (re.compile(r"^gap-(\d+)$"), "layout-gap-{0}"),
    (re.compile(r"^grid-cols-(\d+)$"), "layout-cols-{0}"),
    (re.compile(r"^space-y-(\d+)$"), "layout-stack layout-gap-{0}"),
    (re.compile(r"^space-x-(\d+)$"), "layout-row layout-gap-{0}"),
    (re.compile(r"^col-span-(\d+)$"), "layout-span-2, or layout-span-all"),
    (
        re.compile(r"^-?(m|p)[xytrbl]?-"),
        "!A screen has no margins or padding: space its parts with layout-stack, layout-row or "
        "layout-grid, and layout-gap-N.",
    ),
    (re.compile(r"^overflow-x-(auto|scroll)$"), "layout-scroll-x, which scrolls sideways"),
    (
        re.compile(r"^overflow-(y-)?(auto|scroll)$"),
        "layout-scroll, which keeps a part to 20rem and scrolls it",
    ),
    (
        re.compile(r"^overflow-"),
        "!A part shows all of its content: leave the class out, or use layout-truncate to cut a "
        "line of text short.",
    ),
    (
        re.compile(r"^(h|min-h|max-h)-"),
        "!A part is as tall as its content: leave the height out, or use layout-scroll to keep "
        "a long one to 20rem and scroll it.",
    ),
    (
        re.compile(r"^(w|min-w|max-w|size)-"),
        "!A part takes the width of its column: leave the size out, or use layout-fill for the "
        "whole width.",
    ),
    (
        re.compile(r"^(shadow|rounded|ring|outline)"),
        "!daisyUI's components have their own borders and shadows, such as card-border.",
    ),
    (re.compile(r"^line-clamp-"), "layout-truncate, which keeps the text to one line"),
    (re.compile(r"^(uppercase|lowercase|capitalize)$"), "!Write the text as it should read."),
    (re.compile(r"^order-"), "!Write the parts in the order a phone shows them."),
    (
        re.compile(r"^(absolute|relative|fixed|inset-|top-|bottom-|left-|right-|z-)"),
        "!A screen places nothing by position: lay its parts out with layout-stack, layout-row "
        "or a grid.",
    ),
    (re.compile(r"^divide-"), "!daisyUI's list and table draw a line between their rows."),
]


@cache
def daisyui_css() -> str:
    """daisyUI's stylesheet, as the renderer draws with it."""
    return (files("cliquet") / "renderer" / f"daisyui-{DAISYUI}.css").read_text()


@cache
def daisyui_classes() -> frozenset[str]:
    """Every class daisyUI's stylesheet defines, `sm:` and `md:` variants included, except
    those of the components that can't work on a screen. Its few `hover:` colours are left
    out too: a screen's only variants are its breakpoints, the same for every class."""
    found = {
        re.sub(r"\\(.)", r"\1", name)
        for name in re.findall(r"\.((?:\\.|[A-Za-z0-9_-])+)", daisyui_css())
    }
    return frozenset(
        name
        for name in found
        if _unusable(name) is None
        and not name[0].isdigit()
        and name.rpartition(":")[0] in ("", *BREAKPOINTS)
    )


def _unusable(name: str) -> str | None:
    """Why a class of daisyUI's can't be used on a screen, if it can't."""
    bare = name.split(":")[-1]
    for start, why in NOT_ON_SCREENS.items():
        if bare == start or bare.startswith(start + "-"):
            return why
    return None


@cache
def classes() -> frozenset[str]:
    """Every class a screen may use."""
    cliquet = set(LAYOUT) | set(SHOWN_BY_WIDTH)
    cliquet |= {f"{size}:{name}" for size in BREAKPOINTS for name in LAYOUT}
    return daisyui_classes() | cliquet


@cache
def _sorted_classes() -> tuple[str, ...]:
    return tuple(sorted(classes()))


def closest(name: str, choices: Iterable[str], n: int = 1) -> list[str]:
    """The `n` choices spelt most like `name`, closest first, among those spelt much like
    it: the "did you mean" of an unknown class or icon."""
    return difflib.get_close_matches(name, list(choices), n=n, cutoff=0.75)


def class_problem(name: str, *, hint: bool = True) -> str | None:
    """Why a class can't be used on a screen, with what to write instead, or None. Without
    `hint`, an unknown class gets no "did you mean", which costs a search of every class."""
    if name in classes():
        return None
    prefix, _, bare = name.rpartition(":")
    if prefix in NEVER_REACHED:
        return (
            f"class {name!r} never applies: a screen is at most about 700 pixels wide. Use sm: "
            f"(from {BREAKPOINTS['sm']}px) or md: (from {BREAKPOINTS['md']}px)."
        )
    why = _unusable(name)
    if why is not None:
        return f"class {name!r} doesn't work on a screen: {why}."
    if prefix and prefix not in BREAKPOINTS:
        return (
            f"class {name!r}: a class takes no {prefix}: variant here. Only sm: and md: exist, "
            "on the classes that have them."
        )
    if prefix and bare in classes():
        return f"class {name!r}: {bare} has no {prefix}: variant. Write {bare} alone."
    tailwind = _TAILWIND.get(bare)
    if tailwind is None:
        tailwind = next(
            (
                use.format(*found.groups())
                for pattern, use in _TAILWIND_PATTERNS
                if (found := pattern.match(bare))
            ),
            None,
        )
    if tailwind is not None:
        advice = tailwind[1:] if tailwind.startswith("!") else f"Use {tailwind}."
        return f"class {name!r} is Tailwind's, which a screen doesn't have. {advice}"
    close = closest(name, _sorted_classes()) if hint else []
    suggestion = f" Did you mean {close[0]!r}?" if close else ""
    return (
        f"class {name!r} isn't daisyUI's or Cliquet's.{suggestion} A screen uses daisyUI's classes "
        "and Cliquet's layout- classes only."
    )


def _bare(classes: str) -> dict[str, str]:
    """Each class of a `class` attribute without its `sm:` or `md:`, to the class as written."""
    return {name.rpartition(":")[2]: name for name in classes.split()}


def on_colour(colour: str, classes: str) -> bool:
    """Whether these classes give an element `colour`'s own background."""
    bare = _bare(classes)
    return any(
        f"{part}-{colour}" in bare and not any(f"{part}-{style}" in bare for style in NOT_SOLID)
        for part in ON_COLOUR
    )


def unreadable(classes: str, around: Iterable[str] = ()) -> list[str]:
    """Why an element's classes can't be read in one theme, each with the classes to write
    instead. `around` holds the classes of the elements around it, which may give it the
    background a `text-X-content` colour reads on."""
    bare = _bare(classes)
    problems = [
        " with ".join(written for name, written in bare.items() if name in mix)
        + " colours its text with daisyUI's neutral, near-black in both themes, so it can't be "
        f'read in the dark theme. Write class="{instead}" instead, which reads in both.'
        for mix, instead in UNREADABLE.items()
        if mix.issubset(bare)
    ]
    for name, written in bare.items():
        found = _CONTENT.fullmatch(name)
        if found is None:
            continue
        colour = found[1]
        if not any(on_colour(colour, one) for one in (classes, *around)):
            problems.append(
                f"{written} is the colour for text on {colour}, so on another background it "
                f"can't be read in one theme. Use it only inside bg-{colour} or a solid "
                f"btn-{colour} or badge-{colour}, and text-base-content elsewhere."
            )
    return problems


def element_problem(tag: str) -> str | None:
    """Why an element can't be used on a screen, or None."""
    if tag in ELEMENTS:
        return None
    why = NOT_ELEMENTS.get(tag)
    if why is not None:
        return f"<{tag}> isn't allowed: {why}."
    return f"<{tag}> isn't an element a screen may use."


def allows(tag: str, attribute: str) -> bool:
    """Whether the element `tag` may carry `attribute`."""
    return (
        attribute in GLOBAL
        or attribute in ELEMENTS.get(tag, frozenset())
        or re.fullmatch(r"aria-[a-z]+", attribute) is not None
    )


# A cut-short table cell takes the room its row leaves, instead of growing to its text.
_CUT_CELL = "width:100%;max-width:0"


def layout_css() -> str:
    """Cliquet's layout classes as CSS, with their `sm:` and `md:` variants.

    A refusal the renderer shows in a form (`.s-problem`) keeps its distance from the form's
    fields by a margin, except in a container that spaces its parts already. A table cell
    grows to its text, so a cut-short cell takes the room its row leaves instead. A row aligned at
    the bottom drops a fieldset's bottom padding, so a field lines up with the button beside it.
    """
    rules = [f":where(.{name}){{gap:{gap}}}" for name, gap in DEFAULT_GAPS.items()]
    rules += [f".{name}{{{body}}}" for name, body in LAYOUT.items()]
    spaced = ",".join(f".{name}" for name in DEFAULT_GAPS)
    rules.append(f":is({spaced})>.s-problem{{margin-top:0}}")
    rules.append(f":is(td,th).layout-truncate{{{_CUT_CELL}}}")
    rules.append(".layout-bottom>.fieldset{padding-bottom:0}")
    rules += [
        f"@media ({width}){{.{name}{{display:none!important}}}}"
        for name, width in SHOWN_BY_WIDTH.items()
    ]
    for size, width in BREAKPOINTS.items():
        gaps = "".join(
            f":where(.{size}\\:{name}){{gap:{gap}}}" for name, gap in DEFAULT_GAPS.items()
        )
        inner = "".join(f".{size}\\:{name}{{{body}}}" for name, body in LAYOUT.items())
        spaced = ",".join(f".{size}\\:{name}" for name in DEFAULT_GAPS)
        problem = f":is({spaced})>.s-problem{{margin-top:0}}"
        cell = f":is(td,th).{size}\\:layout-truncate{{{_CUT_CELL}}}"
        bottom = f".{size}\\:layout-bottom>.fieldset{{padding-bottom:0}}"
        rules.append(f"@media (width>={width}px){{{gaps}{inner}{problem}{cell}{bottom}}}")
    return "\n".join(rules)


def for_renderer() -> dict[str, Any]:
    """The list the renderer page builds from, as JSON."""
    return {
        "elements": {name: sorted(attributes) for name, attributes in ELEMENTS.items()},
        "global": sorted(GLOBAL),
        "void": sorted(VOID),
        "input_types": sorted(INPUT_TYPES),
        "button_types": sorted(BUTTON_TYPES),
        "link_schemes": list(LINK_SCHEMES),
    }
