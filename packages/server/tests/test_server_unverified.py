"""A person whose email isn't verified yet: they get in with no email, claim nothing by one,
share no app and make no token, and have a few days to verify it before Cliquet waits for
them to."""

import contextlib
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import anyio
import httpx
import pytest
from fastmcp import Client
from starlette.applications import Starlette
from starlette.testclient import TestClient

from cliquet_server.app_auth import CHOOSE_LOGIN
from cliquet_server.db import AccountDeleted, Database, PersonInfo, Right, TokenLimit, may
from cliquet_server.executor import LocalExecutor
from cliquet_server.limiter import Limits
from cliquet_server.me import LIST
from cliquet_server.sign_in import (
    EMAIL_ELSEWHERE,
    email_taken,
    email_waiting,
    joined,
    locked_out,
    reminder,
    verify_first,
)

from .server_helpers import (
    APPS,
    AUTH0_ISSUER,
    ME,
    NOW,
    act_as,
    choose_login,
    csrf_for,
    login_form,
    oidc_callback,
    oidc_server,
    rpc_refusal,
    running,
    text,
    texts,
    unexpected,
)

# What a tool's answer passes on to the person.
TELL = "Tell the person, from Cliquet: "


class Clock:
    """The server's clock, which a test moves on."""

    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def server(tmp_path: Path, executor: LocalExecutor, clock: Clock) -> Starlette:
    """The server signing in through a faked provider shaped as Auth0, whose tokens carry no
    email, with etienne's shop, and its clock at `clock`."""
    server = oidc_server(tmp_path, executor, clock=clock)
    database = server.state.apps.database
    database.person_for(AUTH0_ISSUER, "auth0|owner", "etienne@example.com", "etienne", NOW)
    server.state.apps.import_folder(APPS / "stock", "etienne", "shop")
    return server


def lee(server: Starlette, *, builds: bool = False) -> PersonInfo:
    """lee, who chose their login with an email the provider hasn't verified: with the right
    to build, and their own published shop, when `builds`."""
    apps = server.state.apps
    person = apps.database.person_for(AUTH0_ISSUER, "auth0|lee", None, "lee", NOW)
    if builds:
        person = apps.database.grant("lee", Right.BUILD)
        apps.import_folder(APPS / "stock", "lee", "shop")
    return person


def verify(server: Starlette, who: str = "lee") -> None:
    """The provider verifies `who`'s email."""
    provider = server.state.fake_provider
    subject, email, _ = provider.people[who]
    provider.people[who] = (subject, email, True)


def userinfo_reads(server: Starlette) -> int:
    """How many times the server read the faked provider's userinfo endpoint."""
    provider = server.state.fake_provider
    endpoint = provider.document["userinfo_endpoint"]
    return sum(str(sent.url) == endpoint for sent in provider.requests)


async def answers(address: str, token: str, *calls: tuple[str, dict]) -> list:
    """Each call's answer on /me/mcp with `token`, a refusal too."""
    async with Client(f"{address}/me/mcp", auth=token) as client:
        return [await client.call_tool(name, args, raise_on_error=False) for name, args in calls]


def told(answer) -> list[str]:
    """What an answer passes on to the person."""
    return [one.removeprefix(TELL) for one in texts(answer) if one.startswith(TELL)]


def test_a_newcomer_with_no_verified_email_chooses_a_login_and_claims_nothing(
    server: Starlette,
) -> None:
    apps, database = server.state.apps, server.state.apps.database
    database.invite("etienne", "shop", "lee@example.com", "staff", NOW)
    token = server.state.fake_provider.token("lee")
    with running(server) as address:
        chosen, listed, again = anyio.run(
            answers, address, token, (CHOOSE_LOGIN, {"login": "lee"}), (LIST, {}), (LIST, {})
        )
    settings = apps.settings
    assert text(chosen).endswith(TELL + reminder(settings, 7))
    assert "Shares wait" not in text(chosen)
    # Their tools work, and they are reminded once a day only.
    assert not listed.is_error and listed.structured_content["waiting"] == []
    assert told(listed) == told(again) == []

    person = database.person_by_login("lee")
    assert person is not None and person.email is None and not person.email_verified
    # The invitation waits for the email, and so does a share made now.
    assert database.share("etienne", "shop", "lee@example.com", "owner", NOW) == "shared"
    waiting = database.pending_invitations("etienne", "shop")
    assert {(one.email, one.role) for one in waiting} == {
        ("lee@example.com", "staff"),
        ("lee@example.com", "owner"),
    }
    assert database.pending_shares(person.id) == []


def test_a_later_token_that_says_the_email_is_verified_claims_what_waits(
    server: Starlette,
) -> None:
    database = server.state.apps.database
    person = lee(server)
    database.invite("etienne", "shop", "lee@example.com", "staff", NOW)
    provider = server.state.fake_provider
    verified = provider.token("lee", email="Lee@Example.com", email_verified=True)
    with running(server) as address:
        (listed,) = anyio.run(answers, address, verified, (LIST, {}))
    assert listed.structured_content["waiting"] == [
        {"app": "etienne/shop", "owner": "etienne", "role": "staff"}
    ]
    now = database.person_by_id(person.id)
    assert now is not None and now.email == "lee@example.com" and now.email_verified
    assert database.pending_invitations("etienne", "shop") == []


def test_userinfo_is_read_again_at_most_once_an_hour(server: Starlette, clock: Clock) -> None:
    database = server.state.apps.database
    person = lee(server)
    database.invite("etienne", "shop", "lee@example.com", "staff", NOW)
    token = server.state.fake_provider.token("lee")
    with running(server) as address:

        def listed() -> dict:
            (answer,) = anyio.run(answers, address, token, (LIST, {}))
            assert not answer.is_error, answer.content
            return answer.structured_content

        assert listed()["waiting"] == []
        reads = userinfo_reads(server)
        assert reads == 1
        verify(server)
        clock.now = NOW + timedelta(minutes=59)
        assert listed()["waiting"] == []
        assert userinfo_reads(server) == reads
        clock.now = NOW + timedelta(minutes=61)
        assert listed()["waiting"] == [{"app": "etienne/shop", "owner": "etienne", "role": "staff"}]
        assert userinfo_reads(server) == reads + 1
        # Verified, the person is never asked about again.
        clock.now = NOW + timedelta(hours=3)
        listed()
        assert userinfo_reads(server) == reads + 1
    assert database.person_by_id(person.id).email == "lee@example.com"


def test_an_email_another_account_has_leaves_the_account_with_none(
    server: Starlette, clock: Clock
) -> None:
    database = server.state.apps.database
    # They signed in with etienne's email, unverified, and chose their own login.
    kit = database.person_for(AUTH0_ISSUER, "auth0|unverified", None, "kit", NOW)
    provider = server.state.fake_provider
    token = provider.token("unverified", email="etienne@example.com", email_verified=True)
    with running(server) as address:
        first, second = anyio.run(answers, address, token, (LIST, {}), (LIST, {}))
        # Never locked out: the provider verified their email.
        clock.now = NOW + timedelta(days=30)
        (later,) = anyio.run(answers, address, token, (LIST, {}))
    assert not first.is_error and told(first) == [email_taken("etienne@example.com")]
    assert told(second) == [] and not later.is_error
    now = database.person_by_id(kit.id)
    assert now is not None and now.email is None and now.email_verified
    assert database.person_by_login("etienne").email == "etienne@example.com"


def test_an_unverified_person_shares_nothing_and_makes_no_token(server: Starlette) -> None:
    person = lee(server, builds=True)
    settings = server.state.apps.settings
    token = server.state.fake_provider.token("lee")
    shared = {"app": "shop", "email": "sam@example.com", "role": "staff"}
    made = {"name": "Till", "apps": [{"app": "shop", "role": "staff"}]}
    with running(server) as address:
        share, token_made = anyio.run(
            answers, address, token, ("share_app", shared), ("create_token", made)
        )
    assert share.is_error and text(share) == verify_first(settings, "share an app")
    assert token_made.is_error and text(token_made) == verify_first(settings, "make an API token")

    with TestClient(server) as client:
        act_as(server, client, person)
        csrf = csrf_for(server, client)
        form = {"email": "sam@example.com", "role": "staff", "csrf": csrf}
        page = client.post("/manage/lee/shop/share", data=form)
        assert "you can&#39;t share an app" in page.text
        page = client.post(
            "/tokens", data={"name": "Till", "limit": "lee/shop/staff", "csrf": csrf}
        )
        assert page.status_code == 400 and "you can&#39;t make an API token" in page.text
    database = server.state.apps.database
    assert database.pending_invitations("lee", "shop") == [] and database.tokens(person.id) == []


def test_the_days_left_are_told_once_a_day(server: Starlette, clock: Clock) -> None:
    lee(server)
    settings = server.state.apps.settings
    token = server.state.fake_provider.token("lee")
    with running(server) as address:
        first, second = anyio.run(answers, address, token, (LIST, {}), (LIST, {}))
        clock.now = NOW + timedelta(days=6, hours=1)
        (later,) = anyio.run(answers, address, token, (LIST, {}))
    assert told(first) == [reminder(settings, 7)] and told(second) == []
    assert told(later) == [reminder(settings, 1)]
    assert "1 day left" in reminder(settings, 1)

    # The console says it on every page.
    with TestClient(server) as client:
        act_as(server, client, server.state.apps.database.person_by_login("lee"))
        assert "you have 1 day left to verify it" in client.get("/").text


def test_after_the_days_to_verify_cliquet_waits_for_the_email(
    server: Starlette, clock: Clock
) -> None:
    apps, database = server.state.apps, server.state.apps.database
    person = lee(server, builds=True)
    database.invite("etienne", "shop", "lee@example.com", "staff", NOW)
    # A token the operator made them, at the app's address.
    secret = database.create_token(person.id, "till", NOW, [TokenLimit("lee", "shop", "staff")])[1]
    clock.now = NOW + timedelta(days=7)
    refusal = locked_out(apps.settings, person, clock.now)
    assert refusal is not None and "the 7 days to verify it are over" in refusal
    token = server.state.fake_provider.token("lee")
    with running(server) as address:

        async def main() -> None:
            async with Client(f"{address}/me/mcp", auth=token) as client:
                # Their tools are still listed, the builder's too, and each refuses them.
                names = {tool.name for tool in await client.list_tools()}
                assert {LIST, "create_app"} <= names
                for name, args in ((LIST, {}), ("create_app", {"app": "notes"})):
                    refused = await client.call_tool(name, args, raise_on_error=False)
                    assert refused.is_error and text(refused) == refusal

        anyio.run(main)
        headers = {"Authorization": f"Bearer {secret}", "Accept": "application/json"}
        at_app = httpx.post(f"{address}/apps/lee/shop/mcp", json={}, headers=headers)
        assert rpc_refusal(at_app, 403) == refusal

    with TestClient(server) as client:
        act_as(server, client, person)
        for path in ("/", "/tokens", "/apps/lee/shop", "/manage/lee/shop"):
            page = client.get(path)
            assert page.status_code == 403, path
            assert refusal.replace("'", "&#39;") in page.text and 'href="/login"' in page.text
        called = client.post("/apps/lee/shop/staff/rpc", json={"tool": "stock"})
        assert called.status_code == 403 and called.json() == {"error": refusal}

        # A sign-in that reads their verified email lets them in again, and claims what
        # waits for it.
        verify(server)
        assert oidc_callback(server, client, "lee") == "/"
        home = client.get("/")
        assert home.status_code == 200 and "etienne/shop" in home.text
    assert database.person_by_login("lee").email == "lee@example.com"


def test_the_web_signs_in_a_person_whose_email_isn_t_verified(server: Starlette) -> None:
    database = server.state.apps.database
    with TestClient(server) as client:
        assert oidc_callback(server, client, "email-less") == "/auth/choose-login"
        csrf, _ = login_form(client)
        assert choose_login(client, "nell", csrf).status_code == 303
        home = client.get("/")
        assert home.status_code == 200 and "you have 7 days left to verify it" in home.text
    nell = database.person_by_login("nell")
    assert nell is not None and nell.email is None and not nell.email_verified


def test_a_locked_out_person_signs_out_downloads_their_apps_and_deletes_their_account(
    server: Starlette, clock: Clock
) -> None:
    person = lee(server, builds=True)
    clock.now = NOW + timedelta(days=7)
    refusal = locked_out(server.state.apps.settings, person, clock.now)
    assert refusal is not None
    with TestClient(server) as client:
        act_as(server, client, person)
        csrf = csrf_for(server, client)
        page = client.get("/")
        assert page.status_code == 403
        assert 'action="/logout"' in page.text and f'value="{csrf}"' in page.text
        assert 'href="/account"' in page.text

        # Their account page, its deletion and their apps' downloads let them through, and
        # only those.
        account = client.get("/account")
        assert account.status_code == 200
        assert refusal.replace("'", "&#39;") in account.text
        assert 'href="/manage/lee/shop/download"' in account.text
        assert 'action="/account/delete"' in account.text
        download = client.get("/manage/lee/shop/download")
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/zip"
        assert client.get("/manage/lee/shop/history").status_code == 403

        # A draft's screen gets JSON, as a published app's does.
        called = client.post("/build/shop/preview/staff/rpc", json={"tool": "stock"})
        assert called.status_code == 403 and called.json() == {"error": refusal}

        signed_out = client.post("/logout", data={"csrf": csrf}, follow_redirects=False)
        assert signed_out.status_code in {302, 303}
        assert 'session=""' in signed_out.headers["set-cookie"]

        act_as(server, client, person)
        csrf = csrf_for(server, client)
        deleted = client.post("/account/delete", data={"csrf": csrf, "login": "lee"})
        assert deleted.status_code == 200 and 'session=""' in deleted.headers["set-cookie"]
    database = server.state.apps.database
    assert database.person_by_login("lee") is None and database.apps("lee") == []


def test_a_locked_out_person_s_calls_count_against_the_limits(
    tmp_path: Path, executor: LocalExecutor, clock: Clock
) -> None:
    limits = Limits(calls_at_once=8, calls_per_minute=3, sandbox_runs_per_hour=300)
    server = oidc_server(tmp_path, executor, limits=limits, clock=clock)
    person = lee(server)
    clock.now = NOW + timedelta(days=7)
    refusal = locked_out(server.state.apps.settings, person, clock.now)
    token = server.state.fake_provider.token("lee")
    with running(server) as address:
        refused = anyio.run(answers, address, token, *[(LIST, {})] * 4)
    assert [text(answer) for answer in refused[:3]] == [refusal] * 3
    assert text(refused[3]).startswith("Too many calls to your apps: at most 3 a minute.")


def test_a_newcomer_whose_verified_email_is_another_s_is_told_what_to_do(
    server: Starlette,
) -> None:
    provider = server.state.fake_provider
    # etienne has this email, verified, with another identity.
    provider.people["twin"] = ("auth0|twin", "etienne@example.com", True)
    token = provider.token("twin", email="etienne@example.com", email_verified=True)
    with running(server) as address:
        (refused,) = anyio.run(answers, address, token, (LIST, {}))
    assert refused.is_error and text(refused) == EMAIL_ELSEWHERE
    assert "etienne@example.com" not in EMAIL_ELSEWHERE

    with TestClient(server) as client:
        start = client.get("/login", follow_redirects=False)
        back = provider.authorize(start.headers["location"], "twin")
        page = client.get(back, follow_redirects=False)
        assert page.status_code == 403
        assert EMAIL_ELSEWHERE.replace("'", "&#39;") in page.text
    assert server.state.apps.database.person_by_identity(AUTH0_ISSUER, "auth0|twin") is None


def test_an_unverified_answer_of_userinfo_reads_the_database_once(
    server: Starlette, monkeypatch: pytest.MonkeyPatch
) -> None:
    lee(server)
    database = server.state.apps.database
    reads = []
    looked_up = database.person_by_identity

    def counted(issuer: str, subject: str) -> PersonInfo | None:
        reads.append(subject)
        return looked_up(issuer, subject)

    monkeypatch.setattr(database, "person_by_identity", counted)
    app_auth = server.state.app_auth
    token = server.state.fake_provider.token("lee")
    with TestClient(server):
        access = anyio.run(app_auth.access, token, ME)
    assert access is not None and access.person is not None and access.person.login == "lee"
    # The userinfo endpoint was read, and said the email isn't verified.
    assert userinfo_reads(server) == 1
    assert reads == ["auth0|lee"]


def operator_sql(database: Database, statement: str, *values: str) -> list[str]:
    """Run one of the statements the self-hosting guide gives the operator, on the server's
    database, and the first column of what it selects."""
    path = database._engine.url.database
    assert path is not None
    with contextlib.closing(sqlite3.connect(path)) as raw, raw:
        return [row[0] for row in raw.execute(statement, values)]


# The operator's statements as the self-hosting guide gives them: the people with no email,
# then the move to another provider.
NO_EMAIL = "SELECT login FROM people WHERE deleted_at IS NULL AND email IS NULL"
MOVE = (
    "UPDATE people SET issuer = NULL, subject = NULL "
    "WHERE deleted_at IS NULL AND issuer IS NOT NULL AND issuer != ?"
)


def test_a_newcomer_who_verifies_an_imported_owner_s_email_becomes_them(
    server: Starlette, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="cliquet_server.accounts")
    apps, database = server.state.apps, server.state.apps.database
    apps.import_folder(APPS / "stock", "pat", "shop", email="pat@example.com")
    pat = database.person_by_login("pat")
    assert pat is not None
    provider = server.state.fake_provider
    # Their assistant reads no verified email at first, so they choose a login of their own.
    provider.people["pat"] = ("auth0|pat", "pat@example.com", False)
    token = provider.token("pat")
    with running(server) as address:
        (chosen,) = anyio.run(answers, address, token, (CHOOSE_LOGIN, {"login": "pat2"}))
        assert not chosen.is_error, chosen.content
        pat2 = database.person_by_login("pat2")
        assert pat2 is not None and pat2.email is None
        verify(server, "pat")
        clock.now = NOW + timedelta(minutes=61)
        listed, again = anyio.run(answers, address, token, (LIST, {}), (LIST, {}))
    # From that request on, they are pat, with pat's apps.
    assert told(listed) == [joined("pat", "pat2")] and told(again) == []
    assert "pat/shop" in text(listed)
    now = database.person_by_identity(AUTH0_ISSUER, "auth0|pat")
    assert now is not None and now.id == pat.id and now.login == "pat"
    assert now.email == "pat@example.com" and now.email_verified
    # pat2 is closed, as a deleted account is, and its login stays reserved.
    retired = database.account("pat2")
    assert retired is not None and retired.deleted_at is not None and retired.subject is None
    assert "deleted the account pat2" in caplog.messages and unexpected(caplog) == []
    with pytest.raises(AccountDeleted):
        database.person_for(AUTH0_ISSUER, "auth0|someone", None, "pat2", NOW)
    with TestClient(server) as client:
        act_as(server, client, pat2)
        assert client.get("/", follow_redirects=False).headers["location"] == "/login?next=%2F"


def test_a_cleared_identity_is_claimed_once_its_newcomer_verifies_the_email(
    server: Starlette,
) -> None:
    apps, database = server.state.apps, server.state.apps.database
    # pat signed in through the old provider, and owns a shop.
    database.person_for("https://old.example/", "old|pat", "pat@example.com", "pat", NOW)
    apps.import_folder(APPS / "stock", "pat", "shop")
    assert operator_sql(database, NO_EMAIL) == []
    operator_sql(database, MOVE, AUTH0_ISSUER)
    provider = server.state.fake_provider
    provider.people["pat"] = ("auth0|pat", "pat@example.com", False)
    with TestClient(server) as client:
        assert oidc_callback(server, client, "pat") == "/auth/choose-login"
        csrf, _ = login_form(client)
        assert choose_login(client, "pat2", csrf).status_code == 303
        pat2 = database.grant("pat2", Right.BUILD)
        assert operator_sql(database, NO_EMAIL) == ["pat2"]

        verify(server, "pat")
        assert oidc_callback(server, client, "pat") == "/"
        home = client.get("/")
        assert joined("pat", "pat2") in home.text and "pat/shop" in home.text
    # The session that named pat2 finds nobody.
    with TestClient(server) as client:
        act_as(server, client, pat2)
        assert client.get("/", follow_redirects=False).headers["location"] == "/login?next=%2F"
    pat = database.person_by_identity(AUTH0_ISSUER, "auth0|pat")
    assert pat is not None and pat.login == "pat" and may(pat, Right.BUILD)
    assert database.person_by_login("pat2") is None
    assert operator_sql(database, NO_EMAIL) == []


def test_a_newcomer_whose_account_holds_something_stays_apart(server: Starlette) -> None:
    apps, database = server.state.apps, server.state.apps.database
    apps.import_folder(APPS / "stock", "pat", "shop", email="pat@example.com")
    pat2 = database.person_for(AUTH0_ISSUER, "auth0|pat", None, "pat2", NOW)
    # A draft counts as an app.
    database.create_app("pat2", "notes", NOW)
    provider = server.state.fake_provider
    provider.people["pat"] = ("auth0|pat", "pat@example.com", True)
    token = provider.token("pat")
    with running(server) as address:
        first, second = anyio.run(answers, address, token, (LIST, {}), (LIST, {}))
    assert told(first) == [email_waiting("pat@example.com")] and told(second) == []
    assert "To join them, ask the server's operator." in email_waiting("pat@example.com")
    kept = database.person_by_id(pat2.id)
    assert kept is not None and kept.login == "pat2"
    assert kept.email is None and kept.email_verified
    waiting = database.person_by_login("pat")
    assert waiting is not None and waiting.subject is None
    assert waiting.email == "pat@example.com"
