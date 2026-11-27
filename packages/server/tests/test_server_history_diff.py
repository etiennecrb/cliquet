import json
import re
from datetime import timedelta
from pathlib import Path

import anyio
import mcp_types as types
import pytest
from starlette.testclient import TestClient

from cliquet import screenshot
from cliquet.context import Caller
from cliquet.store import GitStore, scratch_repository
from cliquet_server import diffs
from cliquet_server.builder.files import MAX_READ_CHARS
from cliquet_server.db import Right
from cliquet_server.diffs import Line, read_diff
from cliquet_server.me import READ

from .server_helpers import (
    APPS,
    NOW,
    a_shop_member,
    act_as,
    app_client,
    call,
    csrf_for,
    git,
    kept,
    signed_in,
    text,
    tools,
)

HISTORY = "/manage/etienne/shop/history"
STAFF = Caller("till", "staff")
OWNER = Caller("etienne", "owner")
SALE = {"sku": "TEA-SEN", "quantity": 2, "receipt": "R-0300"}


SIGNS = {"added": "+", "removed": "-", "context": " "}


def lines_of(page: str, kind: str) -> list[str]:
    """The lines of one kind in a diff page, as the page's HTML holds them: a line as it is,
    and a CSV file's row as its cells joined by commas, with its sign first."""
    lines = re.findall(rf'<span class="patch-line patch-{kind}">(.*?)</span>', page)
    for found, cells in re.findall(rf'<tr class="patch-row patch-({kind})">(.*?)</tr>', page):
        lines.append(SIGNS[found] + ",".join(re.findall(r"<td>(.*?)</td>", cells)))
    return lines


def test_the_owner_opens_a_change_and_reads_its_diff(server) -> None:
    runtime = server.state.apps.live("etienne", "shop").runtime
    sale = runtime.call(STAFF, "record_sale", SALE, remark="Paid in cash")
    price = runtime.call(Caller("etienne", "owner"), "set_price", {"sku": "TEA-SEN", "price": "9"})

    with signed_in(server) as client:
        history = client.get(HISTORY).text
        sold = client.get(f"{HISTORY}/{sale.commit}")
        priced = client.get(f"{HISTORY}/{price.commit}").text

    # Each entry of the history links to its own page.
    assert f'href="{HISTORY}/{sale.commit}"' in history
    assert f'href="{HISTORY}/{price.commit}"' in history
    assert sold.status_code == 200
    # The entry's details, then the file and its lines.
    assert "<code>record_sale</code>" in sold.text
    assert "Paid in cash" in sold.text
    assert "by till" in sold.text
    assert f"<code>{sale.commit[:7]}</code>" in sold.text
    assert "<code>movements.csv</code>" in sold.text
    assert "1 row added, 0 removed." in sold.text
    (added,) = lines_of(sold.text, "added")
    assert added.startswith("+") and added.endswith(",TEA-SEN,-2,sale,R-0300/TEA-SEN,8.50")
    assert lines_of(sold.text, "removed") == []
    assert lines_of(sold.text, "context")
    # A CSV file's change reads as rows under its header, with no line of git's own.
    assert "<th>date</th><th>sku</th>" in sold.text and "@@" not in sold.text
    # A changed line is one removed and one added. The app wrote the file with other line
    # ends than it had: that alone changes no line.
    assert lines_of(priced, "removed") == ["-TEA-SEN,Sencha green tea 100 g,8.50,10"]
    assert lines_of(priced, "added") == ["+TEA-SEN,Sencha green tea 100 g,9.00,10"]
    assert "<code>products.csv</code>" in priced
    # Like every page, it loads nothing from the network.
    assert not re.search(r'<(?:script|link|img)\b[^>]*(?:src|href)="(?:https?:)?//', sold.text)


def test_an_undo_has_its_own_diff_and_names_what_it_undoes(server) -> None:
    runtime = server.state.apps.live("etienne", "shop").runtime
    sale = runtime.call(STAFF, "record_sale", SALE)
    undo = runtime.revert(Caller("etienne", "owner"), sale.commit)

    with signed_in(server) as client:
        page = client.get(f"{HISTORY}/{undo.commit}").text

    (removed,) = lines_of(page, "removed")
    assert removed.endswith(",TEA-SEN,-2,sale,R-0300/TEA-SEN,8.50")
    assert f'href="{HISTORY}/{sale.commit}"' in page


def test_only_the_owner_reaches_a_change(server) -> None:
    runtime = server.state.apps.live("etienne", "shop").runtime
    sale = runtime.call(Caller("sam", "staff"), "record_sale", SALE)
    page = f"{HISTORY}/{sale.commit}"
    member = a_shop_member(server, "staff")
    stranger = server.state.apps.database.person_for(
        "https://example.eu.auth0.com/", "auth0|ada", "ada@example.com", "ada", NOW
    )

    with TestClient(server) as client:
        anonymous = client.get(page, follow_redirects=False)
        act_as(server, client, member)
        as_member = client.get(page)
        act_as(server, client, stranger)
        as_stranger = client.get(page)

    assert anonymous.status_code == 303
    assert anonymous.headers["location"].startswith("/login?next=")
    # Even the member who made the change: the page is the owner's.
    for refused in (as_member, as_stranger):
        assert refused.status_code == 404
        assert "There is no app etienne/shop here." in refused.text
        assert "R-0300" not in refused.text


def test_a_commit_that_is_not_in_the_history_is_missing(server) -> None:
    apps = server.state.apps
    runtime = apps.live("etienne", "shop").runtime
    published = runtime.store.head()
    sale = runtime.call(STAFF, "record_sale", SALE)
    # A call of an open transaction is a commit of this repository, on no live branch.
    transaction = runtime.begin(STAFF)
    open_call = runtime.call(
        STAFF, "record_sale", {**SALE, "receipt": "R-0301"}, transaction=transaction
    )
    # Another app's change.
    other = apps.import_folder(APPS / "stock", "etienne", "tea")
    elsewhere = other.runtime.call(STAFF, "record_sale", {**SALE, "receipt": "R-0302"})
    blob = git(runtime.store.root, "rev-parse", f"{sale.commit}:data/movements.csv").strip()
    # A publish that changed the code only, as the server writes it.
    repo = apps.repo("etienne", "shop")
    code_only = repo.publish(
        repo.head(),
        {"instructions.md": b"Sell tea.\n"},
        version="0123456789abcdef",
        note="Code only",
        login="etienne",
        when=NOW,
    )
    missing = {
        "a publish that changed no data": code_only,
        "an open transaction's call": open_call.commit,
        "another app's change": elsewhere.commit,
        "a file's id": blob,
        "no commit's id": "0123456789abcdef0123456789abcdef01234567",
        "a short id": sale.commit[:12],
        "an id in capitals": sale.commit.upper(),
        "a ref": "HEAD",
        "a revision": "HEAD~1",
        "a parent": f"{sale.commit[:38]}^1",
        "a range": f"{published}..{sale.commit}",
        "an option": "--all",
    }

    with signed_in(server) as client:
        found = client.get(f"{HISTORY}/{sale.commit}")
        answers = {what: client.get(f"{HISTORY}/{commit}") for what, commit in missing.items()}

    assert found.status_code == 200
    assert {what: answer.status_code for what, answer in answers.items()} == dict.fromkeys(
        missing, 404
    )
    for answer in answers.values():
        assert "There is no such change in etienne/shop." in answer.text
        assert "R-030" not in answer.text


def test_the_first_publish_is_listed_with_the_data_it_brought(server) -> None:
    first = server.state.apps.repo("etienne", "shop").head()

    with signed_in(server) as client:
        history = client.get(HISTORY).text
        overview = client.get("/manage/etienne/shop").text
        page = client.get(f"{HISTORY}/{first}").text

    assert f'href="{HISTORY}/{first}"' in history
    # With the publish's note, as an action has its remark.
    assert (
        f'<td><a class="link" href="{HISTORY}/{first}">Published the first version</a>'
        '<p class="text-base-content/60">It brought the data.</p>'
        '<p class="text-base-content/60">Imported from stock</p></td>'
    ) in history
    recent = overview.split('id="recent"')[1].split("</ul>")[0]
    assert "Published the first version</a>" in recent and "etienne, " in recent
    assert '<h2 class="card-title" id="change-title">Published the first version</h2>' in page
    assert "<p>It brought the data.</p>\n    <p>Imported from stock</p>" in page
    # Each file it brought is new, with every line added.
    assert "<code>products.csv</code>" in page and "<code>movements.csv</code>" in page
    assert page.count('<span class="badge badge-ghost">New file</span>') == 2
    assert "+TEA-SEN,Sencha green tea 100 g,8.50,10" in lines_of(page, "added")
    assert lines_of(page, "removed") == []


# An action of the app's own named publish: only names that start with `app_` are Cliquet's.
OWN_PUBLISH = b'''

@shop.action(roles=["owner"])
def publish(ctx: ActionContext, sku: Sku) -> None:
    """Put one of a product in the shop's window."""
    _move(ctx, sku, -1, "window", "WINDOW")
'''
# A new version's migration: it renames a product.
RENAME = b'''\
from pathlib import Path


def migrate() -> None:
    """Call the rooibos a tea."""
    products = Path("products.csv")
    products.write_text(products.read_text().replace("Rooibos 100 g", "Rooibos tea 100 g"))
'''


@pytest.mark.slow
def test_a_publish_is_told_from_the_app_s_own_action_named_publish(server) -> None:
    apps = server.state.apps
    repo = apps.repo("etienne", "shop")
    first = repo.head()
    code = repo.code()
    code["app.py"] += OWN_PUBLISH
    apps.publish("etienne", "shop", code, note="Add a window")
    code_only = repo.head()
    own = apps.live("etienne", "shop").runtime.call(OWNER, "publish", {"sku": "TEA-ROO"})
    apps.publish("etienne", "shop", code, note="Rename", migration=RENAME)
    migrated = repo.head()
    assert own.commit is not None and len({first, code_only, own.commit, migrated}) == 4

    with signed_in(server) as client:
        history = client.get(HISTORY).text
        own_page = client.get(f"{HISTORY}/{own.commit}").text
        migrated_page = client.get(f"{HISTORY}/{migrated}").text
        code_page = client.get(f"{HISTORY}/{code_only}")
    shop = {"app": "etienne/shop", "role": "owner"}
    told = call(server, "etienne", READ, {**shop, "tool": "app_history", "arguments": {}})

    # The app's own action is listed as any other, between the publishes that changed the data.
    # The one that changed only the code made no change to the data.
    links = re.findall(rf'href="{HISTORY}/([0-9a-f]{{40}})"', history)
    assert links == [migrated, own.commit, first]
    assert f'href="{HISTORY}/{own.commit}"><code>publish</code></a></td>' in history
    assert history.count(">Published a new version</a>") == 1
    assert '<p class="text-base-content/60">Rename</p>' in history
    assert history.count(">Published the first version</a>") == 1
    assert '<h2 class="card-title" id="change-title"><code>publish</code></h2>' in own_page
    assert "Published a new version" not in own_page
    assert lines_of(own_page, "added")[-1].endswith(",TEA-ROO,-1,window,WINDOW/TEA-ROO,")
    assert "Published a new version</h2>" in migrated_page
    assert "<p>Its migration changed the data.</p>\n    <p>Rename</p>" in migrated_page
    assert lines_of(migrated_page, "removed") == ["-TEA-ROO,Rooibos 100 g,5.40,8"]
    assert lines_of(migrated_page, "added") == ["+TEA-ROO,Rooibos tea 100 g,5.40,8"]
    assert code_page.status_code == 404
    # app_history tells them apart the same way, for the owner's agent.
    changes = told.structured_content["changes"]
    assert [(one["commit"], one["action"]) for one in changes] == [
        (migrated, "app_publish"),
        (own.commit, "publish"),
        (first, "app_publish"),
    ]
    version = apps.live("etienne", "shop").version
    assert [one["version"] for one in changes][:2] == [version, None]
    assert changes[2]["version"] not in {None, version}


def test_a_long_diff_is_cut_and_says_how_much_is_left_out(server) -> None:
    rows = "".join(f"2026-11-11,TEA-SEN,1,delivery,D-{n:04},\n" for n in range(2500))
    commit = kept(server, {"movements.csv": rows.encode(), "notes.txt": b"one\ntwo\n"})

    with signed_in(server) as client:
        page = client.get(f"{HISTORY}/{commit}").text

    # The line git gives each hunk counts, but shows only as a gap between two hunks.
    shown = re.findall(r'<span class="patch-line patch-\w+">', page)
    assert len(shown) == diffs.MAX_LINES - 1 == 1999
    assert "D-0000" in page and "D-2499" not in page
    # What git wrote and the page left out: the rest of the first file, and the second whole.
    whole = git(server.state.apps.repo("etienne", "shop").folder, "show", "--format=", commit)
    rest = sum(1 for line in whole.splitlines() if line[:1] in {"@", "+", "-", " ", "\\"})
    rest -= 2000 + 4  # each file's `---` and `+++` are no line of its diff
    assert rest > 500
    assert f"Its first 2,000 lines show here, and\n    {rest:,} more are left out." in page
    assert '<a class="link" href="/manage/etienne/shop/download">Download the app</a>' in page
    # Each file still says how many lines it changed, and what of it isn't there.
    assert "2,500 lines added" in page
    assert "<code>notes.txt</code>" in page
    assert "3 more lines of this file's diff are left out." in page


def test_a_binary_file_shows_its_sizes_and_never_its_bytes(server) -> None:
    first = kept(server, {"photo.bin": b"\x00\x01secret-bytes" * 200})
    second = kept(server, {"photo.bin": b"\x00\x02other-bytes" * 300})
    third = kept(server, {"photo.bin": None})

    with signed_in(server) as client:
        added, changed, removed = (
            client.get(f"{HISTORY}/{commit}").text for commit in (first, second, third)
        )

    assert "<code>photo.bin</code>" in added
    assert "A binary file of 3 KB. Its content isn't shown." in added
    assert "A binary file: 3 KB before, 4 KB after. Its content isn't shown." in changed
    assert "A binary file of 4 KB. Its content isn't shown." in removed
    assert "Removed" in removed
    for page in (added, changed, removed):
        assert "secret-bytes" not in page and "other-bytes" not in page
        assert lines_of(page, r"\w+") == []


def test_html_in_a_data_file_shows_as_text(server) -> None:
    script = "<script>alert('sold')</script>"
    runtime = server.state.apps.live("etienne", "shop").runtime
    sale = runtime.call(STAFF, "record_sale", {**SALE, "receipt": script}, remark="<b>why</b>")
    named = kept(server, {"<img src=x>.csv": b"</span><a href='//example.com'>out</a>\n"})

    with signed_in(server) as client:
        sold = client.get(f"{HISTORY}/{sale.commit}").text
        file = client.get(f"{HISTORY}/{named}").text

    assert "<script>alert" not in sold and "<b>why</b>" not in sold
    assert "&lt;script&gt;alert(&#39;sold&#39;)&lt;/script&gt;" in sold
    assert "&lt;b&gt;why&lt;/b&gt;" in sold
    assert "<img" not in file and "<a href='//example.com'>" not in file
    assert "&lt;img src=x&gt;.csv" in file
    assert lines_of(file, "added") == [
        "+&lt;/span&gt;&lt;a href=&#39;//example.com&#39;&gt;out&lt;/a&gt;"
    ]


def scratch(tmp_path: Path, files: dict[str, bytes]) -> GitStore:
    """A repository whose first commit holds these data files."""
    return GitStore(scratch_repository(tmp_path, files, NOW))


def change(store: GitStore, files: dict[str, bytes | None], minutes: int = 1) -> str:
    """Commit a change to the data files, and give its commit."""
    when = NOW + timedelta(minutes=minutes)
    return store.commit(store.head(), files, "change\n\nApp-Action: change\n", "sam", when)


def test_a_diff_tells_added_removed_and_unchanged_lines_apart(tmp_path: Path) -> None:
    store = scratch(tmp_path, {"list.txt": b"milk\neggs\nbread\n", "old.txt": b"gone\n"})
    commit = change(
        store,
        {"list.txt": b"milk\nbutter\nbread\njam", "old.txt": None, "sub/new.txt": b"hello\n"},
    )

    changed, removed, added = read_diff(store, commit)

    assert (changed.name, changed.status, changed.binary) == ("list.txt", "changed", False)
    assert (changed.added, changed.removed, changed.left_out) == (2, 1, 0)
    assert changed.lines == [
        Line("hunk", "@@ -1,3 +1,4 @@"),
        Line("context", " milk"),
        Line("removed", "-eggs"),
        Line("added", "+butter"),
        Line("context", " bread"),
        Line("added", "+jam"),
        Line("note", "\\ No newline at end of file"),
    ]
    assert (removed.name, removed.status, removed.removed) == ("old.txt", "removed", 1)
    assert (added.name, added.status, added.added) == ("sub/new.txt", "added", 1)
    # The first commit's diff is every file, added.
    first = store.parent(commit)
    assert first is not None
    assert [(file.name, file.status) for file in read_diff(store, first)] == [
        ("list.txt", "added"),
        ("old.txt", "added"),
    ]


def test_a_diff_keeps_only_its_first_lines_and_counts_the_rest(tmp_path: Path) -> None:
    store = scratch(tmp_path, {"a.txt": b"", "b.txt": b""})
    commit = change(store, {"a.txt": b"a\n" * 30, "b.txt": b"b\n" * 5})

    first, second = read_diff(store, commit, max_lines=10)

    # The hunk's own line is one of the ten.
    assert [line.kind for line in first.lines] == ["hunk", *["added"] * 9]
    assert (first.added, first.left_out) == (30, 21)
    assert (second.lines, second.added, second.left_out) == ([], 5, 6)


def test_a_diff_keeps_only_the_lines_its_characters_hold(tmp_path: Path) -> None:
    store = scratch(tmp_path, {"a.txt": b"", "b.txt": b""})
    commit = change(store, {"a.txt": b"a" * 40 + b"\n" + b"a\n" * 3, "b.txt": b"b\n"})

    first, second = read_diff(store, commit, max_chars=40)

    # A line that doesn't fit leaves out the rest of its file: the next file's still may.
    assert [line.text for line in first.lines] == ["@@ -0,0 +1,4 @@"]
    assert (first.added, first.left_out) == (4, 4)
    assert [line.text for line in second.lines] == ["@@ -0,0 +1 @@", "+b"]
    assert second.left_out == 0


def test_a_very_long_line_is_cut(tmp_path: Path) -> None:
    store = scratch(tmp_path, {"one.json": b"{}\n"})
    long = b'{"note": "' + "é".encode() * 300_000 + b'"}\n'
    commit = change(store, {"one.json": long + b"next\n"})

    (file,) = read_diff(store, commit)

    removed, added, after = file.lines[1:]
    assert (removed, after) == (Line("removed", "-{}"), Line("added", "+next"))
    assert added.cut and added.kind == "added"
    # The cut falls inside a character: the line ends with the last whole one.
    assert added.text.startswith('+{"note": "ééé') and added.text.endswith("éé")
    assert len(added.text) < diffs.MAX_LINE
    assert (file.added, file.removed, file.left_out) == (2, 1, 0)


def test_a_line_that_looks_like_a_header_stays_a_line(tmp_path: Path) -> None:
    text = b"diff --git a/data/x b/data/x\n@@ -1 +1 @@\n--- a/data/x\n+++ b/data/x\nBinary files\n"
    store = scratch(tmp_path, {"x": b""})
    commit = change(store, {"x": text})
    undo = change(store, {"x": b""}, minutes=2)

    (added,) = read_diff(store, commit)
    (removed,) = read_diff(store, undo)

    assert (added.name, added.binary, added.added) == ("x", False, 5)
    assert [line.text for line in added.lines[1:]] == [
        f"+{line}" for line in text.decode().splitlines()
    ]
    assert {line.kind for line in added.lines[1:]} == {"added"}
    assert (removed.removed, {line.kind for line in removed.lines[1:]}) == (5, {"removed"})


def test_a_control_character_shows_as_its_picture(
    tmp_path: Path,
) -> None:
    store = scratch(tmp_path, {"x.csv": b"a,b\n"})
    hidden = "refund\u202e001\u2066 \u2028\u0085".encode()
    commit = change(store, {"x.csv": b"a,b\r\n\tc\x1b[31m\xff\nd\re\r\r\n" + hidden})

    (file,) = read_diff(store, commit)

    # The first line is as it was but for its end, so it is no change.
    assert [(line.kind, line.text) for line in file.lines[1:]] == [
        ("context", " a,b"),
        ("added", "+\tc\u241b[31m\ufffd"),
        ("added", "+d\u240de\u240d"),
        # A character that turns the text's direction would make the line read as another.
        ("added", "+refund<U+202E>001<U+2066> <U+2028><U+0085>"),
        ("note", "\\ No newline at end of file"),
    ]
    assert (file.added, file.removed) == (3, 0)


def test_a_file_s_name_reads_as_it_is_whatever_it_holds(tmp_path: Path) -> None:
    names = ["caf\u00e9.csv", 'a "b".csv', "back\\slash.csv", "tab\there.csv", "sub/plain.csv"]
    store = scratch(tmp_path, {})
    commit = change(store, dict.fromkeys(names, b"1\n"))

    files = read_diff(store, commit)

    assert sorted(file.name for file in files) == sorted(
        ["caf\u00e9.csv", 'a "b".csv', "back\\slash.csv", "tab\there.csv", "sub/plain.csv"]
    )
    assert all((file.status, file.added) == ("added", 1) for file in files)


def test_a_change_of_line_ends_alone_is_no_change_of_a_line(tmp_path: Path) -> None:
    store = scratch(tmp_path, {"x.csv": b"a,b\nc,d\n", "y.csv": b"1\n"})
    ends = change(store, {"x.csv": b"a,b\r\nc,d\r\n"})
    both = change(store, {"x.csv": b"a,b\nc,d", "y.csv": b"2\n"}, minutes=2)

    assert read_diff(store, ends) == []
    assert [file.name for file in read_diff(store, both)] == ["y.csv"]


needs_browser = pytest.mark.skipif(
    screenshot.find_browser() is None, reason="needs a Chrome, Chromium or Edge"
)


@pytest.mark.browser
@needs_browser
def test_a_diff_with_long_lines_fits_a_phone(server, address: str) -> None:
    wide = "2026-11-11,TEA-SEN,1,delivery,D-0001," + "a long note " * 40
    # A text file, which reads as lines: a CSV file reads as a table, which scrolls the same.
    kept(server, {"notes.txt": b"first\n"})
    commit = kept(server, {"notes.txt": f"first\n{wide}\nshort\n".encode()})
    path = screenshot.find_browser()
    assert path is not None
    browser = screenshot.Browser(path)
    try:
        browser.visit(f"{address}/login?next=/", 390)
        browser.visit(f"{address}{HISTORY}", 390)
        history = browser.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        browser.visit(f"{address}{HISTORY}/{commit}", 390)
        page = browser.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        # The long line scrolls in its own box, and its colour runs to its end.
        box = browser.evaluate(
            "(() => { const box = document.querySelector('.patch');"
            " const line = document.querySelector('.patch-added');"
            " return JSON.stringify([box.scrollWidth - box.clientWidth,"
            " line.offsetWidth - box.clientWidth,"
            " getComputedStyle(line).fontFamily.includes('monospace'),"
            " getComputedStyle(line).backgroundColor !== "
            " getComputedStyle(document.querySelector('.patch-context')).backgroundColor]); })()"
        )
    finally:
        browser.close()

    assert (history, page) == (0, 0)
    scrolls, longer, monospace, coloured = json.loads(box)
    assert scrolls > 500 and longer > 500
    assert (monospace, coloured) == (True, True)


def test_a_csv_change_reads_as_rows_under_its_header(tmp_path: Path) -> None:
    rows = "".join(f"r{n},{n}\n" for n in range(40))
    store = scratch(tmp_path, {"t.csv": f"name,count\n{rows}".encode(), "n.txt": b"a\n"})
    far = change(store, {"t.csv": f"name,count\n{rows}".replace("r30,30", "r30,31").encode()})
    both = change(
        store,
        {"t.csv": f"name,count\n{rows}".replace("r1,1", "r1,2").replace("r30,30", "x").encode()},
        minutes=2,
    )
    renamed = change(store, {"t.csv": f"label,count\n{rows}".encode()}, minutes=3)
    text = change(store, {"n.txt": b"b\n"}, minutes=4)

    def table(commit: str) -> diffs.Table | None:
        (file,) = read_diff(store, commit)
        return diffs.csv_table(store, file)

    # A change far from the header reads the header from the file, and shows the rows above
    # it as a gap.
    shown = table(far)
    assert shown is not None and shown.header == ["name", "count"]
    assert shown.rows[0].gap
    changed = [(row.kind, row.cells) for row in shown.rows if row.kind != "context"]
    assert changed == [("removed", ["r30", "30"]), ("added", ["r30", "31"])]
    # Rows the change left alone between two of its parts show as a gap.
    shown = table(both)
    assert shown is not None and sum(row.gap for row in shown.rows) == 1
    # A changed header, or a file that isn't CSV, reads as lines.
    assert table(renamed) is None and table(text) is None


def test_the_owner_undoes_a_change_from_its_page(server) -> None:
    runtime = server.state.apps.live("etienne", "shop").runtime
    sale = runtime.call(STAFF, "record_sale", SALE)
    (first,) = [one.commit for one in runtime.history(OWNER) if one.action == "app_publish"]
    sam = a_shop_member(server, "staff")

    with signed_in(server) as client:
        page = client.get(f"{HISTORY}/{sale.commit}").text
        published = client.get(f"{HISTORY}/{first}").text
        expired = client.post(f"{HISTORY}/{sale.commit}/undo", data={"csrf": "x"})
        undone = client.post(
            f"{HISTORY}/{sale.commit}/undo",
            data={"csrf": csrf_for(server, client)},
            follow_redirects=False,
        )
        history = client.get(undone.headers["location"]).text
        again = client.post(
            f"{HISTORY}/{sale.commit}/undo", data={"csrf": csrf_for(server, client)}
        ).text
        refused = client.post(f"{HISTORY}/{first}/undo", data={"csrf": csrf_for(server, client)})
    with TestClient(server) as client:
        act_as(server, client, sam)
        stranger = client.post(f"{HISTORY}/{sale.commit}/undo", data={"csrf": "x"})

    # A change's page offers to undo it, and asks first; a publish's page doesn't.
    assert f'action="{HISTORY}/{sale.commit}/undo"' in page and "data-confirm=" in page
    assert "/undo" not in published
    assert expired.status_code == 403
    assert undone.status_code == 303 and "Undone." in history
    # The undo is the newest change, made by the owner, and the sale is gone.
    newest = runtime.history(OWNER)[0]
    assert newest.action == "app_revert" and newest.reverts == sale.commit
    assert newest.caller == "etienne"
    assert "R-0300" not in (runtime.store.root / "data" / "movements.csv").read_text()
    assert refused.status_code == 409 and "restore an earlier version" in refused.text
    # Undoing it again changes nothing, and says so.
    assert "Nothing changed: what that change did is undone already." in again
    assert runtime.history(OWNER)[0].commit == newest.commit
    assert stranger.status_code == 404


def test_a_csv_change_keeps_its_rows_as_they_are(tmp_path: Path) -> None:
    store = scratch(tmp_path, {"t.csv": b"name,note\na,one\n", "u.csv": b"name,note\na,one\n"})
    twice = change(store, {"t.csv": b"name,note\na,one\nname,note\nb,two\n"})
    note = change(store, {"u.csv": b'name,note\na,one\nb,"first\nsecond"\n'}, minutes=2)

    (file,) = read_diff(store, twice)
    shown = diffs.csv_table(store, file)
    # A row that reads as the header is a row like any other below the first line.
    assert shown is not None and shown.added == 2
    assert [row.cells for row in shown.rows if row.kind == "added"] == [
        ["name", "note"],
        ["b", "two"],
    ]
    # A quoted field that runs onto another line can't be read line by line: lines show.
    (file,) = read_diff(store, note)
    assert diffs.csv_table(store, file) is None


# ---------------------------------------------------------------- read_change


def read_change(server, commit: str, app: str = "shop") -> types.CallToolResult:
    """Call read_change on the person's endpoint as etienne, the shop's owner, who builds."""
    (answer,) = tools(server, ("read_change", {"app": app, "commit": commit}))
    return answer


def test_the_owner_reads_a_change_with_the_builder(server) -> None:
    runtime = server.state.apps.live("etienne", "shop").runtime
    sale = runtime.call(STAFF, "record_sale", SALE, remark="Paid in cash")
    # The overview names each change's commit, which read_change takes.
    latest = server.state.builder.read_app_overview("etienne", "shop")["latest_changes"][0]
    assert latest["commit"] == sale.commit

    answer = read_change(server, latest["commit"])

    assert not answer.is_error
    read = answer.structured_content
    assert read is not None
    assert {key: read[key] for key in ("app", "commit", "caller", "role", "action", "remark")} == {
        "app": "etienne/shop",
        "commit": sale.commit,
        "caller": "till",
        "role": "staff",
        "action": "record_sale",
        "remark": "Paid in cash",
    }
    assert read["time"] == latest["time"]
    (movements,) = read["files"]
    assert {key: movements[key] for key in ("file", "status", "added", "removed")} == {
        "file": "movements.csv",
        "status": "changed",
        "added": 1,
        "removed": 0,
    }
    # Unified text, as git writes it: no table, even for a CSV file.
    lines = movements["diff"].splitlines()
    assert lines[0].startswith("@@ -")
    (added,) = (line for line in lines if line.startswith("+"))
    assert added.endswith(",TEA-SEN,-2,sale,R-0300/TEA-SEN,8.50")
    assert "cut" not in read


def test_read_change_takes_a_full_commit_of_the_app_s_history(server) -> None:
    runtime = server.state.apps.live("etienne", "shop").runtime
    sale = runtime.call(STAFF, "record_sale", SALE)

    answers = [read_change(server, given) for given in (sale.commit[:7], "HEAD", "f" * 40)]

    for answer in answers:
        assert answer.is_error
        assert "Pass a change's full commit id, as read_app_overview's latest_changes give it." in (
            text(answer)
        )
    assert text(answers[0]).startswith(f"shop's history has no change '{sale.commit[:7]}'.")
    server.state.apps.database.create_app("etienne", "notes", NOW)
    draft = read_change(server, sale.commit, app="notes")
    assert text(draft) == "notes isn't published yet, so its data has no history."


def test_only_the_owner_reads_a_change_over_mcp(server, address: str) -> None:
    runtime = server.state.apps.live("etienne", "shop").runtime
    sale = runtime.call(STAFF, "record_sale", SALE)
    database = server.state.apps.database
    # A member of the shop who builds apps of their own.
    builds = a_shop_member(server, "staff")
    database.grant(builds.login, Right.BUILD)
    _, builder = database.create_token(builds.id, "laptop", NOW)
    # Someone who doesn't build.
    ada = database.person_for(
        "https://example.eu.auth0.com/", "auth0|ada", "ada@example.com", "ada", NOW
    )
    _, user = database.create_token(ada.id, "laptop", NOW)

    async def as_member() -> types.CallToolResult:
        async with app_client(address, "/me/mcp", builder) as client:
            arguments = {"app": "etienne/shop", "commit": sale.commit}
            return await client.call_tool("read_change", arguments)

    async def as_user() -> set[str]:
        async with app_client(address, "/me/mcp", user) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    refused = anyio.run(as_member)
    assert refused.is_error
    assert text(refused).startswith("Only etienne builds etienne/shop.")
    assert "R-0300" not in text(refused)
    assert "read_change" not in anyio.run(as_user)


def test_read_change_marks_a_line_cut_long(server) -> None:
    long = "2026-11-11,TEA-SEN,1," + "x" * (diffs.MAX_LINE + 100)
    commit = kept(server, {"deliveries.csv": f"{long}\nshort\n".encode()})

    read = read_change(server, commit).structured_content

    assert read is not None
    (deliveries,) = read["files"]
    # The agent can't take the line's start for its whole value.
    lines = deliveries["diff"].splitlines()
    assert lines[1] == f"+{long}"[: diffs.MAX_LINE] + diffs.LONG_LINE
    assert lines[2] == "+short"


def test_read_change_is_bounded_and_says_how_much_is_left_out(server) -> None:
    rows = "".join(f"2026-11-11,TEA-SEN,1,delivery,D-{n:04},\n" for n in range(2500))
    commit = kept(server, {"deliveries.csv": rows.encode(), "photo.bin": b"\x00\x01secret" * 200})

    answer = read_change(server, commit)

    read = answer.structured_content
    assert read is not None
    deliveries, photo = read["files"]
    assert len(deliveries["diff"]) <= MAX_READ_CHARS
    assert "D-0000" in deliveries["diff"] and "D-2499" not in deliveries["diff"]
    assert deliveries["added"] == 2500
    shown = len(deliveries["diff"].splitlines())
    # Every line of the file's diff, its hunk's included, that the answer doesn't hold.
    left_out = 2500 + 1 - shown
    assert read["cut"] == (
        f"The diffs are cut to fit the answer: {left_out:,} of their lines are left out."
    )
    # A binary file has its sizes, never its bytes.
    assert photo == {
        "file": "photo.bin",
        "status": "added",
        "bytes_before": None,
        "bytes_after": 1600,
    }
    assert "secret" not in text(answer)
