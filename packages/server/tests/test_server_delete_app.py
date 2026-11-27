"""An owner who may build deletes one of their apps from the console, published or a draft,
once they type its name: the page needs no start, and deletes what the builder's tool does."""

import html

from starlette.applications import Starlette
from starlette.testclient import TestClient

from cliquet_server.db import Right

from .server_helpers import a_shop_member, act_as, csrf_for, signed_in

PAGE = "/manage/etienne/shop/delete"


def test_the_owner_deletes_a_published_app_once_they_type_its_name(server: Starlette) -> None:
    database = server.state.apps.database
    repo = server.state.apps.repo("etienne", "shop").folder
    with signed_in(server) as client:
        overview = client.get("/manage/etienne/shop").text
        page = client.get(PAGE)
        csrf = csrf_for(server, client)
        expired = client.post(PAGE, data={"csrf": "wrong", "name": "shop"})
        mistyped = [
            client.post(PAGE, data={"csrf": csrf, "name": typed})
            for typed in ("", "Shop", " shop", "etienne/shop")
        ]
        assert database.app("etienne", "shop") is not None and repo.exists()
        deleted = client.post(PAGE, data={"csrf": csrf, "name": "shop"}, follow_redirects=False)
        home = client.get(deleted.headers["location"]).text
        gone = client.get(PAGE)

    # The overview links the page, which offers the download first and asks for the name.
    assert f'href="{PAGE}">Delete this app</a>' in overview
    assert page.status_code == 200
    text = html.unescape(page.text)
    assert "Delete this app for good" in text
    assert 'href="/manage/etienne/shop/download"' in text
    assert f'action="{PAGE}"' in text
    assert "Type the app's name, shop, to confirm" in text
    assert 'pattern="shop"' in text
    assert expired.status_code == 403
    assert all(answer.status_code == 400 for answer in mistyped)
    assert "Nothing was deleted: type the app's name, shop, exactly as it is." in html.unescape(
        mistyped[1].text
    )
    assert deleted.status_code == 303 and deleted.headers["location"] == "/?notice=deleted"
    assert "The app is deleted, with its data and its history." in home
    assert database.app("etienne", "shop") is None
    assert not repo.exists()
    assert gone.status_code == 404


def test_a_draft_is_deleted_from_its_card(server: Starlette) -> None:
    builder = server.state.builder
    builder.create_app("etienne", "notes")
    draft = builder.drafts.folder("etienne", "notes")
    with signed_in(server) as client:
        home = client.get("/").text
        page = html.unescape(client.get("/manage/etienne/notes/delete").text)
        deleted = client.post(
            "/manage/etienne/notes/delete",
            data={"csrf": csrf_for(server, client), "name": "notes"},
            follow_redirects=False,
        )

    assert 'href="/manage/etienne/notes/delete"' in home
    assert "etienne/notes · a draft, never published" in page
    # A draft has no download: nothing of it is live.
    assert "/download" not in page
    assert deleted.status_code == 303
    assert server.state.apps.database.app("etienne", "notes") is None
    assert not draft.exists()


def test_nobody_but_an_owner_who_may_build_deletes_an_app(server: Starlette) -> None:
    database = server.state.apps.database
    sam = a_shop_member(server, "owner")
    with TestClient(server) as anonymous:
        signed_out = anonymous.get(PAGE, follow_redirects=False)
        posted = anonymous.post(PAGE, data={"name": "shop"}, follow_redirects=False)
    # sam holds the role named owner, and is still a member, not the app's owner.
    with TestClient(server) as other:
        act_as(server, other, sam)
        not_theirs = other.get(PAGE)
        refused = other.post(PAGE, data={"csrf": csrf_for(server, other), "name": "shop"})
    # etienne, once they may no longer build, deletes it with their account only. Withdrawn
    # after the sign-in, which grants the development login the right: the next request reads
    # the person again.
    with signed_in(server) as client:
        database.withdraw("etienne", Right.BUILD)
        former = client.get(PAGE)
        former_post = client.post(PAGE, data={"csrf": csrf_for(server, client), "name": "shop"})

    assert signed_out.status_code == posted.status_code == 303
    assert signed_out.headers["location"].startswith("/login")
    assert not_theirs.status_code == refused.status_code == 404
    assert former.status_code == former_post.status_code == 404
    assert database.app("etienne", "shop") is not None
