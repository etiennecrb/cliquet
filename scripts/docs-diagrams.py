"""Draw the docs' diagrams as SVG files, in the site's style, for light and dark mode.

Each diagram comes in two shapes: a wide one, drawn at the width of the docs' text, and a tall
one, `-tall.svg`, for phones. A page shows them with a `<picture class="cq-diagram">`. The
colours are the site's, from `website/public/styles.css`, and each file follows the reader's
light or dark mode by itself.

    uv run python scripts/docs-diagrams.py
"""

from dataclasses import dataclass
from pathlib import Path

OUT = Path(__file__).parents[1] / "website/docs/assets/diagrams"

STYLE = """
svg{--bg:oklch(99.5% .004 85);--surface:#fff;--line:oklch(88% .015 85);
--ink:oklch(23% .02 285);--muted:oklch(48% .02 285);--p:oklch(50% .2 285);
--ok:oklch(55% .14 155);--no:oklch(56% .2 25);--a:oklch(70% .16 55);--b:oklch(72% .14 175)}
@media (prefers-color-scheme:dark){svg{--bg:oklch(17% .015 285);--surface:oklch(21% .018 285);
--line:oklch(31% .02 285);--ink:oklch(93% .01 285);--muted:oklch(72% .02 285);
--p:oklch(74% .14 285);--ok:oklch(72% .14 155);--no:oklch(70% .17 25)}}
text{font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.node{fill:var(--surface);stroke:var(--line);stroke-width:1.25}
.app{fill:color-mix(in srgb,var(--p) 5%,var(--bg));
stroke:color-mix(in srgb,var(--p) 40%,var(--line));stroke-width:1.25;stroke-dasharray:5 4}
.t{fill:var(--ink);font-size:15px;font-weight:650}
.s{fill:var(--muted);font-size:13px}
.lbl{fill:var(--p);font-size:11px;font-weight:700;letter-spacing:.08em}
.icbg{fill:color-mix(in srgb,var(--p) 12%,var(--surface))}
.ic{fill:none;stroke:var(--p);stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.ln{fill:none;stroke:color-mix(in srgb,var(--p) 55%,var(--line));stroke-width:1.5}
.hd{fill:color-mix(in srgb,var(--p) 55%,var(--line))}
.pill{fill:var(--surface);stroke:color-mix(in srgb,var(--no) 45%,var(--line))}
.pt{fill:var(--no);font-size:12px;font-weight:650;text-anchor:middle}
.okn{fill:color-mix(in srgb,var(--ok) 7%,var(--surface));
stroke:color-mix(in srgb,var(--ok) 55%,var(--line));stroke-width:1.25}
.non{fill:color-mix(in srgb,var(--no) 6%,var(--surface));
stroke:color-mix(in srgb,var(--no) 50%,var(--line));stroke-width:1.25}
.okb{fill:color-mix(in srgb,var(--ok) 16%,var(--surface))}
.okc{stroke:var(--ok)}
.nob{fill:color-mix(in srgb,var(--no) 14%,var(--surface))}
.noc{stroke:var(--no)}
.fa{fill:color-mix(in srgb,var(--a) 50%,var(--surface));stroke:var(--surface);stroke-width:2}
.fb{fill:color-mix(in srgb,var(--b) 50%,var(--surface));stroke:var(--surface);stroke-width:2}
.ft{fill:var(--ink);font-size:10px;font-weight:700;text-anchor:middle}
.dot{fill:var(--surface);stroke:var(--p);stroke-width:2.5}
.undo{fill:none;stroke:var(--no);stroke-width:1.5;stroke-dasharray:4 4}
"""

# Lucide's drawings, on a 24-unit grid.
ICONS = {
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7"/>',
    "bot": '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 4v4M9 13v1M15 13v1"/>',
    "phone": '<rect x="6" y="2.5" width="12" height="19" rx="2.5"/><path d="M11 18.5h2"/>',
    "shield": '<path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/><path d="M9 12l2 2 4-4"/>',
    "sheet": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M3 15h18M9 4v16"/>',
    "pen": '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13 7l4 4"/>',
    "copy": '<rect x="8" y="8" width="12" height="12" rx="2"/>'
    '<path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
    "check": '<path d="M5 12l4 4 10-10"/>',
    "x": '<path d="M6 6l12 12M18 6L6 18"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3M14 9l2 2"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    "undo": '<path d="M9 14L4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    "calendar": '<rect x="3" y="4" width="18" height="18" rx="2"/>'
    '<path d="M16 2v4M8 2v4M3 10h18"/>',
}


@dataclass
class Card:
    """A box with an icon, a title and up to two lines of text."""

    icon: str
    title: str
    lines: tuple[str, ...] = ()
    kind: str = "node"

    def draw(self, x: float, y: float, w: float, h: float) -> str:
        """The card's SVG at a place and size."""
        bg, stroke = {"node": ("icbg", ""), "okn": ("okb", "okc"), "non": ("nob", "noc")}[self.kind]
        out = f'<rect class="{self.kind}" x="{x}" y="{y}" width="{w}" height="{h}" rx="12"/>'
        out += icon(self.icon, x + 30, y + 30, bg=bg, stroke=stroke)
        out += f'<text class="t" x="{x + 54}" y="{y + 35}">{self.title}</text>'
        # A wide card has room for its text on one line.
        lines = (" ".join(self.lines),) if w >= 300 and self.lines else self.lines
        for i, line in enumerate(lines):
            out += f'<text class="s" x="{x + 16}" y="{y + 68 + i * 18}">{line}</text>'
        return out


def icon(name: str, cx: float, cy: float, r: float = 15, bg: str = "icbg", stroke: str = "") -> str:
    """An icon on a round tint."""
    s = r * 1.1
    return (
        f'<circle class="{bg}" cx="{cx}" cy="{cy}" r="{r}"/>'
        f'<use class="ic {stroke}" href="#{name}" x="{cx - s / 2}" y="{cy - s / 2}" '
        f'width="{s}" height="{s}"/>'
    )


def arrow(points: str) -> str:
    """A line through `points`, such as "M0 0H10", ending in an arrowhead."""
    return f'<path class="ln" d="{points}" marker-end="url(#hd)"/>'


def faces(x: float, y: float) -> str:
    """Two people's initials, overlapping."""
    return (
        f'<circle class="fa" cx="{x}" cy="{y}" r="11"/><circle class="fb" cx="{x + 15}" '
        f'cy="{y}" r="11"/><text class="ft" x="{x}" y="{y + 3.5}">S</text>'
        f'<text class="ft" x="{x + 15}" y="{y + 3.5}">A</text>'
    )


def svg(width: int, height: int, title: str, body: str) -> str:
    """A whole SVG file."""
    symbols = "".join(
        f'<symbol id="{name}" viewBox="0 0 24 24">{drawing}</symbol>'
        for name, drawing in ICONS.items()
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img"><title>{title}</title>'
        f"<style>{STYLE}</style><defs>{symbols}"
        '<marker id="hd" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse"><path class="hd" d="M0 0L10 5L0 10z"/>'
        f"</marker></defs>{body}</svg>\n"
    )


YOU = Card("user", "You", ("describe what", "you need"))
ASSISTANT = Card("bot", "Assistant", ("Claude, ChatGPT…", "builds and uses it"))
PARTS = (("sheet", "Data", "every change saved"), ("shield", "Rules", "always hold"))
SCREENS = ("phone", "Screens", "for your team")


def app_row(x: float, y: float, w: float, part: tuple[str, str, str]) -> str:
    """One part of the app, inside its dashed box."""
    ic, title, text = part
    return (
        f'<rect class="node" x="{x}" y="{y}" width="{w}" height="70" rx="10"/>'
        + icon(ic, x + 28, y + 35, r=14)
        + f'<text class="t" x="{x + 52}" y="{y + 30}">{title}</text>'
        f'<text class="s" x="{x + 52}" y="{y + 50}">{text}</text>'
    )


def team(x: float, y: float, w: float, h: float, lines: tuple[str, ...]) -> str:
    """The team's card, with faces for an icon."""
    out = f'<rect class="node" x="{x}" y="{y}" width="{w}" height="{h}" rx="12"/>'
    out += faces(x + 24, y + 30) + f'<text class="t" x="{x + 54}" y="{y + 35}">Team</text>'
    for i, line in enumerate(lines):
        out += f'<text class="s" x="{x + 16}" y="{y + 68 + i * 18}">{line}</text>'
    return out


def how_it_works() -> tuple[str, str]:
    """You ask your assistant, which builds and uses your app; your team uses its screens."""
    title = "How Cliquet works"
    wide = YOU.draw(0, 92, 140, 110) + arrow("M140 147H168") + ASSISTANT.draw(170, 92, 150, 110)
    wide += arrow("M320 147H348")
    wide += '<rect class="app" x="350" y="0" width="210" height="294" rx="16"/>'
    wide += '<text class="lbl" x="366" y="24">YOUR CLIQUET APP</text>'
    for i, part in enumerate((*PARTS, SCREENS)):
        wide += app_row(362, 38 + i * 84, 186, part)
    wide += arrow("M548 245H588") + team(590, 190, 130, 104, ("on a phone or", "in a browser"))

    tall = YOU.draw(0, 0, 360, 92) + arrow("M180 92V118") + ASSISTANT.draw(0, 120, 360, 92)
    tall += arrow("M180 212V238")
    tall += '<rect class="app" x="0" y="240" width="360" height="300" rx="16"/>'
    tall += '<text class="lbl" x="16" y="264">YOUR CLIQUET APP</text>'
    for i, part in enumerate((*PARTS, SCREENS)):
        tall += app_row(12, 278 + i * 84, 336, part)
    tall += arrow("M180 540V566") + team(0, 568, 360, 92, ("on a phone or in a browser",))
    return svg(720, 296, title, wide), svg(360, 662, title, tall)


CHANGE = Card("pen", "A change", ("someone asks to", "book Oak at 3 pm"))
COPY = Card("copy", "On a copy", ("your real data", "isn't touched yet"))
CHECKED = Card("shield", "Checked", ("every rule,", "every time"))
KEPT = Card("check", "Kept", ("saved in the history,", "with who and why"), "okn")
REFUSED = Card("x", "Refused", ("nothing changes,", "and you see why"), "non")


def change() -> tuple[str, str]:
    """A change is tried on a copy, every rule is checked, then it is kept or refused."""
    title = "What happens when something changes"
    wide = CHANGE.draw(0, 60, 150, 110) + arrow("M150 115H183") + COPY.draw(185, 60, 150, 110)
    wide += arrow("M335 115H368") + CHECKED.draw(370, 60, 150, 110)
    wide += arrow("M520 115H534V50H548") + arrow("M534 115V180H548")
    wide += KEPT.draw(550, 0, 170, 100) + REFUSED.draw(550, 130, 170, 100)

    tall = CHANGE.draw(0, 0, 360, 92) + arrow("M180 92V118") + COPY.draw(0, 120, 360, 92)
    tall += arrow("M180 212V238") + CHECKED.draw(0, 240, 360, 92)
    tall += arrow("M180 332V346H88V362") + arrow("M180 346H272V362")
    tall += KEPT.draw(0, 364, 175, 110) + REFUSED.draw(185, 364, 175, 110)
    return svg(720, 232, title, wide), svg(360, 476, title, tall)


CHANGES = (
    ("Booked Oak", "Sam · Mon 9:12", "node"),
    ("Moved to Pine", "Alex · Mon 11:40", "node"),
    ("Cancelled the retro", "Alex · Tue 10:05", "non"),
    ("Undid the cancel", "Sam · Tue 10:20", "okn"),
)


def history() -> tuple[str, str]:
    """Every change is saved, and undoing one adds a new change."""
    title = "The history of an app"
    wide = '<path class="ln" d="M10 80H710" stroke-linecap="round"/>'
    for i, (t, s, kind) in enumerate(CHANGES):
        x = 90 + i * 180
        wide += f'<circle class="dot" cx="{x}" cy="80" r="7"/>'
        wide += f'<rect class="{kind}" x="{x - 82}" y="104" width="164" height="64" rx="10"/>'
        wide += f'<text class="t" x="{x}" y="131" text-anchor="middle" font-size="14">{t}</text>'
        wide += f'<text class="s" x="{x}" y="152" text-anchor="middle">{s}</text>'
    wide += '<path class="undo" d="M626 70C600 20 476 20 452 68" marker-end="url(#hd)"/>'
    wide += '<rect class="pill" x="508" y="20" width="68" height="24" rx="12"/>'
    wide += '<use class="ic noc" href="#undo" x="516" y="25" width="14" height="14"/>'
    wide += '<text class="pt" x="552" y="36">undo</text>'
    wide += '<text class="s" x="710" y="200" text-anchor="end">'
    wide += "Undoing adds a change: nothing is erased.</text>"

    tall = '<path class="ln" d="M100 10V500" stroke-linecap="round"/>'
    for i, (t, s, kind) in enumerate(CHANGES):
        y = 20 + i * 120
        tall += f'<circle class="dot" cx="100" cy="{y + 32}" r="7"/>'
        tall += f'<rect class="{kind}" x="124" y="{y}" width="236" height="64" rx="10"/>'
        tall += f'<text class="t" x="140" y="{y + 27}" font-size="14">{t}</text>'
        tall += f'<text class="s" x="140" y="{y + 48}">{s}</text>'
    tall += '<path class="undo" d="M90 410C30 390 30 300 88 292" marker-end="url(#hd)"/>'
    tall += '<rect class="pill" x="4" y="338" width="68" height="24" rx="12"/>'
    tall += '<use class="ic noc" href="#undo" x="12" y="343" width="14" height="14"/>'
    tall += '<text class="pt" x="48" y="354">undo</text>'
    tall += '<text class="s" x="0" y="530">Undoing adds a change: nothing is erased.</text>'
    return svg(720, 210, title, wide), svg(360, 540, title, tall)


OWNER = Card("user", "Owner", ("Sam: everything,", "and shares the app"))
ROUTINE = Card("key", "A routine", ("a token for this app,", "as member"))
MEMBERS = Card("calendar", "Members", ("book rooms, move", "their own bookings"))
OTHERS = Card("lock", "Everyone else", ("can't open the app", "until you share it"))


def app_box(x: float, y: float, w: float, h: float) -> str:
    """The meeting rooms app, at the centre of its roles."""
    cx = x + w / 2
    return (
        f'<rect class="app" x="{x}" y="{y}" width="{w}" height="{h}" rx="16"/>'
        f'<text class="lbl" x="{cx}" y="{y + 28}" text-anchor="middle">MEETING ROOMS</text>'
        + icon("calendar", cx, y + h / 2 + 2, r=22)
        + f'<text class="s" x="{cx}" y="{y + h - 18}" text-anchor="middle">'
        "rooms and bookings</text>"
    )


def roles() -> tuple[str, str]:
    """One app, and what each role may do in it."""
    title = "Roles in an app"
    wide = app_box(260, 50, 200, 150)
    for card, x, y, ax, ay in (
        (OWNER, 0, 0, 260, 100),
        (ROUTINE, 0, 140, 260, 160),
        (MEMBERS, 520, 0, 460, 100),
        (OTHERS, 520, 140, 460, 160),
    ):
        wide += card.draw(x, y, 200, 110)
        x1 = x + 200 if x == 0 else x
        wide += f'<path class="ln" d="M{x1} {y + 55}L{ax} {ay}"/>'

    tall = app_box(40, 0, 280, 140)
    tall += '<path class="ln" d="M180 140V156M88 156H272M88 156V170M272 156V170"/>'
    for i, card in enumerate((OWNER, MEMBERS, ROUTINE, OTHERS)):
        tall += card.draw((i % 2) * 185, 170 + (i // 2) * 124, 175, 112)
    return svg(720, 252, title, wide), svg(360, 408, title, tall)


def main() -> None:
    """Write every diagram, wide and tall."""
    OUT.mkdir(parents=True, exist_ok=True)
    for draw in (how_it_works, change, history, roles):
        wide, tall = draw()
        name = draw.__name__.replace("_", "-")
        (OUT / f"{name}.svg").write_text(wide)
        (OUT / f"{name}-tall.svg").write_text(tall)


if __name__ == "__main__":
    main()
