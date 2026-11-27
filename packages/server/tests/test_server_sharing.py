import re
from typing import Any
from urllib.parse import quote

import anyio
import pytest
from starlette.testclient import TestClient

from cliquet_server import sharing
from cliquet_server.arguments import Refusal, not_held
from cliquet_server.builder.errors import no_such_app
from cliquet_server.builder.server import APP_STARTING
from cliquet_server.db import PersonInfo

from .server_helpers import (
    NOW,
    act_as,
    blocked_build,
    csrf_for,
    failing_build,
    member,
    published_before_roles_were_kept,
    signed_in,
    text,
    tools,
    unexpected,
)

FORM = re.compile(r'name="csrf" value="([^"]+)"')


def a_member(server, login: str = "sam", email: str = "sam@example.com") -> PersonInfo:
    return server.state.apps.database.person_for(
        "https://example.eu.auth0.com/", f"auth0|{login}", email, login, NOW
    )


def share(server, client: TestClient, email: str, role: str = "staff", headers=None, **extra):
    data = {"email": email, "role": role, "csrf": csrf_for(server, client), **extra}
    return client.post(
        "/manage/etienne/shop/share", data=data, headers=headers, follow_redirects=False
    )


def remove_member(server, client: TestClient, person_id: int, role: str = "staff"):
    return client.post(
        f"/manage/etienne/shop/members/{person_id}/{role}/remove",
        data={"csrf": csrf_for(server, client)},
        follow_redirects=False,
    )


def remove_waiting(server, client: TestClient, email: str, role: str = "staff"):
    return client.post(
        f"/manage/etienne/shop/shares/{quote(email, safe='')}/{role}/remove",
        data={"csrf": csrf_for(server, client)},
        follow_redirects=False,
    )


def test_the_owner_sees_the_sharing_section(server) -> None:
    person = a_member(server)
    database = server.state.apps.database
    member(database, person, "etienne", "shop", "staff")
    a_member(server, "pat", "pat@example.com")
    database.share("etienne", "shop", "pat@example.com", "staff", NOW)
    database.invite("etienne", "shop", "future@example.com", "staff", NOW)

    with signed_in(server) as client:
        text = client.get("/manage/etienne/shop/sharing").text

    assert 'action="/manage/etienne/shop/share"' in text
    # One list of people, each row with where they stand and a form that removes them.
    people = text.split('id="people"')[1].split("</ul>")[0]
    rows = {row.split("</li>")[0] for row in people.split("<li ")[1:]}
    (sam,) = (row for row in rows if "sam@example.com" in row)
    (pat,) = (row for row in rows if "pat@example.com" in row)
    (future,) = (row for row in rows if "future@example.com" in row)
    assert "Member<" in sam and f"/members/{person.id}/staff/remove" in sam
    assert "Hasn&#39;t accepted" in pat
    assert 'action="/manage/etienne/shop/shares/pat%40example.com/staff/remove"' in pat
    # A share not yet accepted shows only its email: the owner can't tell that pat has an
    # account and that future@example.com has none.
    assert ">pat<" not in pat
    assert pat.replace("pat", "someone") == future.replace("future", "someone")
    # Removing someone asks first, saying what it ends.
    assert "They lose this role at once." in sam
    assert "Their share is cancelled." in pat
    assert 'name="csrf"' in text
    assert 'name="email"' in text
    assert 'name="role"' in text
    assert ">staff<" in text
    # No role is chosen for the owner: each says what it may use.
    assert '<option value="" disabled selected>Choose a role</option>' in text
    assert '<option value="owner" selected>' not in text
    assert re.search(r"<strong>staff</strong> \d+ actions?", text)


def test_a_member_does_not_see_or_use_the_sharing_section(server) -> None:
    person = a_member(server)
    database = server.state.apps.database
    member(database, person, "etienne", "shop", "staff")

    with TestClient(server) as client:
        act_as(server, client, person)
        text = client.get("/apps/etienne/shop/staff").text
        response = client.post(
            "/manage/etienne/shop/share",
            data={"email": "bad@example.com", "role": "staff", "csrf": csrf_for(server, client)},
            follow_redirects=False,
        )

    assert "/manage/etienne/shop" not in text
    assert "sam@example.com" not in text
    assert response.status_code == 404


def test_the_owner_invites_an_unknown_email(server) -> None:
    with signed_in(server) as client:
        response = share(server, client, "future@example.com")

    assert response.status_code == 303
    invitations = server.state.apps.database.pending_invitations("etienne", "shop")
    assert [(invitation.email, invitation.role) for invitation in invitations] == [
        ("future@example.com", "staff")
    ]
    assert server.state.apps.database.may_open("future", "etienne", "shop") is False


def test_a_known_person_accepts_a_share_first(server) -> None:
    person = a_member(server)
    with signed_in(server) as client:
        response = share(server, client, person.email or "")
        client.cookies.clear()
        act_as(server, client, person)
        home = client.get("/").text
        before = client.get("/apps/etienne/shop")
        refused = client.post(
            "/shares/etienne/shop/accept", data={"csrf": "wrong"}, follow_redirects=False
        )
        accepted = client.post(
            "/shares/etienne/shop/accept",
            data={"csrf": csrf_for(server, client)},
            follow_redirects=False,
        )
        after = client.get(accepted.headers["location"])
        page = client.get("/apps/etienne/shop")
        again = client.post(
            "/shares/etienne/shop/accept",
            data={"csrf": csrf_for(server, client)},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/manage/etienne/shop/sharing?notice=shared"
    # Before it is accepted: the app's name, its owner and the role, and nothing it wrote.
    waiting = home.split('id="pending-shares"')[1].split('id="own-apps"')[0]
    assert "<strong>shop</strong>, shared by etienne" in waiting and ">staff<" in waiting
    assert 'action="/shares/etienne/shop/accept"' in waiting
    description = server.state.apps.database.app("etienne", "shop").description
    assert description and description not in home
    assert 'href="/apps/etienne/shop"' not in home
    assert before.status_code == 200 and "etienne shared shop with you" in before.text
    assert refused.status_code == 403
    assert accepted.status_code == 303
    # Accepted from the console, the person stays there, and finds the app in their list.
    assert accepted.headers["location"] == "/?notice=accepted"
    assert "The app is under Shared with you now." in after.text
    assert 'id="pending-shares"' not in after.text
    assert 'href="/apps/etienne/shop"' in after.text
    assert page.status_code == 200
    assert again.status_code == 404


def test_a_person_declines_a_share(server) -> None:
    person = a_member(server)
    server.state.apps.database.share("etienne", "shop", person.email or "", "staff", NOW)
    with TestClient(server) as client:
        act_as(server, client, person)
        declined = client.post(
            "/shares/etienne/shop/decline",
            data={"csrf": csrf_for(server, client)},
            follow_redirects=False,
        )
        home = client.get(declined.headers["location"]).text

    assert declined.status_code == 303
    assert "You declined the share. It gives you nothing." in home
    assert 'id="pending-shares"' not in home
    assert server.state.apps.database.may_open(person.login, "etienne", "shop") is False


def test_the_owner_removes_a_share_still_waiting(server) -> None:
    person = a_member(server)
    database = server.state.apps.database
    member(database, person, "etienne", "shop", "staff")
    database.share("etienne", "shop", person.email or "", "owner", NOW)
    with signed_in(server) as client:
        response = client.post(
            f"/manage/etienne/shop/shares/{quote(person.email or '', safe='')}/owner/remove",
            data={"csrf": csrf_for(server, client)},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert database.waiting_shares("etienne", "shop") == []
    # The role already accepted stays.
    assert database.may_use(person.login, "etienne", "shop", "staff") is True


def test_the_owner_removes_a_member(server) -> None:
    person = a_member(server)
    member(server.state.apps.database, person, "etienne", "shop", "staff")

    with signed_in(server) as client:
        response = remove_member(server, client, person.id)

    assert response.status_code == 303
    assert server.state.apps.database.may_open(person.login, "etienne", "shop") is False
    with TestClient(server) as client:
        act_as(server, client, person)
        page = client.get("/apps/etienne/shop")
    assert page.status_code == 404


def test_removing_a_member_s_role_keeps_their_other_roles(server) -> None:
    database = server.state.apps.database
    person = a_member(server)
    member(database, person, "etienne", "shop", "staff")
    member(database, person, "etienne", "shop", "owner")

    with signed_in(server) as client:
        rows = client.get("/manage/etienne/shop/sharing").text.split('id="people"')[1]
        response = remove_member(server, client, person.id, "owner")

    # Each of their roles is its own row, whose Remove takes that role alone, as it says.
    assert f"/members/{person.id}/owner/remove" in rows
    assert f"/members/{person.id}/staff/remove" in rows
    assert response.status_code == 303
    assert database.may_use(person.login, "etienne", "shop", "owner") is False
    assert database.may_use(person.login, "etienne", "shop", "staff") is True


def test_the_owner_removes_a_share_waiting_for_a_first_sign_in(server) -> None:
    server.state.apps.database.invite("etienne", "shop", "future@example.com", "staff", NOW)
    with signed_in(server) as client:
        response = remove_waiting(server, client, "future@example.com")

    assert response.status_code == 303
    assert server.state.apps.database.pending_invitations("etienne", "shop") == []


def test_the_owner_removes_a_share_whose_email_holds_a_slash(server) -> None:
    server.state.apps.database.invite("etienne", "shop", "a/b@example.com", "staff", NOW)
    with signed_in(server) as client:
        response = remove_waiting(server, client, "a/b@example.com")

    assert response.status_code == 303
    assert server.state.apps.database.pending_invitations("etienne", "shop") == []


def test_a_wrong_role_is_refused(server) -> None:
    with signed_in(server) as client:
        response = share(server, client, "future@example.com", role="boss")

    assert response.status_code == 400
    assert "You don&#39;t hold the role boss in etienne/shop. You hold owner and staff." in (
        response.text
    )
    assert "alert alert-error" in response.text
    assert server.state.apps.database.pending_invitations("etienne", "shop") == []


def test_only_a_published_app_is_shared_as_one_of_its_roles(server) -> None:
    apps = server.state.apps
    apps.database.create_app("etienne", "notes", NOW)
    draft, shop = apps.database.app("etienne", "notes"), apps.database.app("etienne", "shop")
    assert draft is not None and shop is not None

    def refused(info, role: str | None) -> str:
        with pytest.raises((ValueError, Refusal)) as error:
            anyio.run(sharing.share, apps, info, "future@example.com", role)
        return str(error.value)

    assert refused(draft, "staff") == (
        "etienne/notes isn't published yet: publish it before you share it."
    )
    # The roles kept at publish, and `-` only for an app without roles.
    assert refused(shop, "boss") == not_held("etienne/shop", "boss", ["owner", "staff"])
    assert refused(shop, "-") == not_held("etienne/shop", "-", ["owner", "staff"])
    # With no role, the app's one role: the shop has several.
    assert refused(shop, None) == "Name your role in etienne/shop: you hold owner and staff."
    done, said = anyio.run(sharing.share, apps, shop, "future@example.com", "staff")
    assert (done, said) == (
        "shared",
        sharing.SHARED.format(email="future@example.com", role="staff"),
    )
    # Shared at the server's own time.
    (invited,) = apps.database.pending_invitations("etienne", "shop")
    assert invited.created_at == apps.now()


def test_csrf_is_required(server) -> None:
    with signed_in(server) as client:
        missing = client.post(
            "/manage/etienne/shop/share",
            data={"email": "future@example.com", "role": "staff"},
            follow_redirects=False,
        )
        wrong = client.post(
            "/manage/etienne/shop/share",
            data={"email": "future@example.com", "role": "staff", "csrf": "wrong"},
            follow_redirects=False,
        )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert server.state.apps.database.pending_invitations("etienne", "shop") == []


def test_removing_yourself_is_refused(server) -> None:
    owner = server.state.apps.database.person_by_login("etienne")
    assert owner is not None
    with signed_in(server) as client:
        response = remove_member(server, client, owner.id, "owner")

    assert response.status_code == 400
    assert "the owner cannot be removed from their own app" in response.text
    assert server.state.apps.database.may_open("etienne", "etienne", "shop") is True


def test_inviting_the_owner_is_ignored(server) -> None:
    server.state.apps.database.person_for(
        "https://example.eu.auth0.com/", "auth0|owner", "etienne@example.com", "etienne", NOW
    )
    with signed_in(server) as client:
        response = share(server, client, "etienne@example.com")

    assert response.status_code == 303
    assert server.state.apps.database.pending_invitations("etienne", "shop") == []
    assert server.state.apps.database.members("etienne", "shop") == []


def test_sharing_answers_htmx_with_the_lists(server) -> None:
    htmx = {"HX-Request": "true"}
    with signed_in(server) as client:
        plain = share(server, client, "plain@example.com")
        invited = share(server, client, "future@example.com", headers=htmx)
        removed = client.post(
            "/manage/etienne/shop/shares/future%40example.com/staff/remove",
            data={"csrf": csrf_for(server, client)},
            headers=htmx,
        )

    assert plain.status_code == 303
    assert plain.headers["location"].startswith("/manage/etienne/shop/sharing?notice=")
    # htmx gets the sharing part of the page, with the new lists, and no whole page.
    assert invited.status_code == 200
    assert 'id="sharing"' in invited.text
    assert "<html" not in invited.text
    assert "Shared with future@example.com as staff." in invited.text
    assert "future@example.com" in invited.text and "plain@example.com" in invited.text
    assert removed.status_code == 200
    assert "Share removed." in removed.text
    assert "future@example.com" not in removed.text.replace("Invitation removed.", "")


def test_an_app_s_page_offers_a_share_that_waits(server) -> None:
    person = a_member(server)
    server.state.apps.database.share("etienne", "shop", person.email or "", "staff", NOW)
    stranger = a_member(server, "pat", "pat@example.com")
    with TestClient(server) as client:
        act_as(server, client, person)
        opened = client.get("/apps/etienne/shop", follow_redirects=False)
        role = client.get("/apps/etienne/shop/staff")
        other_role = client.get("/apps/etienne/shop/owner")
        screen = client.get("/apps/etienne/shop/staff/screen")
        accepted = client.post(
            "/shares/etienne/shop/accept",
            data={"csrf": csrf_for(server, client), "open": "app"},
            follow_redirects=True,
        )
        client.cookies.clear()
        act_as(server, client, stranger)
        refused = client.get("/apps/etienne/shop")

    # Not a refusal: the share, with only what Cliquet knows itself, to accept or decline.
    for page in (opened, role, other_role):
        assert page.status_code == 200
        assert "etienne shared shop with you" in page.text and "As staff." in page.text
        assert 'action="/shares/etienne/shop/accept"' in page.text
        assert 'action="/shares/etienne/shop/decline"' in page.text
        assert 'name="csrf"' in page.text
    description = server.state.apps.database.app("etienne", "shop").description
    assert description not in opened.text and "Tea shop" not in opened.text
    # The frame that draws a screen stays shut.
    assert screen.status_code == 404
    assert accepted.status_code == 200 and accepted.url.path == "/apps/etienne/shop/staff"
    # Someone with no share still gets the page of a missing app.
    assert refused.status_code == 404 and "There is no app etienne/shop here." in refused.text


def test_the_home_page_offers_one_share_per_app(server) -> None:
    person = a_member(server)
    database = server.state.apps.database
    for role in ("staff", "owner"):
        database.share("etienne", "shop", person.email or "", role, NOW)
    with TestClient(server) as client:
        act_as(server, client, person)
        home = client.get("/").text
        planted = client.get("/?notice=Your+token+leaked").text
    waiting = home.split('id="pending-shares"')[1].split('id="own-apps"')[0]
    # Accepting takes every role shared there, so the app is one row with both.
    assert waiting.count('action="/shares/etienne/shop/accept"') == 1
    assert ">staff<" in waiting and ">owner<" in waiting
    # A notice is a fixed sentence: an address can't put its own words on the page.
    assert "Your token leaked" not in planted


def test_sharing_says_what_it_did(server) -> None:
    person = a_member(server)
    member(server.state.apps.database, person, "etienne", "shop", "staff")
    server.state.apps.database.person_for(
        "https://example.eu.auth0.com/", "auth0|owner", "etienne@example.com", "etienne", NOW
    )
    with signed_in(server) as client:
        held = share(server, client, person.email or "")
        own = share(server, client, "etienne@example.com")
        held_page = client.get(held.headers["location"]).text
        own_page = client.get(own.headers["location"]).text
        htmx = share(server, client, person.email or "", headers={"HX-Request": "true"}).text

    # A role held already reads as any share: until a share is accepted, the owner knows
    # only the email they typed.
    assert held.headers["location"] == "/manage/etienne/shop/sharing?notice=shared"
    assert f"Shared. {sharing.WAITS}" in held_page
    assert own.headers["location"] == "/manage/etienne/shop/sharing?notice=owner"
    assert "the owner holds every role" in own_page
    # htmx is answered in place, so its sentence can name the email and the role.
    assert sharing.SHARED.format(email=person.email, role="staff") in htmx


def test_the_sharing_tab_shows_no_notice_from_the_address(server) -> None:
    with signed_in(server) as client:
        planted = client.get("/manage/etienne/shop/sharing?notice=Your+app+leaked").text
        made_up = client.get("/manage/etienne/shop/sharing?notice=anything").text
        known = client.get("/manage/etienne/shop/sharing?notice=member-removed").text

    # A notice is a code for a fixed sentence: an address can't put its own words on the page.
    assert "Your app leaked" not in planted and 'role="status"' not in planted
    assert "anything" not in made_up and 'role="status"' not in made_up
    assert "Member removed." in known


def test_sharing_says_the_same_whether_or_not_the_email_has_an_account(server) -> None:
    known = a_member(server)
    htmx = {"HX-Request": "true"}
    with signed_in(server) as client:
        said = {
            email: share(server, client, email, headers=htmx).text
            for email in (known.email or "", "future@example.com")
        }
        plain = [share(server, client, email) for email in said]

    # Until a share is accepted, the owner knows only the email they typed.
    for email, page in said.items():
        assert sharing.SHARED.format(email=email, role="staff") in page
    assert {answer.headers["location"] for answer in plain} == {
        "/manage/etienne/shop/sharing?notice=shared"
    }


# ---------------------------------------------------------------- share_app and revoke_share


def not_kept(server) -> tuple[str, dict[str, Any]]:
    """A share of the shop, published before its roles were kept: its start reads them."""
    apps = server.state.apps
    published_before_roles_were_kept(apps.database, "etienne", "shop")
    apps.forget("etienne", "shop")
    return "share_app", {"app": "shop", "email": "sam@example.com", "role": "staff"}


def test_sharing_an_app_that_is_starting_says_so(
    server, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    server.state.apps.start_wait = 0
    shared = not_kept(server)
    with blocked_build(server.state.apps, "shop", monkeypatch):
        (starting,) = tools(server, shared)

    assert starting.is_error and text(starting) == APP_STARTING
    assert not unexpected(caplog)


def test_sharing_an_app_that_can_t_start_says_why(
    server, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    shared = not_kept(server)
    with failing_build(server.state.apps, "shop", monkeypatch):
        (failed,) = tools(server, shared)

    assert failed.is_error
    assert text(failed) == "etienne/shop can't be started: the sandbox is away"
    # The start logs its own failure: the tool adds no crash of its own.
    assert not [one for one in unexpected(caplog) if "share_app" in one.getMessage()]
    assert server.state.apps.database.pending_invitations("etienne", "shop") == []


def test_an_agent_shares_an_app_in_the_same_words_whatever_the_email(server) -> None:
    known = a_member(server)
    staff = {"app": "shop", "role": "staff"}
    emails = (known.email or "", "future@example.com")

    first = tools(server, *(("share_app", {**staff, "email": email}) for email in emails))
    # Again: one email has a share waiting, the other an invitation.
    again = tools(server, *(("share_app", {**staff, "email": email}) for email in emails))

    for email, answer in zip(emails * 2, first + again, strict=True):
        assert not answer.is_error
        assert text(answer) == sharing.SHARED.format(email=email, role="staff")
        assert answer.structured_content == {
            "app": "etienne/shop",
            "email": email,
            "done": "shared",
        }
    assert "Cliquet sends no email: tell them." in sharing.SHARED
    database = server.state.apps.database
    assert [(one.person.login, one.role) for one in database.waiting_shares("etienne", "shop")] == [
        ("sam", "staff")
    ]
    assert [(one.email, one.role) for one in database.pending_invitations("etienne", "shop")] == [
        ("future@example.com", "staff")
    ]


def test_share_app_makes_the_console_s_checks(server) -> None:
    database = server.state.apps.database
    database.create_app("etienne", "notes", NOW)
    database.person_for("https://example.eu.auth0.com/", "auth0|ada", "ada@example.com", "ada", NOW)
    database.create_app("ada", "books", NOW)
    email = {"email": "future@example.com"}

    answers = tools(
        server,
        ("share_app", {"app": "shop", **email}),
        ("share_app", {"app": "shop", **email, "role": "boss"}),
        ("share_app", {"app": "notes", **email}),
        ("share_app", {"app": "nope", **email}),
        ("share_app", {"app": "ada/books", **email}),
        ("share_app", {"app": "etienne/shop", "email": "etienne@example.com", "role": "owner"}),
    )

    said = [text(answer) for answer in answers]
    assert [answer.is_error for answer in answers] == [True] * 5 + [False]
    assert said[:5] == [
        "Name your role in etienne/shop: you hold owner and staff.",
        "You don't hold the role boss in etienne/shop. You hold owner and staff.",
        "etienne/notes isn't published yet: publish it before you share it.",
        no_such_app("nope"),
        "Only ada builds ada/books. Name one of your own apps, alone or as etienne/<name>: "
        "list_apps lists them.",
    ]
    assert said[5] == sharing.OWNERS.format(email="etienne@example.com")
    assert database.pending_invitations("etienne", "shop") == []


def test_an_agent_revokes_a_role_a_member_or_a_share_not_yet_accepted(server) -> None:
    database = server.state.apps.database
    sam = a_member(server)
    member(database, sam, "etienne", "shop", "staff")
    # A new role for a member is a share they accept: it waits, by their email.
    database.share("etienne", "shop", "sam@example.com", "owner", NOW)
    database.invite("etienne", "shop", "future@example.com", "staff", NOW)
    database.invite("etienne", "shop", "future@example.com", "owner", NOW)

    answers = tools(
        server,
        ("revoke_share", {"app": "shop", "who": "sam", "role": "staff"}),
        ("revoke_share", {"app": "shop", "who": "sam"}),
        ("revoke_share", {"app": "shop", "who": "Sam@example.com"}),
        ("revoke_share", {"app": "shop", "who": "future@example.com", "role": "owner"}),
        ("revoke_share", {"app": "shop", "who": "future@example.com"}),
    )

    said = [text(answer) for answer in answers]
    assert said[0] == "sam no longer holds etienne/shop as staff, from now on."
    assert answers[0].structured_content == {
        "app": "etienne/shop",
        "who": "sam",
        "roles": ["staff"],
    }
    # Nothing left to take back from sam the member: the share waiting for them is listed by
    # email, and named so.
    assert answers[1].is_error
    assert said[1] == (
        "Nothing of etienne/shop is held by sam, or waits for them: call read_app_overview for "
        "its members and the shares not yet accepted."
    )
    # A share waiting for a person and an invitation read alike: the owner knows only the email.
    assert said[2] == "The share of etienne/shop with Sam@example.com as owner is cancelled."
    assert said[3] == "The share of etienne/shop with future@example.com as owner is cancelled."
    assert said[4] == "The share of etienne/shop with future@example.com as staff is cancelled."
    assert database.members("etienne", "shop") == []
    assert database.waiting_shares("etienne", "shop") == []
    assert database.pending_invitations("etienne", "shop") == []


def test_revoking_every_role_of_a_member(server) -> None:
    database = server.state.apps.database
    sam = a_member(server)
    for role in ("staff", "owner"):
        member(database, sam, "etienne", "shop", role)

    (answer,) = tools(server, ("revoke_share", {"app": "etienne/shop", "who": " sam "}))

    assert text(answer) == "sam no longer holds etienne/shop as owner and staff, from now on."
    assert database.members("etienne", "shop") == []
    assert database.may_open("sam", "etienne", "shop") is False


def test_revoking_every_role_of_a_member_cancels_the_shares_waiting_for_them(server) -> None:
    database = server.state.apps.database
    sam = a_member(server)
    member(database, sam, "etienne", "shop", "staff")
    database.share("etienne", "shop", "sam@example.com", "owner", NOW)

    # A login is found whatever its case, and an app's name whatever spaces surround it.
    (answer,) = tools(server, ("revoke_share", {"app": " shop ", "who": "Sam"}))

    assert text(answer) == "sam no longer holds etienne/shop as staff, from now on."
    # Or they could accept the share still waiting, and come back.
    assert database.waiting_shares("etienne", "shop") == []
    assert database.may_open("sam", "etienne", "shop") is False


def test_the_overview_lists_members_by_login_and_other_shares_by_email(server) -> None:
    database = server.state.apps.database
    sam = a_member(server)
    member(database, sam, "etienne", "shop", "staff")
    database.share("etienne", "shop", "sam@example.com", "owner", NOW)
    a_member(server, "pat", "pat@example.com")
    database.share("etienne", "shop", "pat@example.com", "staff", NOW)
    database.invite("etienne", "shop", "future@example.com", "staff", NOW)

    overview = server.state.builder.read_app_overview("etienne", "shop")

    assert overview["members"] == [{"login": "sam", "roles": ["staff"]}]
    # A share waiting for a person and an invitation read alike, by the email they were sent
    # to: never by the login of the person it waits for.
    assert overview["waiting"] == [
        {"email": "future@example.com", "role": "staff"},
        {"email": "pat@example.com", "role": "staff"},
        {"email": "sam@example.com", "role": "owner"},
    ]


def a_new_email(server, person: PersonInfo, email: str) -> None:
    """Sign the person in again, with a new email the sign-in provider now gives them."""
    known = server.state.apps.database.known_person(
        "https://example.eu.auth0.com/", f"auth0|{person.login}", email, NOW
    )
    assert known is not None and known.email == email


def test_a_share_keeps_the_email_it_was_shared_with(server) -> None:
    database = server.state.apps.database
    sam = a_member(server)
    database.share("etienne", "shop", "sam@example.com", "staff", NOW)
    a_new_email(server, sam, "sam.new@example.com")

    overview = server.state.builder.read_app_overview("etienne", "shop")
    (answer,) = tools(server, ("revoke_share", {"app": "shop", "who": "sam@example.com"}))

    # Their new email would tell the owner that sam@example.com has an account.
    assert overview["waiting"] == [{"email": "sam@example.com", "role": "staff"}]
    assert "sam.new" not in str(overview)
    assert text(answer) == "The share of etienne/shop with sam@example.com as staff is cancelled."
    assert database.waiting_shares("etienne", "shop") == []


def test_the_sharing_tab_keeps_the_email_a_share_was_sent_to(server) -> None:
    database = server.state.apps.database
    sam = a_member(server)
    database.share("etienne", "shop", "sam@example.com", "staff", NOW)
    a_new_email(server, sam, "sam.new@example.com")

    with signed_in(server) as client:
        text = client.get("/manage/etienne/shop/sharing").text
        removed = remove_waiting(server, client, "sam@example.com")

    people = text.split('id="people"')[1].split("</ul>")[0]
    assert 'action="/manage/etienne/shop/shares/sam%40example.com/staff/remove"' in people
    # Their new email would tell the owner that sam@example.com has an account.
    assert "sam.new" not in text
    assert removed.status_code == 303
    assert database.waiting_shares("etienne", "shop") == []
