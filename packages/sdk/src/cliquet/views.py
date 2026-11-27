"""Views: the page that draws every screen, and what a screen carries besides its body.

A view returns a `Screen`: an HTML template and its values. The worker lays it out, and
`cliquet.screens` turns it into a checked tree. This module serves the one renderer page that
draws every screen of every app, and adds to a screen what the server knows: the tools it may
call, the app's accent, the drawings of its icons, the transaction its calls stay in, and how
its calls reach the app.
See the builder's views guide and the Screens principles in ARCHITECTURE.md.
"""

import hashlib
import json
import re
from functools import cache
from html import escape
from importlib.resources import files
from typing import Any

from cliquet import icons, vocabulary
from cliquet.screens import FORMAT

EXTENSION = "io.modelcontextprotocol/ui"
MIME_TYPE = "text/html;profile=mcp-app"
# The app's accent as daisyUI's primary colour, on an element with the class `s-accent` and
# the accent in `--s-accent`: the screens and the server's pages of an app both use it. The
# accent is a dark shade, for white text on it: in the dark theme, a lighter one with dark
# text reads better on a dark page.
ACCENT_CSS = (
    ".s-accent{--color-primary:var(--s-accent);--color-primary-content:#fff}"
    ".s-accent[data-theme=dark]{--color-primary:color-mix(in oklab,var(--s-accent) 62%,white);"
    "--color-primary-content:color-mix(in oklab,var(--s-accent) 45%,black)}"
)


def _asset(name: str) -> str:
    return (files("cliquet") / "renderer" / name).read_text()


def cliquet_css() -> str:
    """Cliquet's base styles for its trusted pages, after daisyUI's: `.s-screen` and the rest."""
    return _asset("cliquet.css")


def bridge_js() -> str:
    """The script that speaks MCP Apps to the host, inlined by each of Cliquet's trusted pages."""
    return _asset("bridge.js")


# What would close an inlined script or style, in any case.
_CLOSES = re.compile(r"</(?=script|style)", re.IGNORECASE)


def _inlined(code: str) -> str:
    """`code` with nothing that ends its tag early: `<\\/` stands for `</` in CSS and JS."""
    return _CLOSES.sub("<\\\\/", code)


def trusted_page(
    title: str, styles: list[str], body: str, settings: dict[str, Any], scripts: list[str]
) -> str:
    """A page of Cliquet's own, all inlined: `settings` set on `window`, then `scripts`."""
    # Each setting is JSON with no `<` left, so that no value can close or comment its tag.
    values = "".join(
        f"window.{name} = {json.dumps(value, separators=(',', ':')).replace('<', '\\u003c')};\n"
        for name, value in settings.items()
    )
    code = [values, *scripts] if values else scripts
    inlined = "".join(f"<script>\n{_inlined(one)}\n</script>\n" for one in code)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n"
        f"<style>\n{_inlined(chr(10).join(styles))}\n</style>\n</head>\n"
        f"<body>\n{body}{inlined}</body>\n</html>\n"
    )


def page_uri(kind: str, page: str) -> str:
    """The `ui://` name of a trusted page of this `kind`, from its content: no stale copy."""
    digest = hashlib.sha256(page.encode()).hexdigest()[:12]
    return f"ui://cliquet/{kind}-{digest}.html"


@cache
def renderer_html() -> str:
    """The page that draws every screen: daisyUI, Chart.js and Cliquet's script, inlined, so it
    loads nothing from the network. It holds no icon: a screen brings the drawings of its own."""
    allowed = {**vocabulary.for_renderer(), "icon_parts": icons.for_renderer(), "format": FORMAT}
    styles = [vocabulary.daisyui_css(), vocabulary.layout_css(), cliquet_css(), ACCENT_CSS]
    chart = _asset(f"chart-{vocabulary.CHART_JS}.umd.min.js")
    return trusted_page(
        "Cliquet screen",
        styles,
        '<div id="screen" class="s-screen"></div>\n',
        {"CLIQUET_VOCABULARY": allowed},
        [chart, bridge_js(), _asset("screen.js")],
    )


@cache
def renderer_uri() -> str:
    """The `ui://` resource every view of every app is drawn by, named by its content."""
    return page_uri("screen", renderer_html())


def relay_calls(
    screen: dict[str, Any], routes: dict[str, tuple[str, str]], fixed: dict[str, Any]
) -> dict[str, Any]:
    """A copy of a screen whose calls go to another server's tools, which relay them to the
    app, as a screen shown by another server than the app's needs. Its body isn't touched.

    `routes` gives, for each kind of call (`action` and `view`), the tool that relays it and
    the argument that names the app's tool there. That tool takes `fixed` too, and the call's
    own arguments under `arguments`.
    """
    kinds = {kind: {"tool": tool, "key": key} for kind, (tool, key) in routes.items()}
    return {**screen, "relay": {"routes": kinds, "fixed": fixed}}


def in_transaction(screen: dict[str, Any], transaction: str) -> dict[str, Any]:
    """A copy of a screen whose every call carries `"app_transaction": transaction`, so the
    clicks on a screen shown in a transaction stay in it."""
    return {**screen, "transaction": transaction}
