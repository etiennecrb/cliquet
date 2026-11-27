import math
import re
from html import unescape
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE
from starlette.responses import Response
from starlette.routing import Mount
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from cliquet.app import RESERVED_ROLES
from cliquet.limits import MAX_BODY
from cliquet_server.app import BodyLimit, create_app
from cliquet_server.db import ERRORS_KEPT_FOR
from cliquet_server.executor import BuildProblems, LocalExecutor
from cliquet_server.settings import Settings
from cliquet_server.web import TOO_LARGE, UPLOAD_PATH
from cliquet_server.web.common import content_length

from .server_helpers import (
    APPS,
    ERROR_ID,
    NOW,
    STAFF,
    a_shop_member,
    act_as,
    app_token,
    publish_failing,
    published_with_icon,
    rpc_refusal,
    signed_in,
    template_code,
)


def publish_list(server, folder: Path, docstring: str = "") -> None:
    code = template_code(folder)
    if docstring:
        code["app.py"] = code["app.py"].replace(
            b'"""List: a list of items, kept by an agent."""', f'"""{docstring}"""'.encode()
        )
    server.state.apps.database.create_app("etienne", "list", NOW)
    server.state.apps.publish("etienne", "list", code, note="test", data={})


def test_the_home_page_lists_the_apps(server) -> None:
    with signed_in(server) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Shop" in response.text
    assert "Stock for a small tea shop" in response.text
    assert 'href="/manage/etienne/shop"' in response.text


def test_the_home_page_without_apps(settings: Settings, executor: LocalExecutor) -> None:
    with signed_in(create_app(settings, executor=executor)) as client:
        text = client.get("/").text
    # A guide to the first app, in place of empty groups; self-hosting has its own docs.
    assert "Build your first app" in text and "Check the preview, then publish" in text
    assert 'id="own-apps"' not in text and 'id="shared-apps"' not in text
    assert "cliquet-server import" not in text


def test_an_app_s_page_says_how_to_connect(server) -> None:
    with signed_in(server) as client:
        text = client.get("/manage/etienne/shop").text
    # The owner's assistant reaches it at their own endpoint; an agent on its own, at the
    # app's, with a token made for it.
    assert "http://127.0.0.1:8800/me/mcp" in text
    assert "http://127.0.0.1:8800/apps/etienne/shop/mcp" in text
    assert 'href="/tokens?app=etienne/shop"' in text
    assert 'href="/apps/etienne/shop"' in text


def test_an_app_s_page_lists_tools_and_rules(server) -> None:
    with signed_in(server) as client:
        text = client.get("/manage/etienne/shop/tools").text
    assert "record_sale" in text
    assert "set_price" in text
    assert "shelves" in text
    assert "Stock never goes below zero" in text


def test_an_app_s_page_shows_the_latest_changes(server, tmp_path: Path) -> None:
    # A publish that brought no data made no change to it.
    publish_list(server, tmp_path / "list")
    with signed_in(server) as client:
        assert "No change yet." in client.get("/manage/etienne/list/history").text
        live = server.state.apps.live("etienne", "shop")
        live.runtime.call(
            STAFF, "record_sale", {"sku": "TEA-SEN", "quantity": 2, "receipt": "R-0300"}
        )
        text = client.get("/manage/etienne/shop/history").text
        overview = client.get("/manage/etienne/shop").text
    assert "record_sale" in text
    assert "till" in text
    recent = overview.split('id="recent"')[1].split("</ul>")[0]
    assert "<code>record_sale</code>" in recent and "till, " in recent


def test_text_from_an_app_is_escaped(server, tmp_path: Path) -> None:
    publish_list(server, tmp_path / "list", "<b>Bold</b> list.")
    with signed_in(server) as client:
        home = client.get("/").text
        page = client.get("/manage/etienne/list").text
    assert "&lt;b&gt;" in home
    assert "<b>Bold" not in home
    assert "&lt;b&gt;" in page
    assert "<b>Bold" not in page


def test_an_app_without_roles_has_one_address(server, tmp_path: Path) -> None:
    publish_list(server, tmp_path / "list")
    with signed_in(server) as client:
        text = client.get("/manage/etienne/list").text
    assert "/apps/etienne/list/mcp" in text
    assert 'href="/apps/etienne/list"' in text
    assert "Everyone" in text


def test_an_unknown_app_is_a_page_not_an_error(server) -> None:
    with signed_in(server) as client:
        missing = client.get("/apps/etienne/nope")
        nowhere = client.get("/nowhere")
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("text/html")
    assert "There is no app etienne/nope here." in missing.text
    assert nowhere.status_code == 404
    assert "There is no page at this address." in nowhere.text


def test_the_style_sheet_is_served(server) -> None:
    with TestClient(server) as client:
        response = client.get("/static/cliquet.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")


def _theme(css: str, selector: str) -> list[dict[str, str]]:
    """The custom properties of each block that `selector` opens, light first, then dark."""
    blocks = re.findall(re.escape(selector) + r" \{([^}]*)\}", css)
    return [dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block)) for block in blocks]


def test_the_console_wears_the_website_theme() -> None:
    root = Path(__file__).parents[3]
    website = (root / "website/public/styles.css").read_text()
    console = (root / "packages/server/src/cliquet_server/static/cliquet.css").read_text()
    site = [
        {name: value for name, value in block.items() if name not in {"--glow", "--shell"}}
        for block in _theme(website, ":root")[:2]
    ]
    assert len(site) == 2 and len(site[0]) > 20
    assert _theme(console, "body:not(.app-page)") == site


def test_the_console_says_what_cliquet_is_as_the_website_does(server) -> None:
    home = (Path(__file__).parents[3] / "website/public/index.html").read_text()
    with signed_in(server) as client:
        console, app = client.get("/").text, client.get("/apps/etienne/shop/staff").text

    meta = re.compile(r'<meta name="(?:description|theme-color)"[^>]*>')
    assert meta.findall(console) == [one.strip() for one in meta.findall(home)]
    assert len(meta.findall(console)) == 3
    # An app's own page is the app's, with its own colour.
    assert 'name="description"' not in app and "#f7f4ee" not in app


def test_the_hosted_privacy_notice_says_how_long_errors_are_kept() -> None:
    """The website hosts the notice the hosted server links to: it must not drift from the code."""
    notice = (Path(__file__).parents[3] / "website/public/privacy.html").read_text()
    assert '<meta name="robots" content="noindex">' in notice
    assert re.search(r"Last updated: \d{1,2} [A-Z][a-z]+ \d{4}", notice)
    items = [item for item in re.findall(r"<li>(.*?)</li>", notice, re.S) if "errors" in item]
    days = [int(found) for item in items for found in re.findall(r"(\d+) days", item)]
    assert len(days) == 2 and set(days) == {ERRORS_KEPT_FOR.days}


def _srgb(value: str) -> tuple[float, float, float]:
    """An `oklch(L% C H)` colour as linear sRGB, clipped to the screen's gamut."""
    found = re.fullmatch(r"oklch\(([\d.]+)% ([\d.]+) ([\d.]+)\)", value.strip())
    assert found, value
    lightness, chroma, hue = float(found[1]) / 100, float(found[2]), float(found[3])
    a, b = chroma * math.cos(math.radians(hue)), chroma * math.sin(math.radians(hue))
    l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    rgb = (
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    )
    red, green, blue = (min(max(one, 0.0), 1.0) for one in rgb)
    return red, green, blue


def _contrast(one: str, other: str) -> float:
    """WCAG's contrast ratio of two colours."""
    lighter, darker = sorted(
        (0.2126 * r + 0.7152 * g + 0.0722 * b + 0.05 for r, g, b in map(_srgb, (one, other))),
        reverse=True,
    )
    return lighter / darker


def test_the_console_s_secondary_text_reads_on_every_surface() -> None:
    root = Path(__file__).parents[3]
    css = (root / "packages/server/src/cliquet_server/static/cliquet.css").read_text()
    light, dark = _theme(css, "body:not(.app-page)")
    surfaces = ("--color-base-100", "--color-base-200", "--color-base-300", "--surface")
    for theme in (light, {**light, **dark}):
        for surface in surfaces:
            assert _contrast(theme["--muted"], theme[surface]) >= 4.5, surface
    # daisyUI's faded text takes it.
    assert ".tabs > .tab:not([aria-current]), .menu-title, .text-base-content\\/60" in css


def test_the_wordmark_and_icon_are_the_websites(server) -> None:
    with TestClient(server) as client:
        page = client.get("/nowhere").text
    assert "<title>Not found · cliquet.dev</title>" in page
    assert '<span class="tld">.dev</span>' in page
    # The mark's links are the website's, at the root, as a crawler and a client look for them.
    assert '<link rel="icon" href="/favicon.ico" sizes="48x48">' in page
    assert '<link rel="icon" href="/favicon.svg" type="image/svg+xml">' in page
    assert '<link rel="apple-touch-icon" href="/apple-touch-icon.png">' in page


def test_an_app_s_page_wears_the_icon_its_endpoint_gives(server) -> None:
    # The stock example names an accent but no icon: its page keeps the mark's links.
    with signed_in(server) as client:
        page = client.get("/apps/etienne/shop/staff").text
        assert '<link rel="icon" href="/favicon.svg" type="image/svg+xml">' in page
        # An app that names an icon and an accent links the icon its endpoint gives a client,
        # and keeps the mark's own raster for a browser that wants one. A phone's home screen
        # takes the app's own, drawn as a PNG.
        published_with_icon(server)
        page = client.get("/apps/etienne/shop/staff").text
        assert client.get("/apps/etienne/shop/icon.svg").status_code == 200
    assert '<link rel="icon" href="/apps/etienne/shop/icon.svg" type="image/svg+xml">' in page
    assert '<link rel="apple-touch-icon" href="/apps/etienne/shop/apple-touch-icon.png">' in page
    assert '<link rel="icon" href="/favicon.ico" sizes="48x48">' in page
    # The mark's own SVG would compete with the app's: it is the only link left out.
    assert "/favicon.svg" not in page


# Each of the mark's files, and the type the server serves it as.
MARK = {
    "/favicon.ico": "image/x-icon",
    "/favicon.svg": "image/svg+xml",
    "/apple-touch-icon.png": "image/png",
}


def test_the_marks_files_are_the_websites(server) -> None:
    root = Path(__file__).parents[3] / "website" / "public"
    with TestClient(server) as client:
        for path, media_type in MARK.items():
            answer = client.get(path)
            assert answer.status_code == 200, path
            assert answer.headers["content-type"] == media_type, path
            # Written twice, and a test keeps them equal: the site's copy is the truth.
            assert answer.content == (root / path.lstrip("/")).read_bytes(), path
            # A browser asks for the icon with a HEAD first, and reads only its headers.
            head = client.head(path)
            assert head.status_code == 200, path
            assert head.headers["content-type"] == media_type, path
            assert head.content == b"", path
            # The static folder serves the same files, as the pages' other assets are.
            mounted = client.get(f"/static{path}")
            assert mounted.content == answer.content, path


def test_addresses_use_the_base_url(tmp_path: Path, executor: LocalExecutor) -> None:
    settings = Settings.from_env(
        {
            "CLIQUET_DATA_DIR": str(tmp_path / "data"),
            "CLIQUET_BASE_URL": "https://cliquet.example",
            "CLIQUET_ALLOW_REMOTE_LOCAL_EXECUTOR": "1",
            "CLIQUET_DEV_LOGIN": "etienne",
            "CLIQUET_ALLOW_REMOTE_DEV_LOGIN": "1",
        }
    )
    app = create_app(settings, executor=executor, clock=lambda: NOW)
    app.state.apps.import_folder(APPS / "stock", "etienne", "shop")
    with signed_in(app, base_url="https://testserver") as client:
        text = client.get("/manage/etienne/shop", headers={"Host": "cliquet-internal.example"}).text
    assert "https://cliquet.example/apps/etienne/shop/mcp" in text
    assert "https://cliquet.example/me/mcp" in text
    assert "cliquet-internal.example" not in text


def test_every_page_has_the_headers(server) -> None:
    with TestClient(server) as client:
        # Signed out, a search engine meets the redirect to sign-in, and a missing page.
        signed_out = [client.get("/", follow_redirects=False), client.get("/nowhere")]
    for page in signed_out:
        assert page.headers["x-robots-tag"] == "noindex"
    with signed_in(server) as client:
        pages = [
            client.get("/"),
            client.get("/assistants"),
            client.get("/manage/etienne/shop"),
            client.get("/apps/etienne/shop/staff"),
            client.get("/apps/etienne/shop/staff/screen?view=till&embed"),
        ]
    for page in pages:
        assert page.status_code == 200, page.url
        assert page.headers["x-content-type-options"] == "nosniff"
        assert page.headers["content-security-policy"] == "frame-ancestors 'self'"
        assert page.headers["x-frame-options"] == "SAMEORIGIN"
        assert page.headers["referrer-policy"] == "same-origin"
        assert page.headers["x-robots-tag"] == "noindex"


def test_pages_and_files_travel_gzipped(server) -> None:
    gzip_ok = {"accept-encoding": "gzip"}
    with signed_in(server) as client:
        page = client.get("/", headers=gzip_ok)
        style = client.get("/static/cliquet.css", headers=gzip_ok)
        plain = client.get("/", headers={"accept-encoding": "identity"})
    assert page.headers["content-encoding"] == "gzip"
    assert "Shop" in page.text  # httpx undoes the gzip, as a browser does.
    assert style.headers["content-encoding"] == "gzip"
    assert "content-encoding" not in plain.headers
    assert "accept-encoding" in plain.headers["vary"].lower()


def test_a_body_over_the_limit_is_refused_before_it_is_read(server) -> None:
    rpc = "/apps/etienne/shop/staff/rpc"
    json_type = {"content-type": "application/json"}
    with signed_in(server) as client:
        declared = client.post(rpc, content=b" " * (MAX_BODY + 1), headers=json_type)
        # Sent from an iterator, the body goes in chunks, with no length.
        chunked = client.post(rpc, content=iter([b" " * MAX_BODY, b" "]), headers=json_type)
        # The form is read before anything says who sent it.
        form_type = {"content-type": "application/x-www-form-urlencoded"}
        form = client.post("/logout", content=b"csrf=" + b"x" * MAX_BODY, headers=form_type)
        within = client.post(
            rpc, content=iter([b'{"method": ', b'"tools/list"}']), headers=json_type
        )
        # The upload keeps its own, larger limit: the shop is published, so it takes nothing.
        upload = client.post("/build/shop/upload", content=b"x" * (MAX_BODY + 1))
        # An app's address refuses with a JSON-RPC error, which an MCP client shows.
        endpoint = client.post("/apps/etienne/shop/mcp", content=b" " * (MAX_BODY + 1))
    too_large = f"a request's body is at most {MAX_BODY:,} bytes"
    for refused in (declared, chunked, form):
        assert refused.status_code == 413
        assert refused.json() == {"error": too_large}
        assert refused.headers["x-content-type-options"] == "nosniff"
    assert rpc_refusal(endpoint, 413) == too_large
    assert within.status_code == 200 and "tools" in within.json()
    assert upload.status_code == 409


def test_the_body_limit_is_mcp_s_own() -> None:
    # So an MCP endpoint refuses no message that MCP would take.
    assert MAX_BODY == DEFAULT_MAX_REQUEST_BODY_SIZE


def sent_in_chunks(
    chunks: list[bytes],
    path: str = "/logout",
    length: str | None = None,
    limits: dict[str, int] | None = None,
) -> tuple[int, list[bytes]]:
    """POST `chunks` through `BodyLimit`, one message each and with no length unless `length`
    says one, to an app that answers 200: the status, and the chunks the app read."""
    messages: list[Message] = [
        {"type": "http.request", "body": chunk, "more_body": n < len(chunks) - 1}
        for n, chunk in enumerate(chunks)
    ]
    read: list[bytes] = []
    statuses: list[int] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            read.append(message["body"])
            if not message["more_body"]:
                break
        await Response()(scope, receive, send)

    async def receive() -> Message:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    headers = [] if length is None else [(b"content-length", length.encode())]
    scope: dict[str, Any] = {"type": "http", "method": "POST", "path": path, "headers": headers}
    anyio.run(BodyLimit(app, limits), scope, receive, send)
    return statuses[0], read


def test_a_body_in_chunks_is_handed_on_whole_or_refused() -> None:
    half = MAX_BODY // 2
    assert sent_in_chunks([b"a" * half, b"b" * half, b""]) == (200, [b"a" * half, b"b" * half, b""])
    assert sent_in_chunks([b"a" * half, b"b" * half, b"c"]) == (413, [])


def test_a_route_s_own_limit_is_for_a_body_that_states_its_length() -> None:
    upload = "/build/books/upload"
    limits = {"/build/{app}/upload": 3 * MAX_BODY}
    over = str(MAX_BODY + 1)
    # The route takes a stated length up to its own limit; any other route keeps MAX_BODY.
    assert sent_in_chunks([b"x"], upload, over, limits) == (200, [b"x"])
    assert sent_in_chunks([b"x"], "/logout", over, limits) == (413, [])
    assert sent_in_chunks([b"x"], upload, str(3 * MAX_BODY + 1), limits) == (413, [])
    for bad in ("-1", "1e3", "1" * 21, ""):
        assert sent_in_chunks([b"x"], upload, bad, limits) == (413, []), bad
    # A body with no length is held in memory to be counted: MAX_BODY on every route.
    half = MAX_BODY // 2
    assert sent_in_chunks([b"a" * half, b"b" * half, b"c"], upload, None, limits) == (413, [])


def test_a_route_that_refuses_itself_gets_a_body_over_its_limit_unread() -> None:
    limit = 3 * MAX_BODY
    seen: list[tuple[Any, bytes]] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        message = await receive()
        seen.append((scope.get(TOO_LARGE), message["body"]))
        # Then the client is gone, as a real receive says once the body is read.
        assert (await receive())["type"] == "http.disconnect"
        await Response(status_code=413)(scope, receive, send)

    async def receive() -> Message:
        raise AssertionError("the body is read")

    async def send(message: Message) -> None:
        pass

    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/build/books/upload",
        "headers": [(b"content-length", str(limit + 1).encode())],
    }
    middleware = BodyLimit(app, {UPLOAD_PATH: limit}, own_refusals={UPLOAD_PATH})
    anyio.run(middleware, scope, receive, send)
    assert seen == [(True, b"")]


def route_paths(routes: list[Any], prefix: str = "") -> list[str]:
    """Every route's path, those under a mount included."""
    paths = []
    for route in routes:
        path = prefix + getattr(route, "path", "")
        paths.append(path)
        if isinstance(route, Mount):
            paths += route_paths(route.routes, path)
    return paths


def test_the_reserved_role_names_are_the_words_under_an_app_s_address(server) -> None:
    # A role's page is /apps/<login>/<app>/<role>, so no role may take a word the server uses
    # there. An app's own icon, `icon.svg`, is one of those words, but no role could hold it,
    # since a role's name has no dot: only the words a role could take are compared.
    under_an_app = re.compile(r"/apps/\{login\}/\{app\}/([^/{]+)(?:/|$)")
    a_role = re.compile(r"[a-z][a-z0-9_-]*")
    words = {
        found.group(1)
        for path in route_paths(server.routes)
        if (found := under_an_app.match(path)) is not None
        and a_role.fullmatch(found.group(1)) is not None
    }
    assert words == RESERVED_ROLES


def test_content_length() -> None:
    assert content_length({"content-length": "12"}) == 12
    assert content_length({}) is None
    for bad in ("12 ", "0x1", "-3", "1" * 21):
        with pytest.raises(ValueError):
            content_length({"content-length": bad})


def test_a_screen_error_is_hidden_from_members(server, monkeypatch) -> None:
    person = a_shop_member(server)
    publish_failing(server)
    call = {"method": "tools/call", "params": {"name": "broken", "arguments": {}}}

    with TestClient(server) as client:
        act_as(server, client, person)
        page = client.get("/apps/etienne/shop/owner?view=broken")
        member = client.post("/apps/etienne/shop/owner/rpc", json=call).json()
    with signed_in(server) as client:
        owner = client.post("/apps/etienne/shop/staff/rpc", json=call).json()

    assert page.status_code == 200 and "Sencha" not in page.text
    assert member["isError"]
    (shown,) = member["content"]
    assert "Sencha" not in shown["text"]
    assert re.fullmatch(
        rf"The app failed with a KeyError\. Nothing was changed\. Error {ERROR_ID} has the "
        r"details: the app's owner can read it in the app's console\.",
        shown["text"],
    )
    assert owner["isError"] and "KeyError: 'Sencha costs 4.20'" in owner["content"][0]["text"]

    # When the app's code can't even be loaded, its text is the owner's only too.
    apps = server.state.apps
    apps.forget("etienne", "shop")

    def fail(login: str, name: str, **_: object):
        raise BuildProblems(["app.py: KeyError: 'Sencha costs 4.20'"])

    # Every start fails, as one of a version whose code can't be loaded.
    monkeypatch.setattr(apps, "_start", fail)
    with TestClient(server) as client:
        act_as(server, client, person)
        pages = [
            client.get("/apps/etienne/shop/owner"),
            client.get("/apps/etienne/shop", follow_redirects=False),
            client.post("/apps/etienne/shop/owner/rpc", json=call),
        ]
    # The app's MCP endpoint, for the member's token.
    token = app_token(server, "owner", person=person)
    with TestClient(server) as client:
        pages.append(
            client.post(
                "/apps/etienne/shop/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {token}",
                },
            )
        )
    with signed_in(server) as client:
        own = client.get("/apps/etienne/shop/staff")
    # The same failure is kept once for the owner, under one id.
    ids = {"".join(re.findall(ERROR_ID, unescape(response.text))) for response in pages}
    assert len(ids) == 1
    for response in pages:
        assert response.status_code == 503
        assert "Sencha" not in response.text
        assert re.search(
            rf"Error {ERROR_ID} has the details: the app.s owner can read it in the app.s "
            "console",
            unescape(response.text),
        )
    # After the failed calls above.
    latest = apps.database.errors("etienne", "shop", NOW)[0]
    assert latest.start and {latest.id} == ids
    assert own.status_code == 503 and "Sencha costs 4.20" in own.text
