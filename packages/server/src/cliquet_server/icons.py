"""Lucide's drawings, which the server inlines in its pages: an app's icon loads nothing.

They are the SDK's copy, vendored by `scripts/vendor-icons.py`, at the version it checks
names against.
"""

from functools import cache

from markupsafe import Markup, escape

from cliquet.icons import drawings

# How Lucide draws: a line in the text's colour, on a 24 by 24 frame.
_FRAME = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="{stroke}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'
    "{more}>{parts}</svg>"
)


def _attributes(values: dict[str, str], dashed: bool = False) -> str:
    """SVG attributes, each value escaped, with underscores as dashes when a name has them."""
    return "".join(
        f' {key.replace("_", "-") if dashed else key}="{escape(value)}"'
        for key, value in values.items()
    )


def framed(parts: Markup, stroke: str = "currentColor", **more: str) -> Markup:
    """Lucide's 24 by 24 frame around an icon's shapes, with any more attributes it takes, as
    a page draws it or as a file of its own."""
    frame = _FRAME.format(stroke=escape(stroke), more=_attributes(more, True), parts=parts)
    return Markup(frame)


def known(name: object) -> bool:
    """Whether `name` is one of the icons the server can draw."""
    return isinstance(name, str) and name in drawings()


@cache
def elements(name: str | None) -> Markup | None:
    """The icon's shapes as SVG elements, every value escaped, or None when there is no such
    icon: for a page to draw, or for a standalone SVG of its own."""
    if name is None or not known(name):
        return None
    return Markup(
        "".join(f"<{part}{_attributes(attributes)}/>" for part, attributes in drawings()[name])
    )


@cache
def icon_svg(name: str | None) -> Markup | None:
    """The icon as inline SVG, every value escaped, or None when there is no such icon."""
    parts = elements(name)
    if parts is None:
        return None
    return framed(parts, aria_hidden="true", focusable="false")
