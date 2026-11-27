"""Copy Lucide's icons into the SDK: their names and tags, which check an icon's name and
search the set, and their drawings, which a screen and an app's tile are drawn with.

Run it again only to upgrade Lucide, from the package npm publishes, after changing `LUCIDE` in
`packages/sdk/src/cliquet/icons.py`:

    npm pack lucide-static@1.68.0 --pack-destination /tmp/vendor
    uv run python scripts/vendor-icons.py /tmp/vendor

Each is one compact JSON file, by icon name, rather than one file per icon, and the renderer
page inlines neither. The first holds each name's tags, the words Lucide files it under. The
second holds each drawing as Lucide's own nodes, `[element, {attribute: value}]`, inside a 24
by 24 frame: the renderer and the server build the SVG from them. An icon whose drawing uses an
element or an attribute outside `DRAWN`, in `cliquet.icons`, stops the script, since neither
would draw it.
"""

import json
import sys
import tarfile
from pathlib import Path

from cliquet.icons import DRAWN, LUCIDE, SHARED

SDK = Path(__file__).parents[1] / "packages/sdk/src/cliquet/renderer"


def member(archive: Path, name: str) -> bytes:
    """A file of an npm package's archive, by its path inside the package."""
    with tarfile.open(archive) as tar:
        found = tar.extractfile(f"package/{name}")
        if found is None:
            raise SystemExit(f"{archive.name} has no {name}")
        return found.read()


def compact(value: object) -> str:
    """JSON with no spaces, keys in order, so an upgrade's diff shows only what changed."""
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False) + "\n"


def main(folder: str) -> None:
    """Check the package in `folder`, then write the SDK's files."""
    archive = Path(folder) / f"lucide-static-{LUCIDE}.tgz"
    package = json.loads(member(archive, "package.json"))
    if package.get("version") != LUCIDE:
        raise SystemExit(f"{archive.name} isn't lucide-static {LUCIDE}")
    nodes = json.loads(member(archive, "icon-nodes.json"))
    tags = json.loads(member(archive, "tags.json"))
    if set(nodes) != set(tags):
        raise SystemExit("Lucide's names and tags list different icons")
    for name, drawing in nodes.items():
        for element, attributes in drawing:
            unknown = set(attributes) - DRAWN.get(element, frozenset()) - SHARED
            if element not in DRAWN or unknown:
                raise SystemExit(f"{name} draws <{element}> {sorted(unknown)}: see DRAWN")
            if attributes.get("fill", "currentColor") != "currentColor":
                raise SystemExit(f"{name} fills a part with {attributes['fill']!r}")
    licence = member(archive, "LICENSE")
    for old in SDK.glob("lucide-*.json"):
        old.unlink()
    (SDK / f"lucide-{LUCIDE}.json").write_text(compact(tags))
    (SDK / f"lucide-{LUCIDE}-drawings.json").write_text(compact(nodes))
    (SDK / "lucide-LICENSE").write_bytes(licence)
    written = sorted(SDK.glob("lucide-*"))
    print("\n".join(f"{path}: {len(path.read_bytes()):,} bytes" for path in written))
    print(f"{len(nodes)} icons")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
