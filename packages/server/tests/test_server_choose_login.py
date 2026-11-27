"""A person chooses their login at first sign-in, apart from their email: on the web, or in
their assistant."""

import re
from pathlib import Path

import anyio
import mcp_types as types
import pytest
from fastmcp import Client
from fastmcp.client.messages import MessageHandler
from starlette.applications import Starlette
from starlette.testclient import TestClient

from cliquet_server.app_auth import CHOOSE_FIRST, CHOOSE_LOGIN
from cliquet_server.db import Right, may
from cliquet_server.executor import LocalExecutor
from cliquet_server.identity import WORDS, email_login, login_problem, login_suggestions
from cliquet_server.limiter import Limits
from cliquet_server.me import (
    ACCEPT,
    ASK_FILE,
    DECLINE,
    DESCRIBE,
    LIST,
    READ,
    SHOW,
    UPLOAD_FILE,
    WRITE,
)

from .server_helpers import (
    APPS,
    AUTH0_ISSUER,
    NOW,
    PEOPLE,
    app_client,
    choose_login,
    login_form,
    oidc_callback,
    oidc_server,
    running,
    text,
)

# The suggestion's shape: one or two of the words, then a short key.
SUGGESTION = re.compile(r"^([a-z]+)(?:-([a-z]+))?-([a-z0-9]{3})$")
# The person's tools, the same for everyone.
PERSON_TOOLS = {LIST, DESCRIBE, READ, WRITE, SHOW, ACCEPT, DECLINE, ASK_FILE, UPLOAD_FILE}


@pytest.fixture
def server(tmp_path: Path, executor: LocalExecutor) -> Starlette:
    """The server signing in through a faked provider shaped as Auth0."""
    return oidc_server(tmp_path, executor)


def test_two_people_with_the_same_local_part_both_choose_a_login(server: Starlette) -> None:
    database = server.state.apps.database
    with TestClient(server) as first:
        assert oidc_callback(server, first, "sam-a", "/tokens") == "/auth/choose-login"
        csrf, suggested = login_form(first)
        chosen = choose_login(first, suggested, csrf)
        assert chosen.status_code == 303
        assert chosen.headers["location"] == "/tokens"
        assert first.get("/tokens").status_code == 200
    with TestClient(server) as second:
        assert oidc_callback(server, second, "sam-b") == "/auth/choose-login"
        csrf, offered = login_form(second)
        # The local part is taken now, so the second is offered it with a key, the same one
        # at each load, and in place of the taken one they type.
        assert re.fullmatch(r"sam-[a-z0-9]{3}", offered), offered
        assert login_form(second)[1] == offered
        refused = choose_login(second, "sam", csrf)
        assert refused.status_code == 400 and f'value="{offered}"' in refused.text
        assert choose_login(second, offered, csrf).status_code == 303
        assert offered in second.get("/").text

    a, b = database.person_by_email("sam@a.example"), database.person_by_email("sam@b.example")
    assert a is not None and b is not None
    assert suggested == a.login == "sam" and b.login == offered

    # Both use Cliquet from an assistant too.
    with running(server) as address:

        async def main() -> None:
            for who in ("sam-a", "sam-b"):
                token = server.state.fake_provider.token(who)
                async with app_client(address, "/me/mcp", token) as client:
                    listed = await client.call_tool(LIST, {})
                    assert not listed.is_error, listed.content

        anyio.run(main)


@pytest.mark.parametrize(
    ("email", "login"),
    [
        ("sam@example.com", "sam"),
        ("Sam.Smith+tea@Example.com", "sam-smith"),
        ("étienne_corbillé@example.fr", "etienne-corbille"),
        ("--sam..lee@example.com", "sam-lee"),
        ("Søren.Straße@example.dk", "soren-strasse"),
        (f"{'a' * 60}@example.com", "a" * 35),
    ],
)
def test_the_suggestion_is_the_email_s_local_part(email: str, login: str) -> None:
    assert email_login(email) == login
    offered = login_suggestions(email)
    assert next(offered) == login
    keyed = next(offered)
    assert keyed.startswith(f"{login}-") and login_problem(keyed) is None


@pytest.mark.parametrize("email", [None, "", "+tag@example.com", "日本@example.com"])
def test_without_a_local_part_the_suggestion_is_random_words_and_a_key(
    email: str | None,
) -> None:
    for _ in range(200):
        login = next(login_suggestions(email))
        match = SUGGESTION.fullmatch(login)
        assert match is not None, login
        assert match[1] in WORDS and (match[2] is None or match[2] in WORDS)
        assert login_problem(login) is None


def test_a_local_part_that_can_t_be_a_login_takes_a_key() -> None:
    # Too short, it takes a key; one of Cliquet's own names, or app-, gives words instead.
    assert re.fullmatch(r"jo-[a-z0-9]{3}", next(login_suggestions("jo@example.com")))
    for email in ("admin@example.com", "cliquet@example.com", "app@example.com"):
        assert SUGGESTION.fullmatch(next(login_suggestions(email))), email


def test_the_page_offers_a_free_suggestion(
    server: Starlette, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The page offers the person's local part, from their verified email.
    with TestClient(server) as client:
        oidc_callback(server, client, "sam-a")
        assert login_form(client)[1] == "sam"
    # A taken suggestion is passed over.
    server.state.apps.database.person_for(
        AUTH0_ISSUER, "auth0|x", "x@example.com", "calm-fox-2a3", NOW
    )
    offered = ["calm-fox-2a3", "quiet-otter-4k2"]
    monkeypatch.setattr("cliquet_server.sign_in.login_suggestions", lambda *_: iter(offered))
    with TestClient(server) as client:
        oidc_callback(server, client, "sam-a")
        assert login_form(client)[1] == "quiet-otter-4k2"


def test_a_wrong_login_is_refused_with_what_to_write(server: Starlette) -> None:
    server.state.apps.database.person_for(AUTH0_ISSUER, "auth0|x", "x@example.com", "taken", NOW)
    with TestClient(server) as client:
        oidc_callback(server, client, "sam-a")
        csrf, _ = login_form(client)
        for login, sentence in (
            ("taken", "Someone already has the login 'taken'. Choose another one."),
            ("me", "'me' is kept for Cliquet's own pages. Choose another login."),
            ("tokens", "'tokens' is kept for Cliquet's own pages. Choose another login."),
            ("app-shop", "'app-shop' is kept for Cliquet's own pages. Choose another login."),
            ("sam smith", "Use lower-case letters, digits and single hyphens"),
            ("app_shop", "Use lower-case letters, digits and single hyphens"),
            ("-sam", "with no hyphen at either end"),
            ("ab", "A login is 3 to 39 characters long"),
        ):
            refused = choose_login(client, login, csrf)
            assert refused.status_code == 400, login
            assert sentence.replace("'", "&#39;") in refused.text, login
            assert "session" not in refused.headers.get("set-cookie", "")
        # What they wrote stays in the field, lower case.
        assert 'value="sam smith"' in choose_login(client, "Sam Smith", csrf).text
        # Without the form's token, nothing is made.
        assert choose_login(client, "fine-login", "wrong").status_code == 403
    assert server.state.apps.database.person_by_email("sam@a.example") is None


def test_the_login_page_needs_a_sign_in_first(server: Starlette) -> None:
    with TestClient(server) as client:
        page = client.get("/auth/choose-login", follow_redirects=False)
        sent = client.post("/auth/choose-login", data={"login": "x"}, follow_redirects=False)
    assert page.headers["location"] == sent.headers["location"] == "/login"


# Where a newcomer's email comes from: their access token's claims, as some providers put it
# there and Auth0 does with an Action, or the provider's userinfo endpoint, as Auth0's tokens
# carry none.
EMAIL_FROM = pytest.mark.parametrize("where", ["claims", "userinfo"])


def newcomer_token(server: Starlette, who: str, where: str = "userinfo", **claims: object) -> str:
    """An access token of the faked provider for `who`, with their email in its claims or
    not."""
    if where == "claims":
        _, email, verified = PEOPLE[who]
        claims = {"email": email, "email_verified": verified, **claims}
    return server.state.fake_provider.token(who, **claims)


class ToolsChanged(MessageHandler):
    """Counts the notifications that the tools changed."""

    def __init__(self) -> None:
        self.count = 0

    async def on_tool_list_changed(self, message: types.ToolListChangedNotification) -> None:
        self.count += 1


async def described(client: Client) -> dict[str, str]:
    """The tools a client lists, each with its description."""
    return {tool.name: tool.description or "" for tool in await client.list_tools()}


def userinfo_reads(server: Starlette) -> int:
    """How many times the server read the faked provider's userinfo endpoint."""
    provider = server.state.fake_provider
    endpoint = provider.document["userinfo_endpoint"]
    return sum(str(sent.url) == endpoint for sent in provider.requests)


@EMAIL_FROM
def test_a_newcomer_chooses_their_login_in_their_assistant(server: Starlette, where: str) -> None:
    apps, database = server.state.apps, server.state.apps.database
    apps.import_folder(APPS / "stock", "etienne", "shop")
    database.invite("etienne", "shop", "sam@a.example", "staff", NOW)
    changed = ToolsChanged()

    with running(server) as address:

        async def main() -> str:
            token = newcomer_token(server, "sam-a", where)
            async with Client(f"{address}/me/mcp", auth=token, message_handler=changed) as client:
                # The tools they will have, and the one that makes them someone.
                tools = await described(client)
                assert set(tools) == PERSON_TOOLS | {CHOOSE_LOGIN}
                offered = re.search(r"offering ([a-z0-9-]+), which is free", tools[CHOOSE_LOGIN])
                assert offered is not None and offered[1] == "sam"
                assert (
                    "permanent" in tools[CHOOSE_LOGIN]
                    and "never their email" in tools[CHOOSE_LOGIN]
                )
                # Each list offers the same login.
                assert (await described(client))[CHOOSE_LOGIN] == tools[CHOOSE_LOGIN]

                refused = await client.call_tool(LIST, {}, raise_on_error=False)
                assert refused.is_error and text(refused) == CHOOSE_FIRST
                for login, sentence in (
                    ("etienne", "Someone already has the login 'etienne'. Choose another one."),
                    ("me", "'me' is kept for Cliquet's own pages. Choose another login."),
                    ("ab", "A login is 3 to 39 characters long, and 'ab' has 2."),
                ):
                    taken = await client.call_tool(
                        CHOOSE_LOGIN, {"login": login}, raise_on_error=False
                    )
                    assert taken.is_error and sentence in text(taken), login
                assert changed.count == 0

                chosen = await client.call_tool(CHOOSE_LOGIN, {"login": offered[1]})
                assert text(chosen) == (
                    f"The person's login is now {offered[1]}. Shares wait for them to accept: "
                    "etienne/shop as staff. Say so, and accept or decline one only if they ask "
                    "you to. Their tools work now: list your tools again, or, if your host "
                    "doesn't, start a new conversation."
                )
                assert changed.count == 1
                assert set(await described(client)) == PERSON_TOOLS
                listed = await client.call_tool(LIST, {})
                assert listed.structured_content is not None
                assert listed.structured_content["waiting"] == [
                    {"app": "etienne/shop", "owner": "etienne", "role": "staff"}
                ]
                again = await client.call_tool(
                    CHOOSE_LOGIN, {"login": "another"}, raise_on_error=False
                )
                assert text(again) == f"The person's login is {offered[1]}, and it can't change."
                return offered[1]

        login = anyio.run(main)

    person = database.person_by_login(login)
    assert person is not None and person.email == "sam@a.example"
    assert person.issuer == AUTH0_ISSUER and person.subject == "auth0|sam-a"
    assert not may(person, Right.BUILD)
    # Read once, while they had no person, however many requests they sent.
    assert userinfo_reads(server) == (1 if where == "userinfo" else 0)


def test_the_first_newcomer_sees_the_builder_s_tools_and_builds(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    privacy = "https://example.com/privacy"
    server = oidc_server(tmp_path, executor, CLIQUET_PRIVACY_URL=privacy)
    with running(server) as address:

        async def main() -> None:
            async with Client(f"{address}/me/mcp", auth=newcomer_token(server, "ann")) as client:
                tools = await described(client)
                assert CHOOSE_LOGIN in tools and "create_app" in tools
                assert f"its privacy notice, {privacy}." in tools[CHOOSE_LOGIN]
                refused = await client.call_tool("create_app", {"app": "x"}, raise_on_error=False)
                assert refused.is_error and text(refused) == CHOOSE_FIRST
                await client.call_tool(CHOOSE_LOGIN, {"login": "ann"})
                tools = await described(client)
                assert CHOOSE_LOGIN not in tools and "create_app" in tools

        anyio.run(main)
    ann = server.state.apps.database.person_by_login("ann")
    assert ann is not None and may(ann, Right.BUILD)


@pytest.mark.parametrize("grants", [False, True])
def test_a_server_may_grant_build_to_everyone_at_sign_up(
    tmp_path: Path, executor: LocalExecutor, grants: bool
) -> None:
    server = oidc_server(tmp_path, executor, CLIQUET_GRANT_BUILD_ON_SIGN_UP="1" if grants else "")
    assert server.state.settings.grant_build_on_sign_up is grants
    database = server.state.apps.database
    # Someone else was first.
    database.person_for(AUTH0_ISSUER, "auth0|owner", "etienne@example.com", "etienne", NOW)
    with running(server) as address:

        async def main() -> None:
            async with Client(f"{address}/me/mcp", auth=newcomer_token(server, "ann")) as client:
                # A host that never lists its tools again has the builder's already.
                assert ("create_app" in await described(client)) is grants
                await client.call_tool(CHOOSE_LOGIN, {"login": "ann"})
                assert ("create_app" in await described(client)) is grants

        anyio.run(main)
    with TestClient(server) as client:
        assert oidc_callback(server, client, "kim") == "/auth/choose-login"
        csrf, _ = login_form(client)
        assert choose_login(client, "kim", csrf).status_code == 303
    for login in ("ann", "kim"):
        person = database.person_by_login(login)
        assert person is not None and may(person, Right.BUILD) is grants


def test_without_a_verified_email_a_newcomer_chooses_a_login_too(server: Starlette) -> None:
    provider = server.state.fake_provider

    async def chooses(token: str) -> None:
        async with Client(f"{address}/me/mcp", auth=token) as client:
            assert CHOOSE_LOGIN in await described(client)
            refused = await client.call_tool(LIST, {}, raise_on_error=False)
            assert refused.is_error and text(refused) == CHOOSE_FIRST

    with running(server) as address:
        # The userinfo endpoint refuses a token without `openid`, as Auth0 does.
        anyio.run(chooses, newcomer_token(server, "sam-a", scope="offline_access"))
        # It gives no email, or doesn't verify it, or names someone else.
        anyio.run(chooses, newcomer_token(server, "email-less"))
        anyio.run(chooses, newcomer_token(server, "unverified"))
        provider.userinfo_subject = "auth0|someone-else"
        anyio.run(chooses, newcomer_token(server, "sam-b"))
        # The answers are kept: a provider that comes back is read again later.
        reads = userinfo_reads(server)
        provider.userinfo_subject = None
        anyio.run(chooses, newcomer_token(server, "sam-b"))
        assert userinfo_reads(server) == reads
    assert server.state.apps.database.person_by_email("sam@a.example") is None


def test_a_newcomer_s_calls_count_against_the_limits(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    limits = Limits(calls_at_once=8, calls_per_minute=3, sandbox_runs_per_hour=300)
    server = oidc_server(tmp_path, executor, limits=limits)
    with running(server) as address:

        async def main() -> list[str]:
            async with Client(f"{address}/me/mcp", auth=newcomer_token(server, "kim")) as client:
                answers = [await client.call_tool(LIST, {}, raise_on_error=False) for _ in range(4)]
            return [text(answer) for answer in answers]

        answers = anyio.run(main)
    assert answers[:3] == [CHOOSE_FIRST] * 3
    assert answers[3].startswith("Too many calls to your apps: at most 3 a minute.")


def test_imported_and_earlier_people_keep_their_logins(server: Starlette) -> None:
    apps, database = server.state.apps, server.state.apps.database
    # The operator chose this login and this email, which have nothing in common.
    apps.import_folder(APPS / "stock", "the-shop", "shop", email="pat@example.com")
    # Someone who signed in before logins were chosen: theirs came from their email.
    earlier = database.person_for(AUTH0_ISSUER, "auth0|old", "kim@example.com", "kim", NOW)

    with TestClient(server) as client:
        assert oidc_callback(server, client, "imported", "/apps/the-shop/shop") == (
            "/apps/the-shop/shop"
        )
        assert client.get("/").status_code == 200
    with TestClient(server) as client:
        assert oidc_callback(server, client, "earlier") == "/"
        assert "kim" in client.get("/").text

    imported = database.person_by_email("pat@example.com")
    assert imported is not None
    assert imported.login == "the-shop" and imported.subject == "auth0|imp"
    assert database.person_by_login("kim") == earlier


def test_a_new_identity_signs_out_whoever_was_signed_in(server: Starlette) -> None:
    kim = server.state.apps.database.person_for(
        AUTH0_ISSUER, "auth0|old", "kim@example.com", "kim", NOW
    )
    web_auth = server.state.web_auth
    session = web_auth.session_cookie(kim)

    with TestClient(server) as client:
        client.cookies.set("session", session)
        start = client.get("/login", follow_redirects=False)
        back = server.state.fake_provider.authorize(start.headers["location"], "sam-a")
        answer = client.get(back, follow_redirects=False)
        assert 'session=""' in answer.headers["set-cookie"]
        # Signing out forgets the identity waiting for its login too.
        token = web_auth.csrf_for_session(session)
        out = client.post("/logout", data={"csrf": token}, follow_redirects=False)
        assert 'new_person=""' in out.headers["set-cookie"]


def test_a_deleted_account_s_login_is_kept_and_its_identity_starts_afresh(
    server: Starlette, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = server.state.apps.database
    with TestClient(server) as client:
        oidc_callback(server, client, "ann")
        csrf, _ = login_form(client)
        assert choose_login(client, "ann", csrf).status_code == 303
        ann = database.person_by_login("ann")
        assert ann is not None
        database.close_account("ann", NOW)
        # The session that named them finds nobody.
        assert client.get("/", follow_redirects=False).headers["location"] == "/login?next=%2F"
    offered = ["ann", "quiet-otter-4k2"]
    monkeypatch.setattr("cliquet_server.sign_in.login_suggestions", lambda *_: iter(offered))
    with TestClient(server) as client:
        # Signing in again is a new identity's first sign-in, which chooses another login.
        assert oidc_callback(server, client, "ann") == "/auth/choose-login"
        csrf, suggested = login_form(client)
        assert suggested == "quiet-otter-4k2"
        refused = choose_login(client, "ann", csrf)
        assert refused.status_code == 400
        assert "Someone already has the login &#39;ann&#39;" in refused.text
        assert choose_login(client, "ann-again", csrf).status_code == 303
    again = database.person_by_login("ann-again")
    assert again is not None and again.id != ann.id and again.email == "ann@example.com"
    assert database.memberships(again.id) == [] and database.tokens(again.id) == []
