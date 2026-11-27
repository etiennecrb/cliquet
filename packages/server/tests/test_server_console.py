import gc
import html
import io
import os
import re
import subprocess
import sys
import zipfile
from collections.abc import MutableMapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import pytest
from markupsafe import escape
from starlette.requests import ClientDisconnect
from starlette.testclient import TestClient

from cliquet import views
from cliquet.context import Caller
from cliquet.limits import MAX_BODY, MAX_UPLOAD
from cliquet_server import repos
from cliquet_server.builder import BuilderError
from cliquet_server.builder.drafts import MAX_DATA_FILE, check_path
from cliquet_server.db import AppInfo, PersonInfo, Right
from cliquet_server.executor import SandboxError
from cliquet_server.icons import icon_svg
from cliquet_server.manifest import accent, icon, title
from cliquet_server.web import common
from cliquet_server.web.common import app_identity

from .server_helpers import (
    GIT_ENV,
    NOW,
    a_shop_member,
    act_as,
    csrf_for,
    draft_files,
    git,
    member,
    one_unexpected,
    signed_in,
)

ISSUER = "https://example.eu.auth0.com/"


def a_member(server, login: str = "sam", email: str = "sam@example.com") -> PersonInfo:
    """A person who may use etienne/shop as staff."""
    database = server.state.apps.database
    person = database.person_for(ISSUER, f"auth0|{login}", email, login, NOW)
    return member(database, person, "etienne", "shop", "staff")


def groups(text: str) -> tuple[str, str]:
    """The home page's two groups: the person's own apps and the apps shared with them, each
    empty when the page shows none."""
    own = text.partition('id="own-apps"')[2].split('id="shared-apps"')[0]
    shared = text.partition('id="shared-apps"')[2].split("</main>")[0]
    return own, shared


def test_the_home_page_groups_own_and_shared_apps(server) -> None:
    database = server.state.apps.database
    person = a_member(server)
    database.create_app("sam", "notes", NOW)
    database.mark_published("sam", "notes", NOW, "Notes kept by Sam")
    person = database.grant("sam", Right.BUILD)

    with TestClient(server) as client:
        act_as(server, client, person)
        home = client.get("/").text

    own, shared = groups(home)
    assert "Your apps" in own
    assert 'href="/manage/sam/notes"' in own
    assert "Notes kept by Sam" in own
    assert "owner" in own
    assert "shop" not in own
    assert "Shared with you" in shared
    assert 'href="/apps/etienne/shop"' in shared
    assert "Stock for a small tea shop" in shared
    assert "staff" in shared
    assert "etienne" in shared
    assert "notes" not in shared


def test_only_the_owner_opens_the_console_page(server) -> None:
    person = a_member(server)
    pages = [
        "/manage/etienne/shop",
        "/manage/etienne/shop/views",
        "/manage/etienne/shop/sharing",
        "/manage/etienne/shop/tools",
        "/manage/etienne/shop/history",
    ]

    with TestClient(server) as client:
        anonymous = client.get(pages[0], follow_redirects=False)
        act_as(server, client, person)
        refused = [client.get(page) for page in pages]
        share = client.post(
            "/manage/etienne/shop/share",
            data={"email": "bad@example.com", "role": "staff", "csrf": "x"},
        )
    with signed_in(server) as client:
        owned = [client.get(page) for page in pages]

    assert anonymous.status_code == 303
    assert anonymous.headers["location"].startswith("/login?next=")
    assert all(page.status_code == 404 for page in refused)
    assert all("There is no app etienne/shop here." in page.text for page in refused)
    assert share.status_code == 404
    assert all(page.status_code == 200 for page in owned)


def test_only_a_builder_sees_the_build_and_manage_pages(server) -> None:
    database = server.state.apps.database
    server.state.builder.create_app("etienne", "notes")
    staff = a_member(server)
    restore = f"/manage/etienne/shop/versions/{'0' * 40}/restore"
    pages = [
        "/manage/etienne/shop",
        "/manage/etienne/shop/history",
        "/build/notes/preview",
        "/build/notes/upload",
    ]

    with signed_in(server) as client:
        csrf = csrf_for(server, client)
        # Withdrawn after the sign-in: the next request reads the person again.
        database.withdraw("etienne", Right.BUILD)
        refused = [client.get(page) for page in pages]
        restored = client.post(restore, data={"csrf": csrf})
        called = client.post("/build/notes/preview/owner/rpc", json={})
        home = client.get("/").text
        own_page = client.get("/apps/etienne/shop/owner")
        # Their own app's download stays theirs, for the account page.
        download = client.get("/manage/etienne/shop/download")
        with TestClient(server) as other:
            act_as(server, other, staff)
            member_page = other.get("/apps/etienne/shop/staff")
            member_home = other.get("/").text
        database.grant("etienne", Right.BUILD)
        granted = [client.get(page, follow_redirects=False) for page in pages]

    assert [page.status_code for page in refused] == [404] * len(pages)
    assert "There is no app etienne/shop here." in refused[0].text
    assert restored.status_code == 404
    assert called.status_code == 404 and called.json() == {"error": "there is no app etienne/notes"}
    # A user's console links to neither, and their own app opens as its members' does.
    assert 'href="/?new' not in home and 'href="/manage/' not in home
    assert 'href="/apps/etienne/shop"' in home
    assert own_page.status_code == 200 and 'href="/manage/' not in own_page.text
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    # Their app keeps serving its members. A user with no app of their own sees no such group.
    assert member_page.status_code == 200
    assert 'id="own-apps"' not in member_home and 'id="nav-own"' not in member_home
    assert 'href="/?new' not in member_home and 'href="/apps/etienne/shop"' in member_home
    # A grant holds at once: the preview with no preview running is a page too.
    assert [page.status_code for page in granted] == [200] * len(pages)
    assert 'href="/?new#new-app"' in granted[0].text


def test_the_views_tab_shows_each_role_s_screens(server) -> None:
    with signed_in(server) as client:
        first = client.get("/manage/etienne/shop/views").text
        staff = client.get("/manage/etienne/shop/views?role=staff&view=shelves&width=768").text
        no_role = client.get("/manage/etienne/shop/views?role=boss")
        no_view = client.get("/manage/etienne/shop/views?role=staff&view=takings")

    # The owner's first view, at a phone's width, until something else is chosen.
    assert 'data-src="/apps/etienne/shop/owner/screen?view=till&width=full&embed"' in first
    assert 'data-width="390"' in first
    assert 'data-src="/apps/etienne/shop/staff/screen?view=shelves&width=full&embed"' in staff
    assert 'data-width="768"' in staff
    # A link for each role, and for each of this role's views.
    assert "role=owner" in staff and "role=staff" in staff
    assert "view=till" in staff and "view=delivery" in staff
    assert "view=takings" not in staff
    assert "This is the live app." in staff and "changes its data" in staff
    assert no_role.status_code == 404
    assert no_view.status_code == 404


def test_the_pages_load_nothing_from_the_network(server) -> None:
    person = a_member(server)
    owner_pages = [
        "/",
        "/?new",
        "/assistants",
        "/manage/etienne/shop",
        "/manage/etienne/shop/views",
        "/manage/etienne/shop/sharing",
        "/manage/etienne/shop/tools",
        "/manage/etienne/shop/history",
        "/apps/etienne/shop/owner",
        "/tokens",
        "/account",
        "/build/shop/preview/owner",
    ]
    with TestClient(server) as client:
        # A page signed out: the home page sends to sign-in.
        pages = [client.get("/nowhere").text]
        act_as(server, client, person)
        pages.append(client.get("/apps/etienne/shop/staff").text)
        # The page a deleted account ends on, signed out.
        form = {"csrf": csrf_for(server, client), "login": person.login}
        pages.append(client.post("/account/delete", data=form).text)
        assert "Your account is deleted" in pages[-1]
    with signed_in(server) as client:
        server.state.builder.start_preview("etienne", "shop")
        pages += [client.get(page).text for page in owner_pages]
        assert "Nothing here is live" in pages[-1]
        css = client.get("/static/vendor/daisyui-5.8.1.css")
        htmx = client.get("/static/vendor/htmx-2.0.11.min.js")

    # Every file a page loads is the server's own, at a root-relative address.
    for text in pages:
        loads = re.findall(r'<(?:script|link)\b[^>]*\b(?:src|href)="([^"]+)"', text)
        assert all(urlsplit(url).scheme == urlsplit(url).netloc == "" for url in loads), loads
        assert all(url.startswith("/") for url in loads), loads
        assert any(url.startswith("/static/") for url in loads), "every page loads its style sheet"
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert htmx.status_code == 200


def test_an_app_s_page_takes_its_accent(server) -> None:
    with signed_in(server) as client:
        page = client.get("/apps/etienne/shop/owner").text
        console = client.get("/manage/etienne/shop").text

    # The app's page is in the app's colour, as its screens are; the console keeps its own.
    assert '<html lang="en" class="s-accent" style="--s-accent: #0f766e">' in page
    assert views.ACCENT_CSS in page
    assert "--s-accent" not in console
    # The accent comes from the sandbox's manifest and goes into a style: only #rrggbb passes.
    assert accent({"accent": "#0F766E"}) == "#0f766e"
    assert accent({"accent": "red; background: url(x)"}) is None
    assert accent({"accent": None}) is None and accent({}) is None


def test_an_app_s_tile_takes_the_accent_of_its_published_manifest(server) -> None:
    database = server.state.apps.database
    person = a_member(server)
    database.create_app("sam", "notes", NOW)
    database.mark_published("sam", "notes", NOW, "Notes kept by Sam")
    shop = '<span class="mark" style="--mark: #0f766e" aria-hidden="true">S</span>'
    shop_sm = '<span class="mark mark-sm" style="--mark: #0f766e" aria-hidden="true">S</span>'
    plain = '<span class="mark" aria-hidden="true">N</span>'

    with TestClient(server) as client:
        act_as(server, client, person)
        member_home = client.get("/").text
    with signed_in(server) as client:
        owner_home = client.get("/").text
        manage = client.get("/manage/etienne/shop").text
        page = client.get("/apps/etienne/shop/owner").text

    # The stock example's accent, kept from the manifest when it was published.
    own, shared = groups(member_home)
    assert shop in shared
    assert plain in own, "an app without an accent keeps Cliquet's colour"
    assert shop in groups(owner_home)[0]
    assert shop_sm in owner_home and shop_sm in manage, "the sidebar"
    assert '<span class="mark mark-lg" style="--mark: #0f766e"' in manage
    assert shop in page


def test_an_app_s_tile_shows_its_icon_on_its_accent(server) -> None:
    apps = server.state.apps
    code = apps.repo("etienne", "shop").code()
    source = code["app.py"].decode()
    code["app.py"] = source.replace(
        'accent="#0f766e",', 'accent="#be123c",\n    icon="clapperboard",'
    ).encode()
    apps.publish("etienne", "shop", code, note="An icon")
    person = a_member(server)

    with TestClient(server) as client:
        act_as(server, client, person)
        member_home = client.get("/").text
    with signed_in(server) as client:
        pages = [
            client.get(page).text
            for page in ("/", "/manage/etienne/shop", "/apps/etienne/shop/owner")
        ]

    drawing = str(icon_svg("clapperboard"))
    assert drawing.startswith('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"')
    assert '<path d="M3 11h18v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>' in drawing
    tile = f'<span class="mark" style="--mark: #be123c" aria-hidden="true">{drawing}</span>'
    assert tile in groups(member_home)[1]
    home, manage, page = pages
    assert tile in groups(home)[0] and tile in page
    assert f'<span class="mark mark-sm" style="--mark: #be123c" aria-hidden="true">{drawing}' in (
        manage
    ), "the sidebar"
    assert f'<span class="mark mark-lg" style="--mark: #be123c" aria-hidden="true">{drawing}' in (
        manage
    )
    # The server draws only the icons it has, whatever a sandbox's manifest says.
    assert icon({"icon": "clapperboard"}) == "clapperboard"
    assert icon({"icon": "<script>"}) is None and icon({"icon": 3}) is None and icon({}) is None
    assert icon_svg("no-such-icon") is None


def test_an_app_reads_as_its_own_title(server) -> None:
    apps = server.state.apps
    with signed_in(server) as client:
        assert "<span>Shop</span>" in client.get("/").text  # its name, with a capital
    code = apps.repo("etienne", "shop").code()
    source = code["app.py"].decode()
    code["app.py"] = source.replace(
        'accent="#0f766e",', 'accent="#0f766e",\n    title="ShopStock & Co",'
    ).encode()
    apps.publish("etienne", "shop", code, note="A title")

    with signed_in(server) as client:
        pages = [
            client.get(page).text
            for page in ("/", "/manage/etienne/shop", "/apps/etienne/shop/owner")
        ]
    for page in pages:
        assert "<span>ShopStock &amp; Co</span>" in page
        assert "<span>Shop</span>" not in page
    assert "<title>ShopStock &amp; Co" in pages[2]
    # The server keeps only a title the SDK would accept, whatever a sandbox's manifest says.
    assert title({"title": "FoodBank HQ"}) == "FoodBank HQ"
    assert title({"title": "x" * 41}) is None and title({"title": 3}) is None
    assert title({}) is None
    # It keeps the form the SDK checked: one character per accented letter, no spaces around.
    assert title({"title": " Re\u0301sultats "}) == "R\u00e9sultats"


def test_an_app_s_tile_letter_is_its_title_s_first_letter() -> None:
    def letter(name: str, title: str | None) -> str:
        info = AppInfo("sam", name, "", datetime.now(UTC), None, None, None, title)
        return app_identity(info)["tile"]["letter"]

    assert letter("shop", None) == "S"
    assert letter("shop", "Épicerie") == "É"
    assert letter("shop", "(Beta) stock") == "B"  # a letter, never the punctuation
    assert letter("shop", "« »") == "S"  # nothing to draw: the name's letter


def test_an_app_published_before_the_registry_kept_its_accent_gets_it_when_it_starts(
    server,
) -> None:
    apps = server.state.apps
    apps.database.set_look("etienne", "shop", None, None, None)
    apps.forget("etienne", "shop")
    before = apps.database.app("etienne", "shop")

    apps.live("etienne", "shop")

    after = apps.database.app("etienne", "shop")
    assert before is not None and before.accent is None
    assert after is not None and after.accent == "#0f766e"


def test_the_tools_tab_groups_tools_by_kind(server) -> None:
    with signed_in(server) as client:
        every = client.get("/manage/etienne/shop/tools").text
        staff = client.get("/manage/etienne/shop/tools?role=staff").text
        nobody = client.get("/manage/etienne/shop/tools?role=boss")

    # Actions, queries, screens, then rules, each in its own section.
    order = [every.index(f'id="{one}"') for one in ["actions", "queries", "screens", "rules"]]
    assert order == sorted(order)
    actions = every.split('id="actions"')[1].split('id="queries"')[0]
    screens = every.split('id="screens"')[1].split('id="rules"')[0]
    assert "record_sale" in actions and "set_price" in actions
    assert "<code>stock</code>" not in actions
    # A screen's preview opens as the first role that sees it, or as the role chosen.
    assert 'href="/manage/etienne/shop/views?role=owner&view=till"' in screens
    assert 'href="/manage/etienne/shop/views?role=owner&view=takings"' in screens
    assert 'href="/manage/etienne/shop/views?role=staff&view=till"' in staff
    assert "Stock never goes below zero" in every
    # A role shows only what it may use; the rules hold for everyone.
    assert "record_sale" in staff
    assert "set_price" not in staff
    assert "Stock never goes below zero" in staff
    assert nobody.status_code == 404


def test_the_console_opens_the_app_in_a_new_tab(server) -> None:
    with signed_in(server) as client:
        text = client.get("/manage/etienne/shop").text

    opener = re.search(r'<a class="btn btn-primary app-open"[^>]*>', text)
    assert opener is not None
    assert 'href="/apps/etienne/shop"' in opener.group(0)
    assert 'target="_blank"' in opener.group(0)
    assert "(opens in a new tab)" in text


def test_the_overview_sums_up_the_app(server) -> None:
    a_member(server)
    with signed_in(server) as client:
        text = client.get("/manage/etienne/shop").text

    # What happened lately comes first: the latest changes, each linked to its page.
    recent = text.split('id="recent"')[1].split("</ul>")[0]
    assert "Published the first version" in recent and "/manage/etienne/shop/history/" in recent
    assert text.index('id="recent"') < text.index('id="summary"')
    # How many of each, on one line, with links to the tabs that list them, and who holds it.
    count = '<a class="link link-hover" href="/manage/etienne/shop/tools#{}"><strong>{}</strong> {}'
    assert count.format("actions", 5, "actions") in text
    assert count.format("rules", 3, "rules") in text
    assert ">You and 1 member</a>" in text
    # No error, no warning.
    assert 'id="overview-errors"' not in text
    # The data files, and each role with what it may use and who holds it.
    assert "<code>products.csv</code>" in text and "<code>movements.csv</code>" in text
    assert "3 actions, 4 queries, 3 screens · you and 1 member" in text
    # The owner's assistant uses their own endpoint; an agent on its own, the app's.
    assert "http://127.0.0.1:8800/me/mcp" in text
    assert "http://127.0.0.1:8800/apps/etienne/shop/mcp" in text
    assert 'href="/tokens?app=etienne/shop"' in text
    assert "/staff/mcp" not in text


def test_the_history_tab_shows_each_change_s_remark(server) -> None:
    runtime = server.state.apps.live("etienne", "shop").runtime
    sale = {"sku": "TEA-SEN", "quantity": 1, "receipt": "R-0950"}
    runtime.call(Caller("etienne", "staff"), "record_sale", sale, remark="Paid in <cash>")
    with signed_in(server) as client:
        text = client.get("/manage/etienne/shop/history").text

    remark = '<code>record_sale</code></a><p class="text-base-content/60">Paid in &lt;cash&gt;</p>'
    assert remark in text


BANNER = "Preview of your draft, on a copy of the data. Nothing here is live."
SALE = {"sku": "TEA-SEN", "quantity": 2, "receipt": "R-0900"}


def call(name: str, arguments: dict) -> dict:
    return {"method": "tools/call", "params": {"name": name, "arguments": arguments}}


def test_the_preview_page(server) -> None:
    person = a_member(server)
    builder = server.state.builder

    with signed_in(server) as client:
        none = client.get("/build/shop/preview/owner")
        first = client.get("/build/shop/preview")
        builder.start_preview("etienne", "shop")
        moved = client.get("/build/shop/preview", follow_redirects=False)
        page = client.get("/build/shop/preview/owner")
        staff = client.get("/build/shop/preview/staff?view=shelves")
        screen = client.get("/build/shop/preview/staff/screen?view=shelves")
        no_role = client.get("/build/shop/preview/boss")
        no_view = client.get("/build/shop/preview/staff?view=takings")
        no_app = client.get("/build/nope/preview/owner")
    with TestClient(server) as client:
        builder.start_preview("etienne", "shop")
        signed_out = client.get("/build/shop/preview/owner", follow_redirects=False)
        act_as(server, client, person)
        refused = [
            client.get(path)
            for path in (
                "/build/shop/preview",
                "/build/shop/preview/staff",
                "/build/shop/preview/staff/screen",
            )
        ]

    # Opening the page never starts a preview: it says how to start one.
    assert none.status_code == 200
    assert "start_preview" in none.text
    assert BANNER not in none.text
    assert "start_preview" in first.text
    assert moved.status_code == 303
    assert moved.headers["location"] == "/build/shop/preview/owner"
    assert BANNER in page.text
    assert 'data-src="/build/shop/preview/owner/screen?view=till&width=full&embed"' in page.text
    assert 'href="/build/shop/preview/staff"' in page.text
    assert "?view=takings" in page.text
    assert 'data-src="/build/shop/preview/staff/screen?view=shelves&width=full&embed"' in staff.text
    assert "?view=takings" not in staff.text
    assert screen.status_code == 200
    assert "cliquetCalls" in screen.text
    assert 'query.has("driver")' not in screen.text
    assert no_role.status_code == 404
    assert no_view.status_code == 404
    assert no_app.status_code == 404
    assert signed_out.status_code == 303
    assert signed_out.headers["location"].startswith("/login?next=")
    assert all(one.status_code == 404 for one in refused)
    assert all("No such app" in one.text for one in refused)
    assert all(BANNER not in one.text for one in refused)


def test_the_preview_rpc(server) -> None:
    person = a_member(server)
    builder = server.state.builder
    rpc = "/build/shop/preview/staff/rpc"
    own = {"origin": "http://testserver"}

    with signed_in(server) as client:
        none = client.post(rpc, json=call("stock", {}), headers=own)
        builder.start_preview("etienne", "shop")
        sale = client.post(rpc, json=call("record_sale", SALE), headers=own)
        listed = client.post(rpc, json={"method": "tools/list"}, headers=own)
        foreign = client.post(
            rpc,
            json=call("record_sale", {**SALE, "receipt": "R-0901"}),
            headers={"origin": "https://elsewhere.example"},
        )
        text = client.post(
            rpc,
            content=b'{"method": "tools/list"}',
            headers={**own, "content-type": "text/plain"},
        )
        no_role = client.post("/build/shop/preview/boss/rpc", json=call("stock", {}), headers=own)
        seen = builder.try_call("etienne", "shop", "stock", role="owner")
    # The server closes its previews when it stops.
    assert builder.previews.find("etienne", "shop") is None
    with TestClient(server) as client:
        builder.start_preview("etienne", "shop")
        anonymous = client.post(rpc, json=call("stock", {}), headers=own)
        act_as(server, client, person)
        stranger = client.post(rpc, json=call("record_sale", SALE), headers=own)

    assert none.status_code == 404
    assert "start_preview" in none.json()["error"]
    assert sale.status_code == 200, sale.text
    assert sale.json().get("isError") is not True
    assert "record_sale" in {tool["name"] for tool in listed.json()["tools"]}
    assert foreign.status_code == 403
    assert text.status_code == 403
    assert no_role.status_code == 404
    assert anonymous.status_code == 401
    assert stranger.status_code == 404
    assert stranger.json() == {"error": "there is no app sam/shop"}
    rows = seen["result"]
    if isinstance(rows, dict) and "result" in rows:
        rows = rows["result"]
    assert next(row["quantity"] for row in rows if row["sku"] == "TEA-SEN") == 9
    live = server.state.apps.live("etienne", "shop").runtime.query_json(
        Caller("etienne", "owner"), "stock", {}
    )
    assert next(row["quantity"] for row in live if row["sku"] == "TEA-SEN") == 11


def test_the_console_tab_is_screens(server) -> None:
    with signed_in(server) as client:
        views = client.get("/manage/etienne/shop/views")
        old = client.get(
            "/manage/etienne/shop/preview?role=staff&view=shelves", follow_redirects=False
        )
        tools = client.get("/manage/etienne/shop/tools").text

    assert views.status_code == 200
    tabs = views.text.split('nav-tabs"')[1].split("</nav>")[0]
    assert (
        '<a class="tab" href="/manage/etienne/shop/views" aria-current="page">Screens</a>' in tabs
    )
    assert ">Preview<" not in tabs
    assert old.status_code in {301, 307, 308}
    assert old.headers["location"] == "/manage/etienne/shop/views?role=staff&view=shelves"
    assert "/manage/etienne/shop/preview" not in tools


COUNT_QUERY = '''

@shop.query(roles=["owner", "staff"])
def movement_count(ctx: QueryContext) -> int:
    """How many stock movements there are."""
    return len(_read(MOVEMENTS))
'''


def test_the_preview_page_says_a_publish_ended_it(server) -> None:
    builder = server.state.builder
    rpc = "/build/shop/preview/staff/rpc"
    own = {"origin": "http://testserver"}

    with signed_in(server) as client:
        builder.start_preview("etienne", "shop")
        assert builder.publish_app("etienne", "shop", "Again")["published"] is True
        page = client.get("/build/shop/preview/staff")
        clicked = client.post(rpc, json=call("stock", {}), headers=own)
        builder.start_preview("etienne", "shop")
        builder.stop_preview("etienne", "shop")
        stopped = client.get("/build/shop/preview/staff")

    assert "Shop was published" in page.text
    assert "start_preview" in page.text
    assert clicked.status_code == 404
    assert clicked.json()["error"] == (
        "shop was published, which ended its preview: "
        "ask your assistant for a new one with start_preview"
    )
    assert "was published" not in stopped.text
    assert "No preview is running" in stopped.text


def test_the_preview_page_follows_the_draft(server) -> None:
    builder = server.state.builder
    rpc = "/build/shop/preview/staff/rpc"
    own = {"origin": "http://testserver"}

    with signed_in(server) as client:
        builder.start_preview("etienne", "shop")
        source = builder.read_file("etienne", "shop", "app.py")["text"]
        builder.write_file("etienne", "shop", "app.py", source + COUNT_QUERY)
        # A call from the page runs the draft as it is now, with no page loaded since the edit.
        counted = client.post(rpc, json=call("movement_count", {}), headers=own).json()
        listed = client.post(rpc, json={"method": "tools/list"}, headers=own).json()
        page = client.get("/build/shop/preview/staff")
        live = server.state.apps.live("etienne", "shop").runtime
        live.call(Caller("etienne", "staff"), "record_sale", SALE)
        changed = client.get("/build/shop/preview/staff").text
        # A draft that doesn't build: the calls run the last version that built.
        builder.write_file("etienne", "shop", "app.py", "this isn't Python\n")
        broken = client.post(rpc, json=call("movement_count", {}), headers=own).json()

    assert counted.get("isError") is not True, counted
    assert "movement_count" in {tool["name"] for tool in listed["tools"]}
    assert page.status_code == 200
    assert "The current data changed since this preview started." not in page.text
    assert "The current data changed since this preview started." in changed
    assert broken.get("isError") is not True, broken


def test_the_preview_link_opens_the_draft_s_first_role(server) -> None:
    builder = server.state.builder

    with signed_in(server) as client:
        builder.start_preview("etienne", "shop")
        source = builder.read_file("etienne", "shop", "app.py")["text"]
        builder.write_file("etienne", "shop", "app.py", source.replace('"owner"', '"boss"'))
        opened = client.get("/build/shop/preview", follow_redirects=False)

    # The roles are the draft's as it is now, not those the preview last ran.
    assert opened.status_code == 303
    assert opened.headers["location"] == "/build/shop/preview/boss"


def form_token(server, client: TestClient) -> str:
    token = server.state.web_auth.csrf_for_session(client.cookies.get("session"))
    assert token is not None
    return token


def upload(server, client: TestClient, path: str, content: bytes, app: str = "books", **form: str):
    fields = {"csrf": form_token(server, client), "path": path, **form}
    return client.post(
        f"/build/{app}/upload",
        data=fields,
        files={"file": ("upload.bin", content, "application/octet-stream")},
        follow_redirects=False,
    )


def a_ledger(size: int) -> bytes:
    line = b'2026-01-01 * "Tea" ; made up\n'
    return (line * (size // len(line) + 1))[:size]


def test_the_owner_uploads_a_data_file(server) -> None:
    builder = server.state.builder
    builder.create_app("etienne", "books")
    content = a_ledger(600_000)

    with signed_in(server) as client:
        page = client.get("/build/books/upload?path=data/ledger.beancount")
        sent = upload(server, client, "data/ledger.beancount", content)
        shown = client.get(sent.headers["location"])

    assert page.status_code == 200
    assert 'enctype="multipart/form-data"' in page.text
    assert 'value="data/ledger.beancount"' in page.text
    assert 'type="file"' in page.text
    assert "Nothing is live until" in page.text
    # Post, redirect, get: the page the person lands on lists the file.
    assert sent.status_code == 303, sent.text
    assert sent.headers["location"].startswith("/build/books/upload?")
    assert draft_files(builder.drafts, "etienne", "books")["data/ledger.beancount"] == content
    assert shown.status_code == 200
    assert "data/ledger.beancount" in shown.text
    assert "586 KB" in shown.text
    assert "Go back to your assistant and tell it the file is there." in shown.text
    assert "Go back to your assistant" not in page.text
    # The preview notices new data, as for any write to the draft.
    assert builder.drafts.data_revision("etienne", "books") >= 2


def test_an_upload_may_pass_the_limit_on_other_bodies(server) -> None:
    builder = server.state.builder
    builder.create_app("etienne", "books")
    content = a_ledger(5_000_000)
    assert MAX_BODY < len(content) < MAX_DATA_FILE

    with signed_in(server) as client:
        sent = upload(server, client, "data/ledger.beancount", content)

    assert sent.status_code == 303, sent.text
    assert draft_files(builder.drafts, "etienne", "books")["data/ledger.beancount"] == content


def test_an_upload_needs_the_owner(server) -> None:
    builder = server.state.builder
    builder.create_app("etienne", "books")
    person = a_member(server)

    with TestClient(server) as client:
        page = client.get("/build/books/upload?path=data/a.csv", follow_redirects=False)
        sent = client.post(
            "/build/books/upload",
            data={"csrf": "x", "path": "data/a.csv"},
            files={"file": ("a.csv", b"a,b\n")},
            follow_redirects=False,
        )
        act_as(server, client, person)
        other_page = client.get("/build/books/upload?path=data/a.csv")
        other = upload(server, client, "data/a.csv", b"a,b\n")

    assert page.status_code == 303
    assert page.headers["location"].startswith("/login?next=")
    assert sent.status_code == 303
    assert sent.headers["location"].startswith("/login?next=")
    for refused in (other_page, other):
        assert refused.status_code == 404
        assert "No such app" in refused.text
    assert "data/a.csv" not in draft_files(builder.drafts, "etienne", "books")
    assert not builder.drafts.exists("sam", "books")


def test_an_upload_needs_the_form_token(server) -> None:
    builder = server.state.builder
    builder.create_app("etienne", "books")
    person = a_member(server)
    stranger = server.state.web_auth.csrf_for_session(server.state.web_auth.session_cookie(person))

    with signed_in(server) as client:
        missing = client.post(
            "/build/books/upload",
            data={"path": "data/a.csv"},
            files={"file": ("a.csv", b"a,b\n")},
        )
        foreign = upload(server, client, "data/a.csv", b"a,b\n", csrf=stranger)

    for refused in (missing, foreign):
        assert refused.status_code == 403
        assert "The form expired" in refused.text
    assert "data/a.csv" not in draft_files(builder.drafts, "etienne", "books")


def test_an_upload_refuses_a_bad_path(server) -> None:
    builder = server.state.builder
    builder.create_app("etienne", "books")
    before = draft_files(builder.drafts, "etienne", "books")

    with signed_in(server) as client:
        answers = {
            path: upload(server, client, path, b"print('hi')\n")
            for path in ("app.py", "data/.x", "../x", "migrate.py", "app.py.lock")
        }

    for path in ("data/.x", "../x"):
        with pytest.raises(BuilderError) as refused:
            check_path(path)
        assert str(escape(str(refused.value))) in answers[path].text
    assert "isn&#39;t a data file" in answers["app.py"].text
    for answer in answers.values():
        assert answer.status_code == 400
    assert draft_files(builder.drafts, "etienne", "books") == before


def test_an_upload_stops_at_20_mb(server) -> None:
    builder = server.state.builder
    builder.create_app("etienne", "books")
    before = draft_files(builder.drafts, "etienne", "books")

    with signed_in(server) as client:
        over = upload(server, client, "data/big.csv", b"x" * (MAX_DATA_FILE + 1))
        whole = upload(server, client, "data/big.csv", b"x" * (MAX_DATA_FILE + 1_100_000))
        # The page's own form names the path in its address, which keeps it on the page.
        page = client.get("/build/books/upload?path=data/big.csv")
        action = re.search(r'<form class="upload-form" method="post" action="([^"]+)"', page.text)
        assert action is not None
        named = client.post(
            html.unescape(action.group(1)),
            content=b"x" * (MAX_UPLOAD + 1),
            headers={"content-type": "multipart/form-data; boundary=x"},
        )
        chunked = client.post(
            "/build/books/upload",
            content=iter([b"--x\r\n"]),
            headers={"content-type": "multipart/form-data; boundary=x"},
        )

    assert over.status_code == 413
    assert f"{MAX_DATA_FILE:,}" in over.text
    # Over the whole upload's limit, the body isn't read, and the page still says why.
    for refused in (whole, named):
        assert refused.status_code == 413
        assert refused.headers["content-type"].startswith("text/html")
        assert f"{MAX_DATA_FILE:,}" in refused.text
    assert 'value="data/big.csv"' in named.text
    assert chunked.status_code == 411
    assert draft_files(builder.drafts, "etienne", "books") == before


def test_a_published_app_takes_no_upload(server) -> None:
    builder = server.state.builder

    with signed_in(server) as client:
        page = client.get("/build/shop/upload?path=data/products.csv")
        sent = upload(server, client, "data/products.csv", b"sku\n", app="shop")

    for answer in (page, sent):
        assert "actions" in answer.text
        assert "migration" in answer.text
        assert 'type="file"' not in answer.text
    assert page.status_code == 200
    assert sent.status_code == 409
    assert not builder.drafts.exists("etienne", "shop")
    live = server.state.apps.repo("etienne", "shop").data(["products.csv"])["products.csv"]
    assert b"TEA-SEN" in live


def test_an_upload_that_arrives_after_the_first_publish_says_so(server, monkeypatch) -> None:
    builder = server.state.builder
    builder.create_app("etienne", "books")
    real = builder.upload

    def upload_late(info, path, source, size) -> None:
        # The assistant publishes while the file is on its way.
        builder.publish_app("etienne", "books", "First version")
        real(info, path, source, size)

    monkeypatch.setattr(builder, "upload", upload_late)
    with signed_in(server) as client:
        sent = upload(server, client, "data/items.csv", b"name,note\nTea,two\n")

    assert sent.status_code == 409
    assert "Books was published while the file was on its way, so it isn&#39;t in it." in sent.text
    assert 'type="file"' not in sent.text
    assert server.state.apps.repo("etienne", "books").data(["items.csv"]) == {}


def test_an_upload_needs_a_chosen_file(server) -> None:
    builder = server.state.builder
    builder.create_app("etienne", "books")
    builder.drafts.write("etienne", "books", "data/a.csv", b"a,b\n")

    with signed_in(server) as client:
        fields = {"csrf": form_token(server, client), "path": "data/a.csv"}
        # A browser sends a form with no file chosen as an empty part with no name.
        none = client.post(
            "/build/books/upload",
            data=fields,
            files={"file": ("", b"", "application/octet-stream")},
        )
        empty = upload(server, client, "data/a.csv", b"")

    for refused in (none, empty):
        assert refused.status_code == 400
        assert "Choose a file to upload." in refused.text
    assert draft_files(builder.drafts, "etienne", "books")["data/a.csv"] == b"a,b\n"


def test_an_upload_from_another_site_is_refused(server) -> None:
    builder = server.state.builder
    builder.create_app("etienne", "books")

    with signed_in(server) as client:
        token = form_token(server, client)
        foreign = client.post(
            "/build/books/upload",
            data={"csrf": token, "path": "data/a.csv"},
            files={"file": ("a.csv", b"a,b\n")},
            headers={"origin": "https://elsewhere.example"},
        )
        own = client.post(
            "/build/books/upload",
            data={"csrf": token, "path": "data/a.csv"},
            files={"file": ("a.csv", b"a,b\n")},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )

    assert foreign.status_code == 403
    assert own.status_code == 303
    assert draft_files(builder.drafts, "etienne", "books")["data/a.csv"] == b"a,b\n"


# Downloads


def a_commit(folder: Path, parent: str, entries: dict[str, tuple[str, bytes]]) -> str:
    """A commit on top of `parent` that adds `entries` (name: (mode, content)) at the top."""
    tree = git(folder, "ls-tree", parent)
    for name, (mode, content) in entries.items():
        blob = git(folder, "hash-object", "-w", "--stdin", input=content).strip()
        kind = "tree" if mode == "040000" else "blob"
        tree += f"{mode} {kind} {blob}\t{name}\n"
    made = git(folder, "mktree", input=tree.encode()).strip()
    return git(folder, "commit-tree", made, "-p", parent, "-m", "made by the test").strip()


def a_temp_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Where the server makes its temporary files during this test."""
    folder = tmp_path / "temp"
    folder.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(folder))
    return folder


def unzip(content: bytes, into: Path) -> Path:
    """Unzip a download into `into`, and give the app's folder."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        assert all(name.startswith("shop/") for name in names), names
        archive.extractall(into)
    return into / "shop"


def test_the_owner_downloads_the_app(server, monkeypatch, tmp_path) -> None:
    temp = a_temp_folder(monkeypatch, tmp_path)
    repo = server.state.apps.repo("etienne", "shop")
    with signed_in(server) as client:
        overview = client.get("/manage/etienne/shop").text
        response = client.get("/manage/etienne/shop/download")

    assert 'href="/manage/etienne/shop/download"' in overview
    assert "Download the app" in overview
    assert "Its code, its data and their whole history, as a git repository in a zip" in overview
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    disposition = response.headers["content-disposition"]
    # Dated by the server's clock.
    assert disposition == 'attachment; filename="shop-2026-11-11.zip"'
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        kinds = {info.filename: info.compress_type for info in archive.infolist()}
    objects = [name for name in kinds if name.startswith("shop/.git/objects/")]
    assert objects and all(kinds[name] == zipfile.ZIP_STORED for name in objects if name[-1] != "/")
    assert kinds["shop/app.py"] == zipfile.ZIP_DEFLATED
    folder = unzip(response.content, tmp_path / "out")
    # The index holds no times, inode, owner or size of the server's files.
    stats = [line for line in git(folder, "ls-files", "--debug").splitlines() if ":" in line]
    numbers = [n for line in stats for n in re.findall(r"\d+", line.split("flags")[0])]
    assert stats and set(numbers) == {"0"}
    names = repo.data_names()
    assert names
    for name, content in repo.data(names).items():
        assert (folder / "data" / name).read_bytes() == content
    assert (folder / "app.py").read_bytes() == repo.code()["app.py"]
    assert (folder / "instructions.md").is_file()
    assert git(folder, "log", "--format=%H") == git(repo.folder, "log", "--format=%H")
    assert git(folder, "status", "--porcelain") == ""
    assert list(temp.iterdir()) == []


def test_a_download_leaves_the_server_refs_out(server, tmp_path) -> None:
    repo = server.state.apps.repo("etienne", "shop")
    head = repo.head()
    assert head is not None
    hidden = {
        "refs/app/transactions/t-1": b"unkept work of another caller",
        "refs/notes/commits": b"a note the server keeps",
        "refs/tags/v1": b"a tagged secret",
        "refs/heads/other": b"another branch's file",
    }
    for ref, content in hidden.items():
        git(repo.folder, "update-ref", ref, a_commit(repo.folder, head, {"x": ("100644", content)}))
    (repo.folder / ".git" / "app").mkdir(exist_ok=True)
    (repo.folder / ".git" / "app" / "t-1.json").write_text('{"arguments": "secret"}')
    git(repo.folder, "remote", "add", "upstream", "https://example.com/shop.git")

    with signed_in(server) as client:
        response = client.get("/manage/etienne/shop/download")

    folder = unzip(response.content, tmp_path / "out")
    branch = git(repo.folder, "symbolic-ref", "HEAD").strip()
    assert git(folder, "for-each-ref", "--format=%(refname)").split() == [branch]
    assert git(folder, "remote") == ""
    assert not (folder / ".git" / "app").exists()
    for content in hidden.values():
        blob = git(repo.folder, "hash-object", "--stdin", input=content).strip()
        found = subprocess.run(["git", "cat-file", "-e", blob], cwd=folder, capture_output=True)
        assert found.returncode != 0, content
    where = str(repo.folder).encode()
    for path in (folder / ".git").rglob("*"):
        if path.is_file():
            assert where not in path.read_bytes(), path


def test_a_download_needs_the_owner(server) -> None:
    member = a_shop_member(server)
    database = server.state.apps.database
    stranger = database.person_for(ISSUER, "auth0|kim", "kim@example.com", "kim", NOW)

    with TestClient(server) as client:
        anonymous = client.get("/manage/etienne/shop/download", follow_redirects=False)
        refused = []
        for person in (member, stranger):
            act_as(server, client, person)
            refused.append(client.get("/manage/etienne/shop/download"))
    with signed_in(server) as client:
        missing = client.get("/manage/etienne/nothing/download")

    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == "/login?next=%2Fmanage%2Fetienne%2Fshop%2Fdownload"
    for page in refused:
        assert page.status_code == 404
        assert "There is no app etienne/shop here." in page.text
        assert page.headers["content-type"].startswith("text/html")
    assert missing.status_code == 404


def test_a_download_works_without_the_sandbox(server, monkeypatch, tmp_path) -> None:
    apps = server.state.apps

    def no_sandbox(owner: str):
        raise SandboxError("the sandbox doesn't start")

    with signed_in(server) as client:
        apps.forget("etienne", "shop")
        monkeypatch.setattr(apps.executor, "sandbox", no_sandbox)
        overview = client.get("/manage/etienne/shop")
        response = client.get("/manage/etienne/shop/download")

    assert overview.status_code == 503
    # Without Retry-After: a page that waited for the start stops asking, and shows this.
    assert "retry-after" not in overview.headers
    assert response.status_code == 200
    folder = unzip(response.content, tmp_path / "out")
    assert (folder / "app.py").is_file()


def test_a_download_imports_back(server, tmp_path) -> None:
    apps = server.state.apps
    with signed_in(server) as client:
        response = client.get("/manage/etienne/shop/download")
    folder = unzip(response.content, tmp_path / "out")
    downloaded = {name: (folder / name).read_bytes() for name in ("app.py", "app.py.lock")}

    # The real command: it builds the app's environment, with this SDK, and changes no file.
    env = {key: value for key, value in os.environ.items() if key != "CLIQUET_IN_APP_ENV"}
    checked = subprocess.run(
        [sys.executable, "-m", "cliquet.cli", "check", str(folder)],
        capture_output=True,
        text=True,
        env={**env, **GIT_ENV},
        stdin=subprocess.DEVNULL,
    )
    assert checked.returncode == 0, checked.stderr
    assert "pass every check" in checked.stdout
    assert {name: (folder / name).read_bytes() for name in downloaded} == downloaded
    assert git(folder, "status", "--porcelain") == ""

    apps.import_folder(folder, "etienne", "shop-again")
    old, new = apps.repo("etienne", "shop"), apps.repo("etienne", "shop-again")
    assert new.data_names() == old.data_names()
    assert new.data(new.data_names()) == old.data(old.data_names())
    assert new.code() == old.code()
    assert len(git(new.folder, "log", "--format=%H").split()) == 1


def test_a_download_holds_a_symbolic_link_as_a_file(server, tmp_path) -> None:
    secret = tmp_path / "server-secret.txt"
    secret.write_bytes(b"a file of the server outside the app")
    repo = server.state.apps.repo("etienne", "shop")
    head = repo.head()
    assert head is not None
    link = ("120000", str(secret).encode())
    git(repo.folder, "update-ref", "HEAD", a_commit(repo.folder, head, {"outside": link}))

    with signed_in(server) as client:
        response = client.get("/manage/etienne/shop/download")

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        contents = [archive.read(name) for name in names if not name.endswith("/")]
        # The link is a plain file that holds its target, as git writes it without links.
        assert archive.read("shop/outside") == str(secret).encode()
    assert "shop/app.py" in names
    assert all(b"a file of the server outside the app" not in content for content in contents)
    folder = unzip(response.content, tmp_path / "out")
    assert not (folder / "outside").is_symlink()
    assert git(folder, "status", "--porcelain") == ""


def test_the_zip_follows_no_link_on_disk(tmp_path) -> None:
    secret = tmp_path / "server-secret.txt"
    secret.write_bytes(b"a file of the server outside the app")
    folder = tmp_path / "shop"
    (folder / "data").mkdir(parents=True)
    (folder / "data" / "stock.csv").write_text("sku\n")
    (folder / "outside").symlink_to(secret)
    (folder / "elsewhere").symlink_to(tmp_path, target_is_directory=True)

    repos._zip(folder, tmp_path / "shop.zip", "shop")

    with zipfile.ZipFile(tmp_path / "shop.zip") as archive:
        assert archive.namelist() == ["shop/", "shop/data/", "shop/data/stock.csv"]


def test_a_published_app_without_a_commit_is_missing(server) -> None:
    apps = server.state.apps
    apps.database.create_app("etienne", "empty", NOW)
    apps.database.mark_published("etienne", "empty", NOW, "Nothing yet")
    folder = apps.repo("etienne", "empty").folder
    folder.mkdir(parents=True)
    git(folder, "init", "--quiet")

    with signed_in(server) as client:
        response = client.get("/manage/etienne/empty/download")

    assert response.status_code == 404
    assert "There is no app etienne/empty here." in response.text


@pytest.mark.parametrize("step", ["_copy", "_zip"])
def test_a_download_removes_its_temporary_files(
    server, monkeypatch, tmp_path, step, caplog
) -> None:
    temp = a_temp_folder(monkeypatch, tmp_path)
    real = getattr(repos, step)

    def full_disk(*args: Path) -> None:
        real(*args)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(repos, step, full_disk)
    with signed_in(server) as client:
        response = client.get("/manage/etienne/shop/download")

    # A page that crashes is a failure of Cliquet's own: logged once, and answered 500.
    assert response.status_code == 500
    record = one_unexpected(caplog, OSError)
    assert record.getMessage() == "unexpected: GET /manage/etienne/shop/download failed"
    assert list(temp.iterdir()) == []


def test_a_download_is_closed_when_the_client_leaves(tmp_path) -> None:
    from cliquet_server.web.download import zip_response

    path = tmp_path / "shop.zip"
    path.write_bytes(b"zip" * 1_000_000)
    zipped = path.open("rb")

    async def receive() -> dict:
        return {"type": "http.request"}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.body":
            raise OSError("the client went away")

    scope = {"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.4"}}

    async def run() -> None:
        await zip_response(zipped, "shop.zip")(scope, receive, send)

    with pytest.raises(ClientDisconnect):
        anyio.run(run)
    gc.collect()
    assert zipped.closed


def test_a_head_request_makes_no_download(server, monkeypatch, tmp_path) -> None:
    temp = a_temp_folder(monkeypatch, tmp_path)

    def no_export(*args: object) -> None:
        raise AssertionError("a HEAD request cloned the repository")

    monkeypatch.setattr(repos.AppRepo, "export", no_export)
    with signed_in(server) as client:
        response = client.head("/manage/etienne/shop/download")
    with TestClient(server) as client:
        anonymous = client.head("/manage/etienne/shop/download", follow_redirects=False)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == 'attachment; filename="shop-2026-11-11.zip"'
    assert response.content == b""
    assert anonymous.status_code == 303
    assert list(temp.iterdir()) == []


def test_a_head_request_starts_no_app_and_no_preview(server, monkeypatch) -> None:
    def no_start(*args: object, **options: object) -> None:
        raise AssertionError("a HEAD request started the app")

    monkeypatch.setattr(server.state.apps, "live", no_start)
    with signed_in(server) as client:
        for path in (
            "/apps/etienne/shop",
            "/apps/etienne/shop/owner/screen",
            "/build/shop/preview",
            "/build/shop/preview/owner/screen",
            "/manage/etienne/shop",
            "/manage/etienne/shop/history",
            "/manage/etienne/shop/sharing",
            "/",
        ):
            response = client.head(path, follow_redirects=False)
            assert (response.status_code, response.headers["allow"]) == (405, "GET"), path


def test_an_export_that_fails_leaves_nothing(server, monkeypatch, tmp_path) -> None:
    repo = server.state.apps.repo("etienne", "shop")
    real = repos._zip

    def full_disk(folder: Path, target: Path, name: str) -> None:
        real(folder, target, name)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(repos, "_zip", full_disk)
    into = tmp_path / "into"
    into.mkdir()
    with pytest.raises(OSError):
        repo.export(into, "shop")

    assert list(into.iterdir()) == []


def test_a_detached_head_is_downloaded_as_main(server, tmp_path) -> None:
    repo = server.state.apps.repo("etienne", "shop")
    head = repo.head()
    assert head is not None
    detached = a_commit(repo.folder, head, {"note.txt": ("100644", b"on a detached head")})
    git(repo.folder, "update-ref", "--no-deref", "HEAD", detached)

    with signed_in(server) as client:
        response = client.get("/manage/etienne/shop/download")

    folder = unzip(response.content, tmp_path / "out")
    assert git(folder, "for-each-ref", "--format=%(refname)").split() == ["refs/heads/main"]
    assert git(folder, "symbolic-ref", "HEAD").strip() == "refs/heads/main"
    assert git(folder, "rev-parse", "HEAD").strip() == detached
    assert (folder / "note.txt").read_bytes() == b"on a detached head"


def test_a_download_runs_no_hook_or_filter_of_the_server(server, monkeypatch, tmp_path) -> None:
    ran = tmp_path / "ran"
    spy = tmp_path / "spy.sh"
    spy.write_text(f"#!/bin/sh\necho ran >> {ran}\ncat\n")
    spy.chmod(0o755)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for hook in ("post-checkout", "reference-transaction", "post-index-change"):
        (hooks / hook).write_text(f"#!/bin/sh\necho ran >> {ran}\n")
        (hooks / hook).chmod(0o755)
    attributes = tmp_path / "attributes"
    attributes.write_text("* filter=spy\n")
    config = tmp_path / "gitconfig"
    config.write_text(
        f"[core]\n\thooksPath = {hooks}\n\tattributesFile = {attributes}\n"
        f'[filter "spy"]\n\tsmudge = {spy}\n\tclean = {spy}\n\trequired = true\n'
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))

    with signed_in(server) as client:
        response = client.get("/manage/etienne/shop/download")

    assert response.status_code == 200
    assert not ran.exists()


def test_a_head_request_for_an_app_without_a_commit_is_missing(server) -> None:
    apps = server.state.apps
    apps.database.create_app("etienne", "empty", NOW)
    apps.database.mark_published("etienne", "empty", NOW, "Nothing yet")
    folder = apps.repo("etienne", "empty").folder
    folder.mkdir(parents=True)
    git(folder, "init", "--quiet")

    with signed_in(server) as client:
        head = client.head("/manage/etienne/empty/download")
        get = client.get("/manage/etienne/empty/download")

    assert head.status_code == get.status_code == 404
    assert head.headers["content-type"] == get.headers["content-type"]


def test_a_download_is_closed_when_the_client_leaves_at_once(tmp_path) -> None:
    from cliquet_server.web.download import zip_response

    path = tmp_path / "shop.zip"
    path.write_bytes(b"zip" * 1000)
    zipped = path.open("rb")

    async def receive() -> dict:
        return {"type": "http.request"}

    async def send(message: MutableMapping[str, Any]) -> None:
        raise OSError("the client went away")

    scope = {"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.4"}}

    async def run() -> None:
        await zip_response(zipped, "shop.zip")(scope, receive, send)

    with pytest.raises((OSError, ClientDisconnect)):
        anyio.run(run)
    # Closed by the response itself, not by the garbage collector.
    assert zipped.closed


def test_a_cancelled_export_closes_what_it_opened(server, monkeypatch, tmp_path) -> None:
    import threading

    from cliquet_server.web import download as web

    repo = server.state.apps.repo("etienne", "shop")
    started, go = threading.Event(), threading.Event()
    seen: list[list] = []
    real = web._exported

    def slow(repo, name, opened):
        seen.append(opened)
        file = real(repo, name, opened)
        started.set()
        go.wait(10)
        return file

    monkeypatch.setattr(web, "_exported", slow)

    async def run() -> None:
        async with anyio.create_task_group() as group:

            async def wait() -> None:
                await web._open_export(repo, "shop", anyio.CapacityLimiter(1))

            group.start_soon(wait)
            await anyio.to_thread.run_sync(started.wait, 10)
            group.cancel_scope.cancel()
            go.set()

    anyio.run(run)
    assert seen and len(seen[0]) == 1
    assert seen[0][0].closed


def test_the_console_lists_drafts_and_leads_to_their_preview(server) -> None:
    builder = server.state.builder
    builder.create_app("etienne", "notes")
    with signed_in(server) as client:
        home = client.get("/").text
        preview = client.get("/build/notes/preview").text
        before = client.get("/manage/etienne/shop").text
        builder.start_preview("etienne", "shop")
        session = builder.previews.find("etienne", "shop")
        assert session is not None
        used = session.used
        during = client.get("/manage/etienne/shop").text
        untouched = session.used == used
        shop_preview = client.get("/build/shop/preview/owner").text

    # A draft never published is listed apart, with its preview and its upload.
    drafts = home.split('id="drafts"')[1].split("</section>")[0]
    assert 'href="/build/notes/preview"' in drafts and 'href="/build/notes/upload"' in drafts
    assert 'href="/build/shop/preview"' not in drafts
    assert 'id="nav-drafts"' in home
    # Its preview's page leads back to the console: home, as it has no page of its own yet.
    assert 'href="/">Back to the console<' in preview
    assert 'href="/manage/etienne/shop">Back to the console<' in shop_preview
    # A published app links to its draft's preview while one is open, and the console's
    # pages don't count as using it.
    assert "Preview the draft" not in before
    assert 'href="/build/shop/preview">Preview the draft<' in during
    assert untouched


def test_without_the_builder_the_console_lists_no_draft(server) -> None:
    database = server.state.apps.database
    database.create_app("etienne", "notes", NOW)
    person = database.person_by_login("etienne")
    nav = common._nav(database, person, None)
    assert nav["drafts"] == []
    assert all("preview" not in app for app in nav["own"])
