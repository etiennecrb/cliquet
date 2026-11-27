"""The account page lists what the signed-in person owns and holds, and deletes their own
account only, once they type their login."""

import html
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from cliquet_server.accounts import Accounts
from cliquet_server.app import create_app
from cliquet_server.db import Database, Right
from cliquet_server.executor import LocalExecutor
from cliquet_server.identity import login_problem
from cliquet_server.settings import Settings

from .server_helpers import (
    APPS,
    NOW,
    a_shop_member,
    act_as,
    csrf_for,
    member,
    one_unexpected,
    signed_in,
)

NOTICE = "https://cliquet.example/privacy"


def _a_club(database: Database, name: str) -> None:
    """kim's published app."""
    database.create_app("kim", name, NOW, email="kim@example.com")
    database.mark_published("kim", name, NOW)


def test_the_account_page_lists_what_the_person_owns_and_holds(server: Starlette) -> None:
    database = server.state.apps.database
    server.state.builder.create_app("etienne", "notes")
    etienne = database.person_by_login("etienne")
    assert etienne is not None
    # A club etienne is a member of, and a choir whose share waits for them.
    _a_club(database, "club")
    member(database, etienne, "kim", "club", "member")
    _a_club(database, "choir")
    database.share("kim", "choir", "etienne@example.com", "member", NOW)
    database.create_token(etienne.id, "Laptop at home", NOW)
    with signed_in(server) as client:
        home = client.get("/").text
        page = client.get("/account")

    # The person's login in the sidebar opens it.
    assert '<a class="person-card" href="/account">' in home
    assert '<span class="person-name">etienne</span>' in home
    assert page.status_code == 200
    text = html.unescape(page.text)
    assert 'href="/manage/etienne/shop/download"' in text
    assert "etienne/notes · a draft, never published" in text
    assert "kim/club" in text and "kim/choir" in text
    assert "Shared with you, waiting for you to accept it" in text
    assert "Laptop at home" in text
    assert "your login, which nobody can take after you" in text
    assert "Your identity at the sign-in provider, and the server's backups, stay" in text
    # No provider is named, and no number of days.
    assert "Auth0" not in text and " days" not in text
    assert 'action="/account/delete"' in text
    assert "account" in str(login_problem("account"))


def test_the_page_links_the_operator_s_privacy_notice(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    settings = Settings.from_env(
        {
            "CLIQUET_DATA_DIR": str(tmp_path / "data"),
            "CLIQUET_DEV_LOGIN": "etienne",
            "CLIQUET_PRIVACY_URL": NOTICE,
        }
    )
    with signed_in(create_app(settings, executor=executor, clock=lambda: NOW)) as client:
        page = client.get("/account").text
    assert f'<a class="link" href="{NOTICE}" rel="privacy-policy">privacy notice</a>' in page


def test_only_the_signed_in_person_deletes_their_account_by_typing_their_login(
    server: Starlette,
) -> None:
    database = server.state.apps.database
    sam = a_shop_member(server, "staff")
    with TestClient(server) as client:
        anonymous = client.get("/account", follow_redirects=False)
        sent = client.post("/account/delete", data={"login": "etienne"}, follow_redirects=False)
    assert anonymous.status_code == sent.status_code == 303
    assert anonymous.headers["location"] == "/login?next=%2Faccount"

    with signed_in(server) as client:
        csrf = csrf_for(server, client)
        expired = client.post("/account/delete", data={"csrf": "wrong", "login": "etienne"})
        refused = [
            client.post("/account/delete", data={"csrf": csrf, "login": typed})
            for typed in ("", "Etienne", " etienne", "sam")
        ]
        assert database.person_by_login("etienne") is not None
        assert database.app("etienne", "shop") is not None
        # sam, signed in, can name only their own account: etienne's stays.
        with TestClient(server) as other:
            act_as(server, other, sam)
            not_theirs = other.post(
                "/account/delete",
                data={"csrf": csrf_for(server, other), "login": "etienne"},
            )
        assert database.person_by_login("etienne") is not None
        deleted = client.post("/account/delete", data={"csrf": csrf, "login": "etienne"})
        after = client.get("/", follow_redirects=False)

    assert expired.status_code == 403
    assert all(answer.status_code == 400 for answer in refused)
    assert "type your login, etienne, exactly as it is" in refused[1].text
    assert not_theirs.status_code == 400
    assert deleted.status_code == 200
    assert "Your account is deleted" in deleted.text
    assert 'session=""' in deleted.headers["set-cookie"]
    # Signed out, with no provider's sign-out to pass through.
    assert after.status_code == 303 and after.headers["location"] == "/login?next=%2F"
    account = database.account("etienne")
    assert account is not None and account.deleted_at == NOW
    assert database.apps("etienne") == [] and database.memberships(sam.id) == []
    assert database.person_by_login("sam") is not None


def test_the_page_and_the_command_delete_the_same(
    server: Starlette, tmp_path: Path, executor: LocalExecutor
) -> None:
    """What the console leaves is what the operator's command leaves."""
    apps = server.state.apps

    def left(database: Database) -> tuple[object, ...]:
        account = database.account("etienne")
        assert account is not None
        return (
            account.deleted_at is not None,
            account.email,
            account.rights,
            database.apps("etienne"),
            database.sandbox("etienne"),
        )

    with signed_in(server) as client:
        csrf = csrf_for(server, client)
        client.post("/account/delete", data={"csrf": csrf, "login": "etienne"})
    by_page = left(apps.database)

    other = create_app(
        Settings.from_env(
            {"CLIQUET_DATA_DIR": str(tmp_path / "other"), "CLIQUET_DEV_LOGIN": "etienne"}
        ),
        executor=executor,
        clock=lambda: NOW,
    )
    other.state.apps.import_folder(APPS / "stock", "etienne", "shop", email="etienne@example.com")
    accounts: Accounts = other.state.accounts
    accounts.delete("etienne")
    assert left(other.state.apps.database) == by_page == (True, None, frozenset(), [], None)


def test_a_deletion_that_stops_signs_out_and_is_finished_later(
    server: Starlette, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    accounts: Accounts = server.state.accounts

    def stopped(login: str) -> None:
        raise RuntimeError("the sandbox went away")

    with signed_in(server) as client:
        csrf = csrf_for(server, client)
        monkeypatch.setattr(server.state.apps.executor, "destroy", stopped)
        answer = client.post("/account/delete", data={"csrf": csrf, "login": "etienne"})
    assert answer.status_code == 200
    assert "the server finishes it as it next starts" in answer.text
    assert 'session=""' in answer.headers["set-cookie"]
    record = one_unexpected(caplog, RuntimeError)
    assert record.getMessage() == "unexpected: the account etienne wasn't deleted"
    monkeypatch.undo()
    assert accounts.left("etienne")
    assert accounts.finish() == ["etienne"]


def test_a_person_without_the_build_right_deletes_their_account_too(
    server: Starlette,
) -> None:
    database = server.state.apps.database
    sam = a_shop_member(server, "staff")
    assert Right.BUILD not in sam.rights
    with TestClient(server) as client:
        act_as(server, client, sam)
        page = client.get("/account").text
        assert "You own no app." in page and "etienne/shop" in page
        csrf = csrf_for(server, client)
        deleted = client.post("/account/delete", data={"csrf": csrf, "login": "sam"})
    assert deleted.status_code == 200
    assert database.person_by_login("sam") is None
    assert [one.person.login for one in database.members("etienne", "shop")] == []
