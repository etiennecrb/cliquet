import subprocess
from pathlib import Path

import anyio
import httpx
import pytest

from cliquet import screenshot
from cliquet_server.builder import Builder
from cliquet_server.db import Right
from cliquet_server.executor import LocalExecutor

from .server_helpers import (
    AUTH0_ISSUER,
    NOW,
    app_client,
    app_token,
    draft_example,
    oidc_server,
    running,
)

SESSION = "session"

pytestmark = [pytest.mark.browser]

needs_browser = pytest.mark.skipif(
    screenshot.find_browser() is None, reason="needs a Chrome, Chromium or Edge"
)


def sign_in(app, client: httpx.Client, person) -> None:
    client.cookies.set(SESSION, app.state.web_auth.session_cookie(person))


def csrf(app, client: httpx.Client) -> str:
    token = app.state.web_auth.csrf_for_session(client.cookies.get(SESSION))
    assert token is not None
    return token


def git(folder: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=folder, capture_output=True, text=True, check=True
    ).stdout


@needs_browser
def test_an_owner_shares_and_a_member_uses_a_role(tmp_path: Path, executor: LocalExecutor) -> None:
    app = oidc_server(tmp_path, executor)
    database = app.state.apps.database
    database.person_for(AUTH0_ISSUER, "auth0|owner", "etienne@example.com", "etienne", NOW)
    # The owner builds and manages the app.
    owner = database.grant("etienne", Right.BUILD)
    builder = Builder(app.state.apps, app.state.settings)
    draft_example(builder, "shop")
    published = builder.publish_app("etienne", "shop", "Private beta test")
    assert published["published"] is True

    member = database.person_for(AUTH0_ISSUER, "auth0|sam", "sam@example.com", "sam", NOW)

    with running(app) as address:
        with httpx.Client(base_url=address) as web:
            anonymous_home = web.get("/", follow_redirects=False)
            anonymous_page = web.get("/apps/etienne/shop", follow_redirects=False)
            sign_in(app, web, owner)
            page = web.get("/manage/etienne/shop/sharing")
            assert page.status_code == 200
            assert "People" in page.text
            invited = web.post(
                "/manage/etienne/shop/share",
                data={
                    "email": member.email or "",
                    "role": "staff",
                    "csrf": csrf(app, web),
                },
                follow_redirects=False,
            )
            assert invited.status_code == 303
            assert database.members("etienne", "shop") == []
            web.cookies.clear()
            sign_in(app, web, member)
            # Until sam accepts, the share gives nothing: the app's page offers it, and the
            # endpoint doesn't serve it.
            waiting = web.get("/")
            assert 'action="/shares/etienne/shop/accept"' in waiting.text
            offered = web.get("/apps/etienne/shop")
            assert offered.status_code == 200 and "etienne shared shop with you" in offered.text
            assert web.get("/apps/etienne/shop/staff/screen").status_code == 404
            # Nor can sam make a token for it.
            with pytest.raises(ValueError, match="You don't hold the role staff"):
                app_token(app, "staff", person=member)
            accepted = web.post(
                "/shares/etienne/shop/accept",
                data={"csrf": csrf(app, web)},
                follow_redirects=False,
            )
            assert accepted.status_code == 303
            assert database.members("etienne", "shop")[0].person.login == "sam"
            home = web.get("/")
            page = web.get("/apps/etienne/shop", follow_redirects=True)
            staff = web.get("/apps/etienne/shop/staff")
            owner_page = web.get("/apps/etienne/shop/owner")
            assert anonymous_home.headers["location"] == "/login?next=%2F"
            assert anonymous_page.status_code == 303
            assert 'href="/apps/etienne/shop"' in home.text
            assert page.status_code == 200
            assert page.url.path == "/apps/etienne/shop/staff"
            assert staff.status_code == 200
            assert owner_page.status_code == 403

        token = app_token(app, "staff", person=member)

        async def member_agent() -> None:
            async with app_client(address, "/apps/etienne/shop/mcp", token) as client:
                names = {tool.name for tool in (await client.list_tools()).tools}
                assert "record_sale" in names
                assert "set_price" not in names
                done = await client.call_tool(
                    "record_sale",
                    {"sku": "TEA-SEN", "quantity": 2, "receipt": "R-0700"},
                )
                assert not done.is_error

        anyio.run(member_agent)
        with pytest.raises(ValueError, match="You don't hold the role owner"):
            app_token(app, "owner", person=member)

        folder = app.state.apps.repo("etienne", "shop").folder
        assert git(folder, "log", "-1", "--format=%an").strip() == "sam"
        assert "App-Role: staff" in git(folder, "log", "-1", "--format=%B")

        with httpx.Client(base_url=address) as web:
            web.cookies.set(SESSION, app.state.web_auth.session_cookie(owner))
            page = web.get("/manage/etienne/shop/sharing")
            assert "People" in page.text
            removed = web.post(
                f"/manage/etienne/shop/members/{member.id}/staff/remove",
                data={"csrf": csrf(app, web)},
                follow_redirects=False,
            )
            assert removed.status_code == 303

        with httpx.Client(base_url=address) as web:
            web.cookies.set(SESSION, app.state.web_auth.session_cookie(member))
            assert web.get("/apps/etienne/shop").status_code == 404
        after = httpx.post(
            f"{address}/apps/etienne/shop/mcp", headers={"Authorization": f"Bearer {token}"}
        )
        assert after.status_code == 404
