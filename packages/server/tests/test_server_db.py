from datetime import UTC, datetime, timedelta, timezone

import pytest

from cliquet_server.db import (
    AccountDeleted,
    AppExists,
    AppInfo,
    Database,
    LoginRefused,
    LoginTaken,
    PersonInfo,
    Right,
    SandboxInfo,
    TokenLimit,
    may,
)

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


def test_an_app_belongs_to_an_owner(database: Database) -> None:
    info = database.create_app("etienne", "shop", NOW)
    assert isinstance(info, AppInfo)
    assert info.published_at is None
    assert database.app("etienne", "shop") == info
    assert database.app("etienne", "other") is None


def test_a_name_is_unique_for_its_owner(database: Database) -> None:
    database.create_app("etienne", "shop", NOW)
    with pytest.raises(AppExists):
        database.create_app("etienne", "shop", NOW)
    database.create_app("sam", "shop", NOW)


def test_names_are_checked(database: Database) -> None:
    for name in ("Shop", "1shop", "a/b", ""):
        with pytest.raises(ValueError):
            database.create_app("etienne", name, NOW)
    with pytest.raises(ValueError):
        database.create_app("Sam_1", "shop", NOW)


def test_apps_are_listed_in_order(database: Database) -> None:
    database.create_app("sam", "b", NOW)
    database.create_app("sam", "a", NOW)
    database.create_app("etienne", "c", NOW)
    assert [(i.login, i.name) for i in database.apps()] == [
        ("etienne", "c"),
        ("sam", "a"),
        ("sam", "b"),
    ]
    assert [i.name for i in database.apps("sam")] == ["a", "b"]
    database.mark_published("sam", "a", NOW)
    assert [i.name for i in database.apps(published=True)] == ["a"]


def test_times_keep_their_offset(database: Database) -> None:
    database.create_app("sam", "a", NOW)
    published = datetime(2026, 11, 11, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    database.mark_published("sam", "a", published, description="first")
    info = database.app("sam", "a")
    assert info is not None and info.published_at == published
    assert info.published_at is not None
    assert info.published_at.tzinfo is not None
    database.mark_published("sam", "a", published + timedelta(hours=1), description="x" * 600)
    again = database.app("sam", "a")
    assert again is not None
    assert again.published_at == published
    assert again.description == "x" * 500


def test_the_look_is_kept_at_publish(database: Database) -> None:
    def look() -> tuple[str | None, str | None]:
        info = database.app("sam", "a")
        assert info is not None
        return info.accent, info.icon

    database.create_app("sam", "a", NOW)
    assert look() == (None, None)
    database.mark_published("sam", "a", NOW, accent="#1e3a8a", icon="calculator")
    assert look() == ("#1e3a8a", "calculator")
    assert [info.icon for info in database.visible_apps("sam")] == ["calculator"]
    database.mark_published("sam", "a", NOW)
    assert look() == (None, None)
    database.set_look("sam", "a", "#be123c", "clapperboard", None)
    assert look() == ("#be123c", "clapperboard")
    # It is written into a page's style: nothing but #rrggbb is kept.
    with pytest.raises(ValueError, match="#rrggbb"):
        database.set_look("sam", "a", "red; background: url(x)", None, None)
    # The app's name as people read it is kept with the look, and goes with it.
    database.mark_published("sam", "a", NOW, title="FoodBank HQ")
    info = database.app("sam", "a")
    assert info is not None and info.title == "FoodBank HQ"
    database.set_look("sam", "a", None, None, "Food Bank")
    assert [one.title for one in database.visible_apps("sam")] == ["Food Bank"]
    database.mark_published("sam", "a", NOW)
    info = database.app("sam", "a")
    assert info is not None and info.title is None
    with pytest.raises(ValueError, match="title"):
        database.set_look("sam", "a", None, None, "x" * 41)
    database.delete_app("sam", "a")
    assert database.apps() == []


def test_a_time_without_an_offset_is_refused(database: Database) -> None:
    with pytest.raises(ValueError):
        database.create_app("sam", "a", datetime(2026, 11, 11))


def test_one_sandbox_per_owner(database: Database) -> None:
    first = SandboxInfo("sam", "cliquet-sam", "http://one", "t1", "a" * 64, NOW)
    database.save_sandbox(first)
    assert database.sandbox("sam") == first
    assert database.sandbox_logins() == ["sam"]
    second = SandboxInfo("sam", "cliquet-sam", "http://two", "t2", "b" * 64, None)
    database.save_sandbox(second)
    assert database.sandbox("sam") == second
    assert database.sandbox("etienne") is None
    database.delete_sandbox("sam")
    assert database.sandbox("sam") is None
    database.delete_sandbox("sam")


def test_owners_are_listed_by_their_last_use(database: Database) -> None:
    for login in ("sam", "etienne", "camille", "dana"):
        database.create_app(login, "shop", NOW)
    database.note_apps_used("sam", NOW - timedelta(days=3))
    database.note_apps_used("etienne", NOW - timedelta(hours=1))
    database.note_apps_used("camille", NOW - timedelta(days=9))
    database.note_apps_used("nobody", NOW)
    # The most recent first; one never used, or used before `since`, isn't listed.
    assert database.apps_used_since(NOW - timedelta(days=7)) == ["etienne", "sam"]
    assert database.person_by_login("nobody") is None


def test_deleting_an_app(database: Database) -> None:
    database.create_app("sam", "shop", NOW)
    database.delete_app("sam", "shop")
    assert database.app("sam", "shop") is None
    database.create_app("sam", "shop", NOW)


def test_an_app_s_roles_are_kept_until_it_is_deleted(database: Database) -> None:
    database.create_app("sam", "shop", NOW)
    assert database.kept_roles("sam", "shop") is None
    database.keep_roles("sam", "shop", ["owner", "staff"])
    assert database.kept_roles("sam", "shop") == ["owner", "staff"]
    # A later publish replaces them.
    database.keep_roles("sam", "shop", ["staff", "buyer"])
    assert database.kept_roles("sam", "shop") == ["staff", "buyer"]
    database.keep_roles("sam", "notes", [])
    assert database.kept_roles("sam", "notes") is None
    database.delete_app("sam", "shop")
    database.create_app("sam", "shop", NOW)
    assert database.kept_roles("sam", "shop") is None
    database.keep_roles("sam", "shop", [])
    assert database.kept_roles("sam", "shop") == []


def make_person(
    database: Database,
    email: str = "sam@example.com",
    login: str = "sam",
    subject: str = "subject-sam",
) -> PersonInfo:
    return database.person_for(
        "https://example.auth0.com",
        subject,
        email,
        login,
        NOW,
    )


def test_a_person_is_unique_by_issuer_and_subject(database: Database) -> None:
    first = make_person(database, "Sam@Example.com", "sam")
    assert first.email == "sam@example.com"
    again = database.person_for(
        "https://example.auth0.com",
        "subject-sam",
        "sam.new@example.com",
        "sam-new",
        NOW,
    )
    assert again.id == first.id
    assert again.login == "sam"
    assert again.email == "sam.new@example.com"
    assert database.person_by_login("sam") == again
    assert database.person_by_email("SAM.NEW@EXAMPLE.COM") == again


def test_the_first_person_a_server_has_may_build_and_nobody_after(database: Database) -> None:
    ann = make_person(database, "ann@example.com", "ann", "subject-ann")
    assert may(ann, Right.BUILD)
    assert not may(make_person(database), Right.BUILD)
    # Not even once nobody may build: the server isn't new any more.
    database.withdraw("ann", Right.BUILD)
    assert not may(make_person(database, "kim@example.com", "kim", "subject-kim"), Right.BUILD)


def test_a_person_builds_once_granted_until_it_is_withdrawn(database: Database) -> None:
    make_person(database, "ann@example.com", "ann", "subject-ann")  # the first, who may build
    sam = make_person(database)
    assert not may(sam, Right.BUILD)
    assert may(database.grant("sam", Right.BUILD), Right.BUILD)
    # Granting again changes nothing, and the right is kept with the person.
    database.grant("sam", Right.BUILD)
    found = database.person_by_login("sam")
    assert found is not None and found.rights == {Right.BUILD}
    assert not may(database.withdraw("sam", Right.BUILD), Right.BUILD)
    assert not may(database.withdraw("sam", Right.BUILD), Right.BUILD)
    assert database.person_by_id(sam.id) == sam


def test_a_right_needs_a_person(database: Database) -> None:
    with pytest.raises(ValueError, match="Nobody has the login 'sam'"):
        database.grant("sam", Right.BUILD)


def test_two_identities_cannot_share_a_login(database: Database) -> None:
    make_person(database, "sam@example.com", "sam", "subject-sam")
    with pytest.raises(ValueError, match="login"):
        database.person_for(
            "https://example.auth0.com",
            "subject-other",
            "sam.other@example.com",
            "sam",
            NOW,
        )


def test_members_can_use_only_their_roles(database: Database) -> None:
    database.create_app("etienne", "shop", NOW)
    database.mark_published("etienne", "shop", NOW)
    sam = make_person(database)
    assert sam.email is not None
    database.share("etienne", "shop", sam.email, "reader", NOW)
    # A share gives nothing until its person accepts it.
    assert database.may_use("sam", "etienne", "shop", "reader") is False
    assert database.may_open("sam", "etienne", "shop") is False
    assert database.accept_share(sam.id, "etienne", "shop", NOW) == ["reader"]

    assert database.may_use("etienne", "etienne", "shop", "owner") is True
    assert database.may_use("etienne", "etienne", "shop", "reader") is True
    assert database.may_use("sam", "etienne", "shop", "reader") is True
    assert database.may_use("sam", "etienne", "shop", "owner") is False
    assert database.may_use("sam", "etienne", "shop", "other") is False
    assert database.may_use("nobody", "etienne", "shop", "reader") is False
    assert database.may_use("sam", "etienne", "missing", "reader") is False


def test_visible_apps_include_owned_and_member_apps(database: Database) -> None:
    database.create_app("etienne", "shop", NOW)
    database.mark_published("etienne", "shop", NOW)
    database.create_app("other", "hidden", NOW)
    sam = make_person(database)
    assert sam.email is not None
    database.share("etienne", "shop", sam.email, "reader", NOW)
    assert database.visible_apps("sam") == []
    database.accept_share(sam.id, "etienne", "shop", NOW)

    assert [app.name for app in database.visible_apps("etienne")] == ["shop"]
    assert [app.name for app in database.visible_apps("sam")] == ["shop"]
    assert [app.name for app in database.visible_apps("other")] == ["hidden"]
    assert database.visible_apps("nobody") == []


def test_an_invitation_waits_for_the_person(database: Database) -> None:
    database.create_app("etienne", "shop", NOW)
    database.invite("etienne", "shop", "Sam@Example.com", "reader", NOW)
    assert database.may_use("sam", "etienne", "shop", "reader") is False
    pending = database.pending_invitations("etienne", "shop")
    assert [(item.email, item.role) for item in pending] == [("sam@example.com", "reader")]


def test_an_invitation_is_claimed_once(database: Database) -> None:
    database.create_app("etienne", "shop", NOW)
    database.mark_published("etienne", "shop", NOW)
    database.invite("etienne", "shop", "sam@example.com", "reader", NOW)
    sam = make_person(database)

    claimed = database.claim_invitations(sam, NOW)
    assert [(item.login, item.name, item.role) for item in claimed] == [
        ("etienne", "shop", "reader")
    ]
    # A claimed invitation is a share to accept, like any other.
    assert database.may_use("sam", "etienne", "shop", "reader") is False
    assert database.pending_invitations("etienne", "shop") == []
    assert database.claim_invitations(sam, NOW) == []
    assert [share.role for share in database.waiting_shares("etienne", "shop")] == ["reader"]
    assert database.accept_share(sam.id, "etienne", "shop", NOW) == ["reader"]
    assert database.may_use("sam", "etienne", "shop", "reader") is True


def test_members_are_listed_with_their_roles(database: Database) -> None:
    database.create_app("etienne", "shop", NOW)
    database.mark_published("etienne", "shop", NOW)
    sam = make_person(database)
    assert sam.email is not None
    database.share("etienne", "shop", sam.email, "reader", NOW)
    assert database.members("etienne", "shop") == []
    database.accept_share(sam.id, "etienne", "shop", NOW)
    members = database.members("etienne", "shop")
    assert [(item.person.login, item.role) for item in members] == [("sam", "reader")]
    assert members[0].app.name == "shop"


def test_only_the_owner_can_share(database: Database) -> None:
    database.create_app("etienne", "shop", NOW)
    sam = make_person(database)
    with pytest.raises(ValueError, match="owner"):
        database.share("sam", "shop", "other@example.com", "reader", NOW)
    with pytest.raises(ValueError, match="owner"):
        database.invite("sam", "shop", "other@example.com", "reader", NOW)
    with pytest.raises(ValueError, match="owner"):
        database.remove_member("sam", "shop", sam.id)
    with pytest.raises(ValueError, match="owner"):
        database.remove_share("sam", "shop", sam.id, "reader")
    with pytest.raises(ValueError, match="owner"):
        database.remove_invitation("sam", "shop", "other@example.com", "reader")


def test_removing_a_member_removes_access(database: Database) -> None:
    database.create_app("etienne", "shop", NOW)
    database.mark_published("etienne", "shop", NOW)
    sam = make_person(database)
    assert sam.email is not None
    database.share("etienne", "shop", sam.email, "reader", NOW)
    database.accept_share(sam.id, "etienne", "shop", NOW)
    database.share("etienne", "shop", sam.email, "writer", NOW)
    database.remove_member("etienne", "shop", sam.id)
    assert database.waiting_shares("etienne", "shop") == []
    assert database.may_use("sam", "etienne", "shop", "reader") is False
    assert database.may_use("etienne", "etienne", "shop", "owner") is True


def test_a_no_role_app_uses_the_dash_role(database: Database) -> None:
    database.create_app("etienne", "notes", NOW)
    database.mark_published("etienne", "notes", NOW)
    sam = make_person(database)
    assert sam.email is not None
    database.share("etienne", "notes", sam.email, "-", NOW)
    database.accept_share(sam.id, "etienne", "notes", NOW)
    assert database.may_use("sam", "etienne", "notes", "-") is True
    assert database.may_use("sam", "etienne", "notes", "reader") is False


def test_a_share_shows_only_what_cliquet_knows(database: Database) -> None:
    database.create_app("etienne", "shop", NOW)
    database.mark_published("etienne", "shop", NOW, "The author's own words.")
    database.create_app("etienne", "draft", NOW)
    sam = make_person(database)
    assert sam.email is not None
    for app in ("shop", "draft"):
        database.share("etienne", app, sam.email, "reader", NOW)
    database.share("etienne", "shop", sam.email, "writer", NOW)
    # Sharing again changes nothing; a draft that was never published isn't listed. Whether
    # an email has an account, or a share waits already, is never said.
    assert database.share("etienne", "shop", sam.email, "reader", NOW) == "shared"
    assert database.share("etienne", "shop", "new@example.com", "reader", NOW) == "shared"
    (first, second) = database.pending_shares(sam.id)
    assert (first.login, first.name, first.role) == ("etienne", "shop", "reader")
    assert second.role == "writer"
    assert not hasattr(first, "description")
    assert database.accept_share(sam.id, "etienne", "shop", NOW) == ["reader", "writer"]
    assert database.pending_shares(sam.id) == []
    # Accepted is accepted: sharing a held role again waits for nothing.
    database.share("etienne", "shop", sam.email, "reader", NOW)
    assert database.waiting_shares("etienne", "shop") == []
    assert database.accept_share(sam.id, "etienne", "missing", NOW) == []
    # A share of an app not published yet isn't shown, so it can't be accepted either.
    assert database.accept_share(sam.id, "etienne", "draft", NOW) == []
    assert database.may_open("sam", "etienne", "draft") is False


def test_a_declined_share_gives_nothing(database: Database) -> None:
    database.create_app("etienne", "shop", NOW)
    database.mark_published("etienne", "shop", NOW)
    sam = make_person(database)
    assert sam.email is not None
    database.share("etienne", "shop", sam.email, "reader", NOW)
    assert database.decline_share(sam.id, "etienne", "shop") is True
    assert database.decline_share(sam.id, "etienne", "shop") is False
    assert database.accept_share(sam.id, "etienne", "shop", NOW) == []
    assert database.may_open("sam", "etienne", "shop") is False
    database.share("etienne", "shop", sam.email, "reader", NOW)
    database.remove_share("etienne", "shop", sam.id, "reader")
    assert database.waiting_shares("etienne", "shop") == []


def test_a_share_keeps_the_email_typed_last(database: Database) -> None:
    database.create_app("etienne", "shop", NOW)
    sam = make_person(database)
    database.share("etienne", "shop", "Sam@Example.com ", "reader", NOW)
    # Sam signs in with a new email: the share keeps the one typed.
    assert database.known_person("https://example.auth0.com", "subject-sam", "sam@new.example", NOW)
    (waiting,) = database.waiting_shares("etienne", "shop")
    assert (waiting.email, waiting.person.id, waiting.person.email) == (
        "sam@example.com",
        sam.id,
        "sam@new.example",
    )
    # Shared again, with the new email: one share still waits, by the email typed last.
    database.share("etienne", "shop", "sam@new.example", "reader", NOW)
    assert [one.email for one in database.waiting_shares("etienne", "shop")] == ["sam@new.example"]


def test_removing_an_invitation(database: Database) -> None:
    database.create_app("etienne", "shop", NOW)
    database.invite("etienne", "shop", "sam@example.com", "reader", NOW)
    database.remove_invitation("etienne", "shop", "SAM@example.com", "reader")
    assert database.pending_invitations("etienne", "shop") == []


def test_invalid_people_and_roles_are_refused(database: Database) -> None:
    database.create_app("etienne", "shop", NOW)
    with pytest.raises(ValueError, match="email"):
        database.person_for("issuer", "subject", "not-an-email", "sam", NOW)
    with pytest.raises(ValueError, match="login"):
        database.person_for("issuer", "subject", "sam@example.com", "Not A Login", NOW)
    with pytest.raises(ValueError, match="role"):
        database.invite("etienne", "shop", "sam@example.com", "", NOW)


def test_an_owner_gets_a_provider_identity_on_first_sign_in(database: Database) -> None:
    database.create_app("etienne", "shop", NOW, email="etienne@example.com")
    local = database.person_by_login("etienne")
    assert local is not None
    assert local.email == "etienne@example.com" and local.issuer is None

    person = database.person_for(
        "https://example.auth0.com",
        "subject-etienne",
        "Etienne@Example.com",
        "etienne",
        NOW,
    )
    assert person.id == local.id
    assert person.email == "etienne@example.com"
    assert database.may_use("etienne", "etienne", "shop", "owner") is True
    assert [app.name for app in database.visible_apps("etienne")] == ["shop"]


def test_a_new_identity_has_no_person_until_it_chooses_a_login(database: Database) -> None:
    database.create_app("the-shop", "shop", NOW, email="pat@example.com")
    assert database.known_person("issuer", "subject-new", "new@example.com", NOW) is None
    claimed = database.known_person("issuer", "subject-pat", "Pat@Example.com", NOW)
    assert claimed is not None and claimed.login == "the-shop"
    with pytest.raises(LoginTaken, match="Choose another one"):
        database.person_for("issuer", "subject-new", "new@example.com", "the-shop", NOW)
    made = database.person_for("issuer", "subject-new", "new@example.com", "newcomer", NOW)
    assert database.known_person("issuer", "subject-new", "new@example.com", NOW) == made


def test_an_account_that_holds_nothing_joins_the_one_waiting_for_its_email(
    database: Database,
) -> None:
    database.create_app("pat", "shop", NOW, email="pat@example.com")
    waiting = database.person_by_login("pat")
    assert waiting is not None
    # pat2 chose a login with no verified email, and may build.
    database.person_for("issuer", "subject-pat", None, "pat2", NOW)
    pat2 = database.grant("pat2", Right.BUILD)
    later = NOW + timedelta(days=1)
    joined = database.known_person("issuer", "subject-pat", "pat@example.com", later)
    assert joined is not None and joined.id == waiting.id and joined.login == "pat"
    assert (joined.issuer, joined.subject) == ("issuer", "subject-pat")
    assert joined.email_verified and may(joined, Right.BUILD)
    # pat2 is closed as a deleted account is: its login stays, and its id finds nobody.
    retired = database.account("pat2")
    assert retired is not None and retired.deleted_at == later and retired.subject is None
    assert database.person_by_id(pat2.id) is None
    assert database.deleted_logins() == ["pat2"]


def test_an_account_that_holds_something_stays_apart_from_the_waiting_one(
    database: Database,
) -> None:
    database.create_app("pat", "shop", NOW, email="pat@example.com")
    pat2 = database.person_for("issuer", "subject-pat", None, "pat2", NOW)
    database.create_token(pat2.id, "till", NOW)
    kept = database.known_person("issuer", "subject-pat", "pat@example.com", NOW)
    assert kept is not None and kept.id == pat2.id
    assert kept.email is None and kept.email_verified
    waiting = database.person_by_login("pat")
    assert waiting is not None and waiting.subject is None and waiting.email == "pat@example.com"
    assert database.deleted_logins() == []


def test_a_new_person_s_login_follows_the_rules(database: Database) -> None:
    with pytest.raises(LoginRefused, match="kept for Cliquet's own pages"):
        database.person_for("issuer", "subject-me", "me@example.com", "me", NOW)
    assert database.known_person("issuer", "subject-me", "me@example.com", NOW) is None


def test_an_imported_login_follows_the_rules(database: Database) -> None:
    with pytest.raises(LoginRefused, match="kept for Cliquet's own pages"):
        database.create_app("tokens", "shop", NOW, email="pat@example.com")
    assert database.person_by_login("tokens") is None


def _closing(database: Database) -> tuple[PersonInfo, PersonInfo]:
    """ann, who owns a shared app, holds a role in sam's, a share and a token; and sam."""
    ann = database.person_for("issuer", "subject-ann", "ann@example.com", "ann", NOW)
    sam = database.person_for("issuer", "subject-sam", "sam@example.com", "sam", NOW)
    database.grant("ann", Right.BUILD)
    for login, name in (("ann", "notes"), ("sam", "shop"), ("sam", "till")):
        database.create_app(login, name, NOW)
        database.mark_published(login, name, NOW)
    database.share("ann", "notes", "sam@example.com", "reader", NOW)
    database.accept_share(sam.id, "ann", "notes", NOW)
    database.invite("ann", "notes", "kim@example.com", "reader", NOW)
    database.share("sam", "shop", "ann@example.com", "staff", NOW)
    database.accept_share(ann.id, "sam", "shop", NOW)
    database.share("sam", "till", "ann@example.com", "staff", NOW)
    database.invite("sam", "till", "ann@example.com", "owner", NOW)
    database.create_token(ann.id, "laptop", NOW)
    # sam's token reaches ann's app too: its limit there goes, its other one stays.
    limits = [TokenLimit("ann", "notes", "reader"), TokenLimit("sam", "shop", "owner")]
    database.create_token(sam.id, "routine", NOW, limits)
    database.note_apps_used("ann", NOW)
    return ann, sam


def test_closing_an_account_removes_every_access_and_forgets_the_identity(
    database: Database,
) -> None:
    ann, sam = _closing(database)
    forgotten = database.close_account("ann", NOW)
    assert forgotten is not None
    assert (forgotten.issuer, forgotten.subject, forgotten.email) == (
        "issuer",
        "subject-ann",
        "ann@example.com",
    )
    # Nobody reaches ann's app any more, and ann reaches nothing.
    assert database.members("ann", "notes") == []
    assert database.pending_invitations("ann", "notes") == []
    assert database.memberships(ann.id) == []
    assert database.pending_shares(ann.id) == []
    assert database.pending_invitations("sam", "till") == []
    assert database.tokens(ann.id) == []
    (routine,) = database.tokens(sam.id)
    assert routine.limits == (TokenLimit("sam", "shop", "owner"),)
    assert [app.name for app in database.visible_apps("sam")] == ["shop", "till"]
    # Their apps stay, for the deletion's next steps to remove.
    assert [app.name for app in database.apps("ann")] == ["notes"]
    # Nothing finds them, but their login is kept.
    assert database.person_by_login("ann") is None
    assert database.person_by_id(ann.id) is None
    assert database.person_by_identity("issuer", "subject-ann") is None
    assert database.person_by_email("ann@example.com") is None
    assert database.apps_used_since(NOW) == []
    kept = database.account("ann")
    assert kept is not None and kept.deleted_at == NOW
    assert (kept.issuer, kept.subject, kept.email, kept.rights) == (None, None, None, frozenset())
    assert database.deleted_logins() == ["ann"]
    assert database.person_by_login("sam") == sam


def test_closing_an_account_again_forgets_nothing_more(database: Database) -> None:
    _closing(database)
    database.close_account("ann", NOW)
    again = database.close_account("ann", NOW + timedelta(days=1))
    assert again is not None and (again.issuer, again.subject, again.email) == (None, None, None)
    kept = database.account("ann")
    assert kept is not None and kept.deleted_at == NOW
    assert database.close_account("nobody", NOW) is None


def test_the_deleted_logins_with_rows_are_those_with_an_app_or_a_sandbox(
    database: Database,
) -> None:
    _closing(database)
    for login in ("bob", "kim"):
        database.person_for("issuer", f"subject-{login}", f"{login}@example.com", login, NOW)
    database.save_sandbox(SandboxInfo("bob", "cliquet-bob", "u", "t", "sdk", NOW))
    database.save_sandbox(SandboxInfo("sam", "cliquet-sam", "u", "t", "sdk", NOW))
    for login in ("ann", "bob", "kim"):
        database.close_account(login, NOW)
    # ann's app and bob's sandbox are left; kim has nothing, and sam wasn't deleted.
    assert database.deleted_logins_with_rows() == ["ann", "bob"]
    assert database.deleted_logins() == ["ann", "bob", "kim"]


def test_a_deleted_account_s_login_is_kept_for_good(database: Database) -> None:
    ann, _sam = _closing(database)
    database.close_account("ann", NOW)
    # The same identity starts afresh, with another login.
    with pytest.raises(AccountDeleted, match="Someone already has the login 'ann'"):
        database.person_for("issuer", "subject-ann", "ann@example.com", "ann", NOW)
    assert database.known_person("issuer", "subject-ann", "ann@example.com", NOW) is None
    again = database.person_for("issuer", "subject-ann", "ann@example.com", "ann-again", NOW)
    # A new id: a session that named the old one finds nobody.
    assert again.id > ann.id and again.rights == frozenset()
    assert database.memberships(again.id) == [] and database.tokens(again.id) == []
    # Nobody gives the login a new person, an app or a right.
    for refused in (
        lambda: database.create_app("ann", "notes2", NOW),
        lambda: database.create_app("ann", "other", NOW, email="new@example.com"),
        lambda: database.grant("ann", Right.BUILD),
        lambda: database.save_sandbox(SandboxInfo("ann", "cliquet-ann", "u", "t", "sdk", NOW)),
    ):
        with pytest.raises(AccountDeleted, match="use another login"):
            refused()
    assert database.sandbox("ann") is None


def test_a_deleted_person_s_last_and_highest_id_is_never_reused(database: Database) -> None:
    database.person_for("issuer", "subject-sam", "sam@example.com", "sam", NOW)
    last = database.person_for("issuer", "subject-ann", "ann@example.com", "ann", NOW)
    database.close_account("ann", NOW)
    newcomer = database.person_for("issuer", "subject-kim", "kim@example.com", "kim", NOW)
    assert newcomer.id > last.id
