"""Cliquet's icons: Lucide's, by name, for an app's tile (`App(..., icon=...)`) and for a
screen (`<span data-icon="plus"></span>`).

They are vendored from Lucide by `scripts/vendor-icons.py`, in two files the renderer page
never inlines: each name's tags, to check a name and search the set, and each drawing. The
runtime adds to a screen the drawings of the icons it names, from this copy, never from what
the app's code answered. The renderer draws them, and the server draws an app's tile.
"""

import json
from functools import cache
from importlib.resources import files
from typing import Any

from cliquet import vocabulary

LUCIDE = "1.68.0"
# What Lucide's drawings are made of, on a 24 by 24 frame: the vendoring script refuses an icon
# drawn with anything else, and the renderer and the server draw these and nothing else.
DRAWN: dict[str, frozenset[str]] = {
    "path": frozenset({"d"}),
    "circle": frozenset({"cx", "cy", "r"}),
    "ellipse": frozenset({"cx", "cy", "rx", "ry"}),
    "rect": frozenset({"x", "y", "width", "height", "rx", "ry"}),
    "line": frozenset({"x1", "y1", "x2", "y2"}),
    "polyline": frozenset({"points"}),
    "polygon": frozenset({"points"}),
}
# A few parts are filled with the stroke's colour, such as a dot: `fill="currentColor"`.
SHARED = frozenset({"fill"})


def _vendored(kind: str) -> Any:
    return json.loads((files("cliquet") / "renderer" / f"lucide-{LUCIDE}{kind}.json").read_text())


@cache
def tags() -> dict[str, tuple[str, ...]]:
    """Every icon's name, with the words Lucide files it under."""
    return {name: tuple(words) for name, words in _vendored("").items()}


@cache
def drawings() -> dict[str, list[list[Any]]]:
    """Every icon's drawing, by name: its parts as `[element, {attribute: value}]`."""
    return _vendored("-drawings")


def suggestion(name: object) -> str:
    """A "Did you mean" with the icons most like `name`, to follow a refusal: those spelt like
    it, then those that hold it in their name or tags. Empty when none is."""
    if not isinstance(name, str):
        return ""
    close = vocabulary.closest(name, tags(), n=3)
    close = (close + [found for found in find_icons(name) if found not in close])[:5]
    return f" Did you mean {', '.join(close)}?" if close else ""


def icon_problem(name: str) -> str | None:
    """Why `name` isn't an icon an app may take, with the closest names, or None."""
    if isinstance(name, str) and name in tags():
        return None
    return (
        f"icon={name!r} isn't one of Lucide's icons.{suggestion(name)} Find one with find_icons, "
        "by a word for the app's subject, such as film or money."
    )


def find_icons(text: str) -> list[str]:
    """The icons whose name or tags hold `text`: those whose name holds it first, then those
    that list it sooner among their tags, which Lucide lists from the closest."""
    words = text.strip().lower()
    if not words:
        return []
    ranked = []
    for name, found in tags().items():
        if words in name:
            ranked.append((-1, name))
        else:
            rank = next((index for index, tag in enumerate(found) if words in tag), None)
            if rank is not None:
                ranked.append((rank, name))
    return [name for _, name in sorted(ranked)]


def for_renderer() -> dict[str, list[str]]:
    """What an icon's parts may be, as the renderer page builds them: each element, with its
    attributes."""
    return {element: sorted(names | SHARED) for element, names in DRAWN.items()}
