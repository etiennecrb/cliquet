import re
import subprocess
from pathlib import Path

from starlette.testclient import TestClient

from cliquet import views
from cliquet.host import host_page
from cliquet.mcp_server import AppServer
from cliquet_server.db import PersonInfo

from .server_helpers import (
    ERROR_ID,
    NOW,
    a_shop_member,
    act_as,
    example_code,
    member,
    signed_in,
    template_code,
)


def git(folder: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=folder, capture_output=True, text=True, check=True
    ).stdout


def test_a_role_s_page_has_a_tab_per_view(server) -> None:
    with signed_in(server) as client:
        response = client.get("/apps/etienne/shop/staff")
    assert response.status_code == 200
    assert "Till" in response.text and "Shelves" in response.text and "Delivery" in response.text
    assert 'aria-current="page">Till<' in response.text
    # The first view opens first. The page shows, along the top of its window, that a click on
    # the screen works: a long screen would scroll the screen's own bar out of sight.
    frame = 'data-src="/apps/etienne/shop/staff/screen?view=till&width=full&embed"'
    assert frame in response.text
    assert '"progress progress-primary working-bar"' in response.text


def test_a_tab_reads_as_the_view_s_title_in_the_order_the_views_are_written(server) -> None:
    code = example_code()
    plain = '@shop.view(roles=["owner", "staff"])\ndef shelves('
    titled = '@shop.view(roles=["owner", "staff"], title="En rayon")\ndef shelves('
    source = code["app.py"].decode()
    assert plain in source
    code["app.py"] = source.replace(plain, titled).encode()
    server.state.apps.publish("etienne", "shop", code, note="a title")

    with signed_in(server) as client:
        page = client.get("/apps/etienne/shop/staff").text
        opened = client.get("/apps/etienne/shop/staff?view=shelves").text
        manage = client.get("/manage/etienne/shop/views?role=staff&view=shelves").text

    # A view without a title reads as its name, and the tabs follow app.py's order.
    tabs = [page.index(f">{label}</a>") for label in ("Till", "En rayon", "Delivery")]
    assert tabs == sorted(tabs)
    assert "Shelves" not in page
    # The address still names the view by its function.
    assert '<a class="tab" href="?view=shelves">En rayon</a>' in page
    assert '<a class="tab" href="?view=shelves" aria-current="page">En rayon</a>' in opened
    assert 'title="En rayon"' in opened
    assert 'aria-current="page">En rayon</a>' in manage and "Shelves" not in manage


def test_the_screen_is_the_sdk_s_host_page(server) -> None:
    with signed_in(server) as client:
        response = client.get("/apps/etienne/shop/staff/screen?view=shelves&embed")
    assert response.status_code == 200
    assert response.text == host_page()


def test_rpc_lists_and_calls_as_person(server) -> None:
    with signed_in(server) as client:
        listed = client.post("/apps/etienne/shop/staff/rpc", json={"method": "tools/list"}).json()
        names = {tool["name"] for tool in listed["tools"]}
        assert "record_sale" in names
        assert "set_price" not in names
        shown = client.post(
            "/apps/etienne/shop/staff/rpc",
            json={"method": "tools/call", "params": {"name": "shelves", "arguments": {}}},
        ).json()
        assert "structuredContent" in shown
        done = client.post(
            "/apps/etienne/shop/staff/rpc",
            json={
                "method": "tools/call",
                "params": {
                    "name": "record_sale",
                    "arguments": {"sku": "TEA-SEN", "quantity": 2, "receipt": "R-0400"},
                },
            },
        ).json()
        assert not done.get("isError")
    folder = server.state.apps.repo("etienne", "shop").folder
    assert git(folder, "log", "-1", "--format=%an").strip() == "etienne"


def test_rpc_reads_the_renderer(server) -> None:
    with signed_in(server) as client:
        tools = client.post("/apps/etienne/shop/staff/rpc", json={"method": "tools/list"}).json()[
            "tools"
        ]
        uri = next(
            tool["_meta"]["ui"]["resourceUri"] for tool in tools if tool["name"] == "shelves"
        )
        page = client.post(
            "/apps/etienne/shop/staff/rpc",
            json={"method": "resources/read", "params": {"uri": uri}},
        ).json()["contents"][0]
    assert page["mimeType"] == "text/html;profile=mcp-app"
    # Cliquet's own page, which loads nothing from the network.
    assert page["text"] == views.renderer_html()
    assert "<script src" not in page["text"] and "<link" not in page["text"]


def test_a_fault_in_a_member_s_call_reads_cliquet_s_fixed_words(server, monkeypatch) -> None:
    # A fault around the app's code, met in answering a screen's call: a member reads
    # Cliquet's fixed words and an id, never the exception's text. The owner reads the text,
    # there and under that id in the app's console.
    def broken(self, uri: str):
        raise RuntimeError(f"no resource at {uri}")

    monkeypatch.setattr(AppServer, "read_resource", broken)
    asked_for = {"method": "resources/read", "params": {"uri": "ui://secret"}}
    sam = a_shop_member(server, "staff")
    with TestClient(server) as client:
        act_as(server, client, sam)
        asked = client.post("/apps/etienne/shop/staff/rpc", json=asked_for)
    assert asked.status_code == 500
    told = re.fullmatch(
        rf"Cliquet couldn't answer this call\. Error ({ERROR_ID}) has the details: the app's "
        r"owner can read it in the app's console\.",
        asked.json()["error"],
    )
    assert told is not None and "secret" not in told.group(0)
    error = server.state.apps.database.error("etienne", "shop", told.group(1), NOW)
    assert error is not None and error.text.startswith("RuntimeError: no resource at ui://secret")
    assert (error.caller, error.role, error.tool) == ("sam", "staff", "resources/read")
    with signed_in(server) as client:
        own = client.post("/apps/etienne/shop/staff/rpc", json=asked_for).json()
    assert own["error"] == "RuntimeError: no resource at ui://secret"


def test_a_call_of_the_wrong_shape_is_refused_and_kept_nowhere(server) -> None:
    # A call a member can send at will is refused in Cliquet's words, and fills no error log.
    sam = a_shop_member(server, "staff")
    wrong = [
        ({"method": "tools/call", "params": {"arguments": {}}}, "a tools/call names its tool"),
        ({"method": "tools/call", "params": "x"}, "a screen's call names its method"),
        ({"method": "resources/read", "params": {}}, "a resources/read names its"),
        ({"method": "prompts/list"}, "Cliquet doesn't handle prompts/list"),
    ]
    with TestClient(server) as client:
        act_as(server, client, sam)
        for body, said in wrong:
            asked = client.post("/apps/etienne/shop/staff/rpc", json=body)
            assert asked.status_code == 400, body
            assert asked.json()["error"].startswith(said)
    assert server.state.apps.database.errors("etienne", "shop", NOW) == []


def test_a_call_that_isn_t_a_json_object_is_refused(server) -> None:
    # A body that isn't JSON, or JSON that isn't an object, is refused in Cliquet's words.
    headers = {"content-type": "application/json"}
    with signed_in(server) as client:
        broken = client.post("/apps/etienne/shop/staff/rpc", content=b"{", headers=headers)
        listed = client.post("/apps/etienne/shop/staff/rpc", json=["tools/list"])
    for asked in (broken, listed):
        assert asked.status_code == 400
        assert asked.json()["error"].startswith("a screen's call is a JSON object")


def test_an_unknown_role_or_view(server) -> None:
    with signed_in(server) as client:
        role = client.get("/apps/etienne/shop/boss")
        view = client.get("/apps/etienne/shop/staff?view=nope")
        rpc = client.post("/apps/etienne/shop/boss/rpc", json={"method": "tools/list"})
    assert role.status_code == 404
    assert "owner, staff" in role.text
    assert view.status_code == 404
    assert rpc.status_code == 404
    assert rpc.json()["error"]


def test_an_app_without_views_shows_its_tools(server, tmp_path: Path) -> None:
    code = template_code(tmp_path / "list")
    server.state.apps.database.create_app("etienne", "list", NOW)
    server.state.apps.publish("etienne", "list", code, note="test", data={})
    with signed_in(server) as client:
        text = client.get("/apps/etienne/list/-").text
    assert "has no screen" in text
    assert "/apps/etienne/list/mcp" in text
    # It says what to send an assistant, which lists the app's tools: the page doesn't.
    assert 'data-copy="Open List on Cliquet."' in text
    assert "add_item" not in text


def test_the_app_s_page_still_answers(server) -> None:
    with signed_in(server) as client:
        response = client.get("/apps/etienne/shop")
    assert response.url.path == "/apps/etienne/shop/owner"
    assert "Use Shop from your AI assistant" in response.text


def a_member(server) -> PersonInfo:
    """Sam, who may use etienne/shop as staff."""
    database = server.state.apps.database
    person = database.person_for(
        "https://example.eu.auth0.com/", "auth0|sam", "sam@example.com", "sam", NOW
    )
    return member(database, person, "etienne", "shop", "staff")


def test_an_app_opens_on_the_person_s_role(server, tmp_path: Path) -> None:
    person = a_member(server)
    code = template_code(tmp_path / "list")
    server.state.apps.database.create_app("etienne", "list", NOW)
    server.state.apps.publish("etienne", "list", code, note="test", data={})

    with signed_in(server) as client:
        owner = client.get("/apps/etienne/shop", follow_redirects=False)
        roleless = client.get("/apps/etienne/list", follow_redirects=False)
    with TestClient(server) as client:
        act_as(server, client, person)
        member = client.get("/apps/etienne/shop", follow_redirects=False)

    assert owner.status_code == 303
    assert owner.headers["location"] == "/apps/etienne/shop/owner"
    assert member.status_code == 303
    assert member.headers["location"] == "/apps/etienne/shop/staff"
    assert roleless.headers["location"] == "/apps/etienne/list/-"


def test_a_member_sees_both_mcp_addresses(server) -> None:
    person = a_member(server)
    address = "http://127.0.0.1:8800/me/mcp"
    agent = "http://127.0.0.1:8800/apps/etienne/shop/mcp"

    with TestClient(server) as client:
        act_as(server, client, person)
        text = client.get("/apps/etienne/shop/staff").text
    with signed_in(server) as client:
        owner = client.get("/apps/etienne/shop/owner").text

    # The address and how to add it are in a dialog, opened from the app's bar.
    assert '<dialog id="connect"' in text
    assert "Use Shop from your AI assistant" in text
    # First what to send an assistant, which opens the app by name, as staff.
    assert "It works as you, with the role staff" in text
    assert 'data-copy="Open Shop on Cliquet."' in text
    # Then, folded, the person's own endpoint for their assistant, and how to add it.
    assert "Not connected yet? Add Cliquet to your assistant" in text
    assert f'data-copy="{address}"' in text
    assert 'href="https://claude.ai/customize/connectors"' in text
    assert f"claude mcp add --transport http cliquet {address}" in text
    assert f"pi mcp add cliquet --url {address}" in text
    assert f"opencode mcp add cliquet --url {address}" in text
    # The app's own address, for an agent that runs on its own, with a token for it.
    assert "Running an agent on its own?" in text
    assert f'data-copy="{agent}"' in text
    assert 'href="/tokens?app=etienne/shop"' in text
    # The steps are for a server reached over the internet: no development token.
    assert "Bearer dev" not in text
    assert "/owner/mcp" not in text and "/staff/mcp" not in text
    # No list of tools or rules: the assistant lists them. A member sees nothing of the
    # owner's.
    assert "record_receipt" not in text and "set_price" not in text
    assert "/manage/etienne/shop" not in text
    # The owner may switch role, and manage the app.
    assert 'href="/apps/etienne/shop/staff"' in owner
    assert 'href="/manage/etienne/shop"' in owner


def test_the_hosted_screen_ignores_driver(server) -> None:
    with signed_in(server) as client:
        response = client.get("/apps/etienne/shop/staff/screen?view=till&shot&driver")
    assert response.status_code == 200
    assert "allow-same-origin" not in response.text


def test_rpc_refuses_another_origin(server) -> None:
    rpc = "/apps/etienne/shop/staff/rpc"
    listing = '{"method": "tools/list"}'
    with signed_in(server) as client:
        elsewhere = client.post(
            rpc,
            content=listing,
            headers={"content-type": "application/json", "origin": "https://evil.example"},
        )
        plain = client.post(rpc, content=listing, headers={"content-type": "text/plain"})
        own = client.post(
            rpc,
            content=listing,
            headers={"content-type": "application/json", "origin": "http://127.0.0.1:8800"},
        )
        same_address = client.post(
            rpc,
            content=listing,
            headers={"content-type": "application/json", "origin": "http://testserver"},
        )
        no_origin = client.post(rpc, json={"method": "tools/list"})
    assert elsewhere.status_code == 403
    assert elsewhere.json()["error"]
    assert plain.status_code == 403
    assert plain.json()["error"]
    assert own.status_code == 200
    assert "tools" in own.json()
    assert same_address.status_code == 200
    assert no_origin.status_code == 200
