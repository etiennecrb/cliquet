"""Copy daisyUI and Chart.js into the SDK, for the page that draws every screen, and daisyUI
into the server, for its own pages, and into the website.

Run it again only to upgrade one of them, from the packages npm publishes:

    npm pack daisyui@5.8.1 chart.js@4.6.1 --pack-destination /tmp/vendor
    uv run python scripts/vendor-screens.py /tmp/vendor

The SDK keeps daisyUI's prebuilt CSS, with its breakpoints moved to the frame a screen is drawn
in, about 380 to 700 pixels wide: `sm` from 640 to 480 pixels and `md` from 768 to 640. The
rules for `lg`, `xl` and `2xl` are dropped, since a screen never reaches them. The server's
pages and the website fill a whole window, so they keep daisyUI's CSS as it comes. Chart.js is
copied as it comes.
"""

import re
import sys
import tarfile
from pathlib import Path

from cliquet.vocabulary import BREAKPOINTS, CHART_JS, DAISYUI, NEVER_REACHED, PREFIX

TARGET = Path(__file__).parents[1] / "packages/sdk/src/cliquet/renderer"
SERVER = Path(__file__).parents[1] / "packages/server/src/cliquet_server/static/vendor"
WEBSITE = Path(__file__).parents[1] / "website/public/vendor"
# daisyUI's own breakpoints, in pixels: it writes each in pixels and in rem.
STOCK = {"sm": 640, "md": 768, "lg": 1024, "xl": 1280, "2xl": 1536}


def _queries(width: int) -> tuple[str, str]:
    """A breakpoint's media query, as daisyUI writes it in pixels and in rem."""
    rem = f"{width / 16:g}rem"
    return f"(width>={width}px)", f"(width>={rem})"


# Each breakpoint a screen reaches, as daisyUI writes it and as Cliquet wants it.
MOVED = {
    old: new
    for size, width in BREAKPOINTS.items()
    for old, new in zip(_queries(STOCK[size]), _queries(width), strict=True)
}
# The breakpoints a screen never reaches.
DROPPED = tuple(query for size in NEVER_REACHED for query in _queries(STOCK[size]))


def member(archive: Path, name: str) -> bytes:
    """A file of an npm package's archive, by its path inside the package."""
    with tarfile.open(archive) as tar:
        found = tar.extractfile(f"package/{name}")
        if found is None:
            raise SystemExit(f"{archive.name} has no {name}")
        return found.read()


def block_end(css: str, start: int) -> int:
    """The index just after the block whose `{` is the first one at or after `start`."""
    depth = 0
    for index in range(css.index("{", start), len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    raise SystemExit("daisyUI's CSS has a block that never closes")


def retune(css: str) -> str:
    """daisyUI's CSS without the breakpoints a screen never reaches, and the others moved."""
    for query in DROPPED:
        pattern = re.compile(r"@media " + re.escape(query) + r"\s*\{")
        while (found := pattern.search(css)) is not None:
            css = css[: found.start()] + css[block_end(css, found.start()) :]
    for old, new in MOVED.items():
        css = css.replace("@media " + old, "@media " + new)
    left = re.findall(r"@media \(width>=[^)]*\)", css)
    unknown = sorted(set(left) - {"@media " + query for query in MOVED.values()})
    if unknown:
        raise SystemExit(f"daisyUI has breakpoints this script doesn't know: {unknown}")
    if re.search(r"\.(lg|xl|2xl)\\:", css):
        raise SystemExit("a rule for lg, xl or 2xl is left outside its media query")
    if re.search(r"\.((sm|md)\\:)?" + re.escape(PREFIX), css):
        raise SystemExit(f"daisyUI now has a class starting with {PREFIX}, which is Cliquet's")
    return css


def main(folder: str) -> None:
    source = Path(folder)
    daisyui = source / f"daisyui-{DAISYUI}.tgz"
    chart = source / f"chart.js-{CHART_JS}.tgz"
    css = member(daisyui, "daisyui.css").decode()
    if not css.startswith(f"/*! 🌼 daisyUI {DAISYUI} "):
        raise SystemExit(f"{daisyui.name} isn't daisyUI {DAISYUI}")
    TARGET.mkdir(parents=True, exist_ok=True)
    for old in TARGET.glob("daisyui-*.css"):
        old.unlink()
    for old in TARGET.glob("chart-*.umd.min.js"):
        old.unlink()
    moved = ", ".join(f"{size} at {width}px" for size, width in BREAKPOINTS.items())
    note = (
        f"/* daisyUI {DAISYUI}, its breakpoints moved by scripts/vendor-screens.py: {moved}, "
        f"and {', '.join(NEVER_REACHED)} dropped. */\n"
    )
    (TARGET / f"daisyui-{DAISYUI}.css").write_text(note + retune(css))
    (TARGET / "daisyui-LICENSE").write_bytes(member(daisyui, "LICENSE"))
    script = member(chart, "dist/chart.umd.min.js")
    # The source map isn't shipped: drop the line that names it.
    script = re.sub(rb"\n?//# sourceMappingURL=\S+\s*$", b"\n", script)
    (TARGET / f"chart-{CHART_JS}.umd.min.js").write_bytes(script)
    (TARGET / "chartjs-LICENSE.md").write_bytes(member(chart, "LICENSE.md"))
    for whole in (SERVER, WEBSITE):
        for old in whole.glob("daisyui-*.css"):
            old.unlink()
        (whole / f"daisyui-{DAISYUI}.css").write_text(css)
        (whole / "daisyui-LICENSE").write_bytes(member(daisyui, "LICENSE"))
    written = [
        *sorted(TARGET.iterdir()),
        *sorted(SERVER.glob("daisyui*")),
        *sorted(WEBSITE.glob("daisyui*")),
    ]
    sizes = {path.name: len(path.read_bytes()) for path in written}
    print("\n".join(f"{name}: {size:,} bytes" for name, size in sizes.items()))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
