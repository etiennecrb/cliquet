import re
import sqlite3
from datetime import datetime, timedelta, timezone

import anyio
import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from cliquet_server import person_tokens, tokens
from cliquet_server.app_auth import filled_tokens_link
from cliquet_server.arguments import Refusal
from cliquet_server.db import Database, MakerExpiring, PersonInfo, TokenExpired, TokenLimit
from cliquet_server.executor import SandboxError

from .server_helpers import (
    APPS,
    NOW,
    act_as,
    app_status,
    app_token,
    csrf_for,
    member,
    rpc_refusal,
    signed_in,
)

ISSUER = "https://example.eu.auth0.com/"
TOKEN = re.compile(r'data-copy="(cliquet_[A-Za-z0-9_-]+)"')
ADDRESS = re.compile(r'data-copy="(http[^"]+/mcp)"')


def named(found: tuple[PersonInfo, tuple[TokenLimit, ...] | None, tokens.ApiToken] | None) -> tuple:
    """What a token gives, with the token by its name alone."""
    assert found is not None
    return (*found[:2], found[2].name)


def test_a_token_is_kept_as_its_hash(database: Database) -> None:
    sam = database.person_for(ISSUER, "auth0|sam", "sam@example.com", "sam", NOW)
    info, token = database.create_token(sam.id, "  Laptop   at home ", NOW)
    assert info.name == "Laptop at home" and info.last_used_at is None
    assert token.startswith("cliquet_") and len(token) == len("cliquet_") + 43
    # The database holds its hash, never the token.
    path = database._engine.url.database
    assert path is not None
    with sqlite3.connect(path) as raw:
        rows = raw.execute("select hash from tokens").fetchall()
    assert rows == [(tokens.digest(token),)]
    with open(path, "rb") as file:
        assert token.encode() not in file.read()

    # Its id, which the apps it reaches see, is random: never the token, nor its row's number.
    assert re.fullmatch(r"tok_[0-9a-f]{16}", info.public_id)
    assert database.token_access(token, NOW) == (
        sam,
        None,
        tokens.ApiToken(info.name, info.public_id),
    )
    assert database.tokens(sam.id)[0].last_used_at == NOW
    # The last use is kept to the minute, so a busy client doesn't write on every request.
    database.token_access(token, NOW + timedelta(seconds=30))
    assert database.tokens(sam.id)[0].last_used_at == NOW
    later = NOW + timedelta(minutes=2)
    database.token_access(token, later)
    assert database.tokens(sam.id)[0].last_used_at == later
    for wrong in ("", "cliquet_", token + "x", token[len("cliquet_") :], "eyJhbGciOi"):
        assert database.token_access(wrong, NOW) is None


def test_a_token_is_named_and_revoked_alone(database: Database) -> None:
    sam = database.person_for(ISSUER, "auth0|sam", "sam@example.com", "sam", NOW)
    pat = database.person_for(ISSUER, "auth0|pat", "pat@example.com", "pat", NOW)
    home, first = database.create_token(sam.id, "home", NOW)
    _, second = database.create_token(sam.id, "phone", NOW + timedelta(seconds=1))
    assert [one.name for one in database.tokens(sam.id)] == ["phone", "home"]
    assert len({one.public_id for one in database.tokens(sam.id)}) == 2
    for name in ("", "   ", "x" * 61):
        with pytest.raises(ValueError, match="A token's name is one line"):
            database.create_token(sam.id, name, NOW)
    with pytest.raises(ValueError, match="already have a token named 'home'"):
        database.create_token(sam.id, "home", NOW)
    # Someone else's token isn't theirs to find or revoke.
    assert database.find_token(pat.id, home.public_id) is None
    assert database.find_token(sam.id, home.public_id) == (home.id, "home")
    assert database.revoke_token(pat.id, home.id) is None
    assert database.revoke_token(sam.id, home.id) == []
    assert database.revoke_token(sam.id, home.id) is None
    assert database.token_access(first, NOW) is None
    assert named(database.token_access(second, NOW)) == (sam, None, "phone")


def test_a_person_has_at_most_fifty_tokens(database: Database) -> None:
    sam = database.person_for(ISSUER, "auth0|sam", "sam@example.com", "sam", NOW)
    for number in range(tokens.MAX_TOKENS):
        database.create_token(sam.id, f"token {number}", NOW)
    with pytest.raises(ValueError, match="You have 50 tokens, the most there may be"):
        database.create_token(sam.id, "one more", NOW)


def test_a_token_works_for_its_days(database: Database) -> None:
    sam = database.person_for(ISSUER, "auth0|sam", "sam@example.com", "sam", NOW)
    usual, _ = database.create_token(sam.id, "usual", NOW)
    week, token = database.create_token(sam.id, "week", NOW, days=7)
    forever, _ = database.create_token(sam.id, "forever", NOW, days=None)
    assert usual.expires_at == NOW + timedelta(days=90)
    assert week.expires_at == NOW + timedelta(days=7)
    assert forever.expires_at is None
    for days in (0, 10, 1000):
        with pytest.raises(ValueError, match="A token works for 7, 30, 90 or 365 days"):
            database.create_token(sam.id, "odd", NOW, days=days)

    # It works until the minute it expires, and then acts as nobody, its use not kept.
    assert database.token_access(token, NOW + timedelta(days=7, seconds=-1)) is not None
    with pytest.raises(TokenExpired) as expired:
        database.token_access(token, NOW + timedelta(days=7, minutes=5))
    assert expired.value.at == week.expires_at and expired.value.person == sam
    assert expired.value.token == tokens.ApiToken("week", week.public_id)
    later = NOW + timedelta(days=8)
    listed = {one.name: one for one in database.tokens(sam.id)}
    assert listed["week"].last_used_at == NOW + timedelta(days=7, seconds=-1)
    assert listed["week"].expired(later) and not listed["usual"].expired(later)
    assert not listed["forever"].expired(later + timedelta(days=10_000))
    # It keeps its name until it is revoked.
    with pytest.raises(ValueError, match="Your token named 'week' expired: revoke it first"):
        database.create_token(sam.id, "week", later)
    database.revoke_token(sam.id, week.id)
    assert database.create_token(sam.id, "week", later)[0].expires_at is not None


def test_a_token_made_by_a_token_never_outlives_it(database: Database) -> None:
    sam = database.person_for(ISSUER, "auth0|sam", "sam@example.com", "sam", NOW)
    database.create_app("sam", "shop", NOW)
    database.mark_published("sam", "shop", NOW)
    shop = [TokenLimit("sam", "shop", "-")]
    month, _ = database.create_token(sam.id, "Month", NOW, days=30)
    forever, _ = database.create_token(sam.id, "Forever", NOW, days=None)

    # Without days, the default lifetime, cut to end with its maker.
    ends, _ = database.create_token(sam.id, "Ends", NOW, shop, month.public_id, days=None)
    assert ends.expires_at == month.expires_at
    usual, _ = database.create_token(sam.id, "Usual", NOW, shop, forever.public_id, days=None)
    assert usual.expires_at == NOW + timedelta(days=90)
    week, _ = database.create_token(sam.id, "Week", NOW, shop, month.public_id, days=7)
    assert week.expires_at == NOW + timedelta(days=7)
    # A lifetime past its maker's is refused, with the latest it may end.
    with pytest.raises(ValueError) as refused:
        database.create_token(sam.id, "Long", NOW, shop, month.public_id, days=90)
    assert str(refused.value) == (
        "The token you call with expires on 11 Dec 2026, 12:00 UTC, and a token it makes never "
        "outlives it: choose fewer days, or leave days out to end with it."
    )
    # A maker with less than the shortest lifetime left makes none, nor one that expired.
    soon = NOW + timedelta(days=24)
    with pytest.raises(MakerExpiring) as expiring:
        database.create_token(sam.id, "Late", soon, shop, month.public_id, days=None)
    assert expiring.value.at == month.expires_at
    with pytest.raises(ValueError, match="The token you are using expired"):
        database.create_token(sam.id, "Late", NOW + timedelta(days=31), shop, month.public_id)
    assert {one.name for one in database.tokens(sam.id)} == {
        "Month",
        "Forever",
        "Ends",
        "Usual",
        "Week",
    }


def test_an_expiry_is_said_in_utc() -> None:
    paris = datetime(2026, 11, 10, 1, 30, tzinfo=timezone(timedelta(hours=2)))
    assert tokens.expiry(paris) == "9 Nov 2026, 23:30 UTC"


def test_an_expired_token_is_refused_everywhere(server: Starlette, address: str) -> None:
    database = server.state.apps.database
    etienne = database.person_by_login("etienne")
    staff = [TokenLimit("etienne", "shop", "staff")]
    # Made 8 days before the server's clock, for 7 days.
    made = NOW - timedelta(days=8)
    _, limited = database.create_token(etienne.id, "Till", made, staff, days=7)
    _, everywhere = database.create_token(etienne.id, "Laptop", made, days=7)
    said = (
        "This token expired on 10 Nov 2026, 12:00 UTC: make another at "
        "http://127.0.0.1:8800/tokens."
    )

    # A limited one is refused on /me/mcp as expired too, not as limited.
    tried = [("/apps/etienne/shop/mcp", limited), ("/me/mcp", everywhere), ("/me/mcp", limited)]
    for path, token in tried:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        refused = httpx.post(f"{address}{path}", json={}, headers=headers)
        # As a revoked token is, with the sentence that says when, and no sign-in to try.
        # At an app's address, the sentence comes in a JSON-RPC error.
        if path.startswith("/apps/"):
            assert rpc_refusal(refused, 401) == said
        else:
            assert refused.status_code == 401, path
            assert refused.json() == {"error": said}
        challenge = refused.headers["WWW-Authenticate"]
        assert challenge == f'Bearer error="invalid_token", error_description="{said}"'
    assert [one.last_used_at for one in database.tokens(etienne.id)] == [None, None]


def test_the_tokens_page_shows_when_each_token_expires(server: Starlette) -> None:
    database = server.state.apps.database
    etienne = database.person_by_login("etienne")
    database.create_token(etienne.id, "Old", NOW - timedelta(days=8), days=7)
    database.create_token(etienne.id, "Forever", NOW, days=None)
    database.create_token(etienne.id, "Usual", NOW)
    with signed_in(server) as client:
        csrf = csrf_for(server, client)
        made = [
            client.post("/tokens", data={"name": name, **chosen, "csrf": csrf})
            for name, chosen in (
                ("Week", {"limit": "etienne/shop/staff", "days": "7"}),
                ("Home", {"limit": "everywhere", "days": "never"}),
                ("Odd", {"limit": "everywhere", "days": "8"}),
                # The name is taken: the form comes back as it was filled.
                ("Home", {"limit": "everywhere", "days": "never"}),
            )
        ]
        page = client.get("/tokens").text

    week, never, odd, again = made
    assert week.status_code == 200 and never.status_code == 200
    # The new token says when it expires, as its row does.
    assert "Expires 18 Nov 2026, 12:00" in week.text
    assert "Never expires." in never.text
    assert again.status_code == 400
    assert '<option value="never" selected>Never</option>' in again.text
    assert 'value="Home"' in again.text and "Your new token" not in again.text
    assert odd.status_code == 400 and "Choose when the token expires." in odd.text
    listed = {one.name: one for one in database.tokens(etienne.id)}
    assert listed["Week"].expires_at == NOW + timedelta(days=7)
    assert listed["Home"].limits is None and listed["Home"].expires_at is None
    # The form offers each lifetime, 90 days unless a link says otherwise.
    assert '<option value="90" selected>90 days</option>' in page
    assert '<option value="never">Never</option>' in page
    rows = {
        name: row
        for row in page.split('<li class="list-row">')[1:]
        if (name := re.search(r"<span>([^<]+)", row)) and (name := name.group(1).strip())
    }
    assert "never expires" in rows["Forever"] and "never expires" in rows["Home"]
    assert "expires 9 Feb 2027, 12:00" in rows["Usual"] and "badge" not in rows["Usual"]
    # A token that expires within 7 days is marked, a 7-day one from the start.
    assert "Expires soon" in rows["Week"] and "expires 18 Nov 2026, 12:00" in rows["Week"]
    assert "Expired</span>" in rows["Old"] and "expired 10 Nov 2026, 12:00" in rows["Old"]


def test_a_token_holds_one_role_in_each_of_its_apps(server: Starlette) -> None:
    apps = server.state.apps
    database = apps.database
    apps.import_folder(APPS / "stock", "etienne", "cafe")
    etienne = database.person_by_login("etienne")
    shop, cafe = TokenLimit("etienne", "shop", "staff"), TokenLimit("etienne", "cafe", "owner")
    info, token = database.create_token(etienne.id, "Routine", NOW, [shop, cafe])
    # In the order of the apps' names, whatever the order they were given in.
    assert info.limits == (cafe, shop)
    assert named(database.token_access(token, NOW)) == (etienne, (cafe, shop), "Routine")
    with pytest.raises(ValueError, match="one role in each app"):
        database.create_token(
            etienne.id, "Two", NOW, [shop, TokenLimit("etienne", "shop", "owner")]
        )
    with pytest.raises(ValueError, match="at least one app"):
        database.create_token(etienne.id, "None", NOW, [])

    # A deleted app takes its limit away, and the token keeps the rest.
    apps.delete("etienne", "cafe")
    assert named(database.token_access(token, NOW)) == (etienne, (shop,), "Routine")
    # With its last app gone, it works nowhere, never everywhere, and the list says so.
    apps.delete("etienne", "shop")
    assert [one.limits for one in database.tokens(etienne.id)] == [()]
    assert database.token_access(token, NOW) is None
    with signed_in(server) as client:
        assert "Works nowhere: its apps were deleted" in client.get("/tokens").text


def test_a_token_works_only_where_its_person_may_go(server: Starlette) -> None:
    apps = server.state.apps
    etienne = apps.database.person_by_login("etienne")
    sam = apps.database.person_for(ISSUER, "auth0|sam", "sam@example.com", "sam", NOW)
    member(apps.database, sam, "etienne", "shop", "staff")

    def make(person: PersonInfo, app: str, role: str) -> str:
        """Why a token for the person's place `app`, as `role`, is refused."""

        async def main() -> None:
            (place,) = await person_tokens.named_places(apps, person, [app])
            limit = person_tokens.limit_for(place, role)
            await person_tokens.create_token(apps, person, "Routine", [limit])

        with pytest.raises((ValueError, Refusal)) as refused:
            anyio.run(main)
        return str(refused.value)

    # The owner holds every role the app declares, and only those.
    assert make(etienne, "etienne/shop", "boss") == (
        "You don't hold the role boss in etienne/shop. You hold owner and staff."
    )
    assert make(etienne, "etienne/nope", "staff") == (
        "There is no app etienne/nope for you. Call list_apps to see the apps you may use."
    )
    assert make(sam, "etienne/shop", "owner") == (
        "You don't hold the role owner in etienne/shop. You hold staff."
    )
    # The database checks a member's roles again, as they may have changed since.
    with pytest.raises(ValueError, match="You don't hold the role owner in etienne/shop"):
        apps.database.create_token(sam.id, "Routine", NOW, [TokenLimit("etienne", "shop", "owner")])
    assert apps.database.tokens(etienne.id) == apps.database.tokens(sam.id) == []
    shop = TokenLimit("etienne", "shop", "staff")
    made = anyio.run(person_tokens.create_token, apps, sam, "Routine", [shop])
    assert made[0].limits == (shop,)


def test_the_tokens_page_makes_a_token_for_several_apps(server: Starlette) -> None:
    server.state.apps.import_folder(APPS / "stock", "etienne", "cafe")
    with signed_in(server) as client:
        page = client.get("/tokens")
        made = client.post(
            "/tokens",
            data={
                "name": "Routine",
                "limit": ["", "etienne/shop/staff", "etienne/cafe/owner"],
                "csrf": csrf_for(server, client),
            },
        )

    # One field per app, each with its roles.
    assert '<label for="token-app-1">etienne/cafe</label>' in page.text
    assert '<label for="token-app-2">etienne/shop</label>' in page.text
    assert made.status_code == 200
    (token, *addresses) = TOKEN.findall(made.text) + ADDRESS.findall(made.text)
    etienne = server.state.apps.database.person_by_login("etienne")
    limits = (TokenLimit("etienne", "cafe", "owner"), TokenLimit("etienne", "shop", "staff"))
    assert named(server.state.apps.database.token_access(token, NOW)) == (
        etienne,
        limits,
        "Routine",
    )
    assert "Works only for etienne/cafe as owner and etienne/shop as staff" in made.text
    assert addresses == [
        "http://127.0.0.1:8800/apps/etienne/cafe/mcp",
        "http://127.0.0.1:8800/apps/etienne/shop/mcp",
    ]


def test_the_tokens_page_shows_a_new_token_once(server: Starlette) -> None:
    with TestClient(server) as anonymous:
        assert anonymous.get("/tokens", follow_redirects=False).status_code == 303
    with signed_in(server) as client:
        page = client.get("/tokens")
        assert 'action="/tokens"' in page.text and 'name="csrf"' in page.text
        expired = client.post("/tokens", data={"name": "home", "csrf": "x"})
        made = client.post(
            "/tokens",
            data={"name": "Laptop", "limit": "everywhere", "csrf": csrf_for(server, client)},
        )
        listed = client.get("/tokens")
        wrong = client.post(
            "/tokens", data={"name": "", "limit": "everywhere", "csrf": csrf_for(server, client)}
        )
        unchosen = client.post("/tokens", data={"name": "Phone", "csrf": csrf_for(server, client)})

    assert page.status_code == 200
    assert expired.status_code == 403
    assert made.status_code == 200
    assert made.headers["cache-control"] == "no-store"
    # A token for everywhere works on the person's endpoint, which serves the builder too.
    assert "request to http://127.0.0.1:8800/me/mcp." in " ".join(made.text.split())
    assert "builder" not in page.text
    (token,) = TOKEN.findall(made.text)
    person = server.state.apps.database.person_by_login("etienne")
    assert named(server.state.apps.database.token_access(token, NOW)) == (person, None, "Laptop")
    # Shown once: the list names it, with when it was made and last used, and never the token.
    assert token not in listed.text and "Laptop" in listed.text
    assert "never used" in listed.text
    # And with the id its apps see, so its person can match an app's records to it.
    (mine,) = server.state.apps.database.tokens(person.id)
    assert f"Apps see it as <code>{mine.public_id}</code>" in listed.text
    # Revoking asks first: what uses it stops at once.
    assert 'data-confirm="Revoke Laptop? Anything that uses it stops working at once."' in (
        listed.text
    )
    assert wrong.status_code == 400 and "A token&#39;s name is one line" in wrong.text
    # Where it works is chosen, never everywhere by default.
    assert '<option value="" selected>Not this app</option>' in page.text
    assert (
        '<input class="checkbox checkbox-sm" type="checkbox" name="limit" value="everywhere">'
        in (page.text)
    )
    assert unchosen.status_code == 400 and "Choose where the token works" in unchosen.text
    assert "Phone" not in [one.name for one in server.state.apps.database.tokens(person.id)]


def test_the_tokens_page_makes_a_token_limited_to_an_app(server: Starlette) -> None:
    database = server.state.apps.database
    with signed_in(server) as client:
        page = client.get("/tokens")
        made = client.post(
            "/tokens",
            data={
                "name": "Routine",
                "limit": "etienne/shop/staff",
                "csrf": csrf_for(server, client),
            },
        )
        listed = client.get("/tokens")
        unknown = client.post(
            "/tokens",
            data={"name": "Nope", "limit": "etienne/shop/boss", "csrf": csrf_for(server, client)},
        )
        both = client.post(
            "/tokens",
            data={
                "name": "Both",
                "limit": ["etienne/shop/staff", "etienne/shop/owner"],
                "csrf": csrf_for(server, client),
            },
        )
        wider = client.post(
            "/tokens",
            data={
                "name": "Wider",
                "limit": ["etienne/shop/staff", "everywhere"],
                "csrf": csrf_for(server, client),
            },
        )

    # Each app the person may use, with its roles and none chosen, then everywhere, last.
    assert '<label for="token-app-1">etienne/shop</label>' in page.text
    for role in ("owner", "staff"):
        assert f'<option value="etienne/shop/{role}">As {role}</option>' in page.text
    assert page.text.index("etienne/shop/staff") < page.text.index('value="everywhere"')
    assert 'value="everywhere" checked' not in page.text
    assert made.status_code == 200
    (token,) = TOKEN.findall(made.text)
    etienne = database.person_by_login("etienne")
    limit = TokenLimit("etienne", "shop", "staff")
    assert named(database.token_access(token, NOW)) == (etienne, (limit,), "Routine")
    assert "Works only for etienne/shop as staff" in made.text
    assert "http://127.0.0.1:8800/apps/etienne/shop/mcp" in made.text
    # The list says where each token works.
    assert "Works only for etienne/shop as staff" in listed.text
    assert unknown.status_code == 400 and "Choose where the token works" in unknown.text
    # One role in each app, and never an app and everywhere at once.
    assert both.status_code == 400 and "one role in each app it works for" in both.text
    assert wider.status_code == 400 and "not both" in wider.text
    assert [one.name for one in database.tokens(etienne.id)] == ["Routine"]


def test_an_app_s_link_asks_for_one_of_its_roles(server: Starlette) -> None:
    sam = database_member(server, "staff")
    with signed_in(server) as client:
        owner = client.get("/tokens?app=etienne/shop").text
        missing = client.get("/tokens?app=etienne/nope").text
    with TestClient(server) as client:
        act_as(server, client, sam)
        member = client.get("/tokens?app=etienne/shop").text

    # The owner holds both roles: the page asks which, and offers nothing else.
    assert '<option value="" disabled selected>Choose a role</option>' in owner
    assert 'value="everywhere"' not in owner
    assert 'name="only" value="etienne/shop"' in owner
    # sam holds one: it is chosen already.
    assert '<option value="etienne/shop/staff" selected>' in member
    assert "etienne/shop/owner" not in member and "Choose a role" not in member
    # A role left unchosen from an app's page is refused, and so is everywhere.
    with signed_in(server) as client:
        refused = [
            client.post(
                "/tokens",
                data={
                    "name": "x",
                    "limit": limit,
                    "only": "etienne/shop",
                    "csrf": csrf_for(server, client),
                },
            )
            for limit in ("", "everywhere", ["etienne/shop/staff", "everywhere"])
        ]
        made = client.post(
            "/tokens",
            data={
                "name": "Routine",
                "limit": "etienne/shop/staff",
                "only": "etienne/shop",
                "csrf": csrf_for(server, client),
            },
        )
    for unchosen in refused:
        assert unchosen.status_code == 400 and "Choose where the token works" in unchosen.text
    # Made from an app's page, the token shows above that app's form, as it was.
    assert made.status_code == 200 and "Your new token, Routine" in made.text
    assert 'name="only" value="etienne/shop"' in made.text and 'value="everywhere"' not in made.text
    assert 'aria-label="The address of etienne/shop"' in made.text
    database = server.state.apps.database
    assert [one.name for one in database.tokens(database.person_by_login("etienne").id)] == [
        "Routine"
    ]
    # A fixed sentence, never the address's own words.
    assert "You can&#39;t make a token for that app" in missing
    assert "etienne/nope" not in missing


def test_a_link_fills_the_tokens_form_in(server: Starlette) -> None:
    database = server.state.apps.database
    server.state.apps.import_folder(APPS / "stock", "etienne", "cafe")
    limits = [TokenLimit("etienne", "shop", "staff"), TokenLimit("etienne", "cafe", "owner")]
    link = filled_tokens_link("Till import", limits, 30)
    with signed_in(server) as client:
        page = client.get(link).text

    assert link == (
        "/tokens?name=Till+import&limit=etienne/shop/staff&limit=etienne/cafe/owner&days=30"
    )
    assert '<option value="30" selected>30 days</option>' in page
    assert '<option value="90">90 days</option>' in page
    assert 'placeholder="Laptop at home" value="Till import">' in page
    assert '<option value="etienne/shop/staff" selected>As staff</option>' in page
    assert '<option value="etienne/cafe/owner" selected>As owner</option>' in page
    assert '<option value="etienne/shop/owner">As owner</option>' in page
    # Each app has its role chosen, and nothing is made until the person sends the form.
    assert '<option value="" selected>Not this app</option>' not in page
    assert 'value="everywhere" checked' not in page
    assert database.tokens(database.person_by_login("etienne").id) == []


def database_member(server: Starlette, role: str):
    """sam, who holds `role` in etienne/shop."""
    database = server.state.apps.database
    sam = database.person_for(ISSUER, "auth0|sam", "sam@example.com", "sam", NOW)
    return member(database, sam, "etienne", "shop", role)


def test_a_token_is_revoked_from_the_page(server: Starlette) -> None:
    database = server.state.apps.database
    owner = database.person_by_login("etienne")
    sam = database.person_for(ISSUER, "auth0|sam", "sam@example.com", "sam", NOW)
    mine, token = database.create_token(owner.id, "home", NOW)
    with TestClient(server) as client:
        act_as(server, client, sam)
        theirs = client.post(
            f"/tokens/{mine.id}/revoke",
            data={"csrf": csrf_for(server, client)},
            follow_redirects=True,
        )
    assert "There is no such token." in theirs.text
    assert theirs.url.query == b"notice=missing"
    assert named(database.token_access(token, NOW)) == (owner, None, "home")
    with signed_in(server) as client:
        expired = client.post(f"/tokens/{mine.id}/revoke", data={"csrf": "x"})
        revoked = client.post(
            f"/tokens/{mine.id}/revoke",
            data={"csrf": csrf_for(server, client)},
            follow_redirects=True,
        )
    assert expired.status_code == 403
    assert "Token revoked. It no longer works." in revoked.text
    assert database.token_access(token, NOW) is None


def test_a_token_made_by_a_token_names_it_and_goes_with_it(server: Starlette, address: str) -> None:
    database = server.state.apps.database
    etienne = database.person_by_login("etienne")
    builder, _ = database.create_token(etienne.id, "Builder", NOW)
    staff = TokenLimit("etienne", "shop", "staff")
    worker, token = database.create_token(etienne.id, "Worker", NOW, [staff], builder.public_id)
    assert worker.made_by == "Builder"
    assert [(one.name, one.made_by) for one in database.tokens(etienne.id)] == [
        ("Worker", "Builder"),
        ("Builder", None),
    ]
    # A token made by a token works only for the apps it is limited to.
    with pytest.raises(ValueError, match="works only for the apps you choose"):
        database.create_token(etienne.id, "Everywhere", NOW, None, builder.public_id)
    # A limited token makes none, so no token is made by one that was made by a token.
    with pytest.raises(ValueError, match="A token limited to apps makes no token"):
        database.create_token(etienne.id, "Grandchild", NOW, [staff], worker.public_id)

    assert app_status(address, token) not in (401, 403)
    with signed_in(server) as client:
        page = client.get("/tokens").text
        assert "Made by Builder" in page
        assert "Revoke Builder and the token it made? Anything that uses them" in page
        assert "Revoke Worker? Anything that uses it stops working at once." in page
        revoked = client.post(
            f"/tokens/{builder.id}/revoke",
            data={"csrf": csrf_for(server, client)},
            follow_redirects=True,
        )
    assert "Token revoked. It no longer works." in revoked.text
    # The token it made stops at its very next request.
    assert app_status(address, token) == 401
    assert database.tokens(etienne.id) == []
    # One made while its maker is revoked never outlives it.
    with pytest.raises(ValueError, match="The token you are using was revoked"):
        database.create_token(etienne.id, "Late", NOW, [staff], builder.public_id)
    assert database.tokens(etienne.id) == []


def test_a_token_is_its_person_under_the_development_login(server, address) -> None:
    database = server.state.apps.database
    sam = database.person_for(ISSUER, "auth0|sam", "sam@example.com", "sam", NOW)
    member(database, sam, "etienne", "shop", "staff")
    token = app_token(server, "staff", person=sam)
    # The development login would be etienne, who holds every role: the token is sam, as staff.
    with TestClient(server) as client:
        signed = client.post(
            "/apps/etienne/shop/mcp",
            json={},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    assert signed.status_code not in (401, 403)
    (key,) = server.state.registry._servers
    assert key[:4] == ("etienne", "shop", "staff", "sam")
    assert key[4] is not None and key[4].name == "shop as staff 0"
    wrong = httpx.post(
        f"{address}/apps/etienne/shop/mcp", headers={"Authorization": "Bearer cliquet_x"}
    )
    assert wrong.status_code == 401


def test_the_tokens_page_lists_the_owner_s_roles_without_a_start(
    server: Starlette, monkeypatch: pytest.MonkeyPatch
) -> None:
    apps = server.state.apps
    apps.forget("etienne", "shop")
    asked: list[str] = []

    async def started(login: str, name: str, wait: float | None = None) -> None:
        asked.append(f"{login}/{name}")
        raise SandboxError("the sandbox can't be reached")

    monkeypatch.setattr(apps, "started", started)
    with signed_in(server) as client:
        page = client.get("/tokens")

    # The roles were kept at publish: the page reads them, and starts no app.
    for role in ("owner", "staff"):
        assert f'<option value="etienne/shop/{role}">As {role}</option>' in page.text
    assert asked == []
