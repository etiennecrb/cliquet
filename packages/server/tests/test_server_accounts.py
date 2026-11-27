"""Deleting an account removes everything the person owns and every access they hold, keeps
only their login and their name in other owners' histories, and finishes when run again
after a stop."""

import base64
import threading
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from cliquet.context import Caller
from cliquet.worker import Committed
from cliquet_server.accounts import Accounts
from cliquet_server.apps import Apps
from cliquet_server.builder import Builder
from cliquet_server.db import Database, Forgotten, PersonInfo, Right, SandboxInfo
from cliquet_server.executor import LocalExecutor, SandboxAway, SandboxClient
from cliquet_server.settings import Settings

from .server_helpers import (
    APPS,
    AUTH0_ISSUER,
    NOW,
    a_shop_member,
    app_token,
    choose_login,
    login_form,
    member,
    oidc_callback,
    oidc_server,
)

SALE = {"sku": "TEA-SEN", "quantity": 1, "receipt": "R-0951"}


def _ann(database: Database) -> PersonInfo:
    """ann, who signed in through the provider and may build."""
    database.person_for(AUTH0_ISSUER, "auth0|ann", "ann@example.com", "ann", NOW)
    return database.grant("ann", Right.BUILD)


def test_deleting_an_account_removes_all_it_owns_and_every_access(server: Starlette) -> None:
    apps: Apps = server.state.apps
    database, builder = apps.database, server.state.builder
    ann, sam = _ann(database), a_shop_member(server, None)
    # Two apps: one published and shared, with an invitation waiting, and a draft on preview.
    apps.import_folder(APPS / "stock", "ann", "store")
    member(database, sam, "ann", "store", "staff")
    database.invite("ann", "store", "kim@example.com", "staff", NOW)
    sams_token = app_token(server, "staff", "store", sam, login="ann")
    builder.create_app("ann", "notes")
    builder.start_preview("ann", "notes")
    part = base64.b64encode(b"a,b\n").decode()
    builder.upload_file("ann", "notes", "data/items.csv", 8, 0, part)
    # A role in etienne's app, a change made there, and an error of its code in ann's call.
    member(database, ann, "etienne", "shop", "staff")
    runtime = apps.live("etienne", "shop").runtime
    assert isinstance(runtime.call(Caller("ann", "staff"), "record_sale", SALE), Committed)
    database.keep_error("etienne", "shop", "e-0123456789ab", NOW, "ann", "staff", "stock", "boom")
    limited = app_token(server, "staff", "shop", ann)
    everywhere = database.create_token(ann.id, "laptop", NOW)[1]
    # Their sandbox: the shared local one keeps their builds, and a record says where it is.
    executor = apps.executor
    assert isinstance(executor, LocalExecutor)
    in_sandbox = executor.root / "apps" / "ann"
    assert in_sandbox.is_dir()
    database.save_sandbox(SandboxInfo("ann", "cliquet-ann", "u", "t", "sdk", NOW))

    accounts: Accounts = server.state.accounts
    forgotten = accounts.delete("ann")

    assert forgotten == Forgotten(AUTH0_ISSUER, "auth0|ann", "ann@example.com")
    data = apps.settings.data_dir
    # Apps, repositories, drafts, the preview, the upload and the sandbox are gone.
    assert database.apps("ann") == []
    assert not (data / "apps" / "ann").exists() and not (data / "drafts" / "ann").exists()
    assert apps.current("ann", "store") is None
    assert builder.previews.find("ann", "notes") is None
    assert not any((data / "uploads").iterdir())
    assert not in_sandbox.exists() and database.sandbox("ann") is None
    # Tokens, shares and memberships too, theirs and their members'.
    assert database.tokens(ann.id) == []
    for token in (limited, everywhere, sams_token):
        assert database.token_access(token, NOW) is None
    assert database.memberships(sam.id) == []
    assert [one.person.login for one in database.members("etienne", "shop")] == []
    with TestClient(server) as client:
        for path, token in (("/apps/etienne/shop/mcp", limited), ("/me/mcp", everywhere)):
            refused = client.post(path, json={}, headers={"Authorization": f"Bearer {token}"})
            assert refused.status_code == 401, path
    assert not accounts.left("ann")

    # etienne's app works, and its history and errors still name them.
    runtime = apps.live("etienne", "shop").runtime
    sale = {**SALE, "receipt": "R-0952"}
    assert isinstance(runtime.call(Caller("etienne", "owner"), "record_sale", sale), Committed)
    callers = [entry.caller for entry in runtime.history(Caller("etienne", "owner"))]
    assert "ann" in callers
    kept = database.error("etienne", "shop", "e-0123456789ab", NOW)
    assert kept is not None and kept.caller == "ann"
    # Only the login stays, reserved.
    account = database.account("ann")
    assert account is not None and account.deleted_at == NOW
    assert database.person_by_login("ann") is None


class NoSandbox:
    """An executor whose sandbox is never set up or woken: it records whose it destroys."""

    def __init__(self) -> None:
        self.destroyed: list[str] = []
        self.away = False

    def sandbox(self, owner: str) -> SandboxClient:
        raise AssertionError(f"{owner}'s sandbox was asked for")

    def sweep(self, owners: set[str]) -> None:
        """Nothing to sweep."""

    def destroy(self, owner: str) -> None:
        if self.away:
            raise SandboxAway("Sprites can't be reached")
        self.destroyed.append(owner)

    def close(self) -> None:
        """Nothing to close."""


@pytest.fixture
def no_sandbox() -> NoSandbox:
    return NoSandbox()


@pytest.fixture
def offline(settings: Settings, database: Database, no_sandbox: NoSandbox) -> Accounts:
    """The deletion, over apps whose sandbox is never asked for, with ann's two drafts, the
    record of her sandbox and a folder of her repositories."""
    apps = Apps(settings, database, no_sandbox, clock=lambda: NOW)
    builder = Builder(apps, settings)
    _ann(database)
    builder.create_app("ann", "one")
    builder.create_app("ann", "two")
    database.save_sandbox(SandboxInfo("ann", "cliquet-ann", "u", "t", "sdk", NOW))
    (settings.data_dir / "apps" / "ann" / "one").mkdir(parents=True)
    return Accounts(apps, builder)


def _all_gone(accounts: Accounts, no_sandbox: NoSandbox) -> None:
    data = accounts.apps.settings.data_dir
    assert accounts.database.apps("ann") == []
    assert accounts.database.sandbox("ann") is None
    assert not (data / "apps" / "ann").exists() and not (data / "drafts" / "ann").exists()
    assert no_sandbox.destroyed[-1:] == ["ann"]
    assert not accounts.left("ann")


def test_a_deletion_never_wakes_or_sets_up_the_sandbox(
    offline: Accounts, no_sandbox: NoSandbox
) -> None:
    offline.delete("ann")
    _all_gone(offline, no_sandbox)


def _stop(where: str, accounts: Accounts, no_sandbox: NoSandbox, monkeypatch: Any) -> None:
    """Make the deletion stop at `where`, as a crash or a deploy would."""
    builder = accounts.builder
    assert builder is not None
    if where in ("first app", "second app"):
        real = builder.remove_app
        calls = []

        def remove_app(login: str, app: str, **kwargs: Any) -> None:
            calls.append(app)
            if len(calls) == (1 if where == "first app" else 2):
                raise RuntimeError("stopped")
            real(login, app, **kwargs)

        monkeypatch.setattr(builder, "remove_app", remove_app)
    elif where == "sandbox":
        monkeypatch.setattr(no_sandbox, "away", True)
    else:

        def delete_sandbox(login: str) -> None:
            raise RuntimeError("stopped")

        monkeypatch.setattr(accounts.database, "delete_sandbox", delete_sandbox)


@pytest.mark.parametrize("where", ["first app", "second app", "sandbox", "sandbox's record"])
def test_a_deletion_stopped_midway_finishes_when_run_again(
    offline: Accounts, no_sandbox: NoSandbox, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    with monkeypatch.context() as stopped:
        _stop(where, offline, no_sandbox, stopped)
        with pytest.raises((RuntimeError, SandboxAway)):
            offline.delete("ann")
    # Closed from the first step: nobody reaches anything of it, and what is left says so.
    assert offline.database.person_by_login("ann") is None
    assert offline.left("ann")
    again = offline.delete("ann")
    assert again == Forgotten(None, None, None)
    _all_gone(offline, no_sandbox)


def test_finishing_runs_only_the_deletions_a_stop_cut_off(
    offline: Accounts, no_sandbox: NoSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as stopped:
        _stop("second app", offline, no_sandbox, stopped)
        with pytest.raises(RuntimeError):
            offline.delete("ann")
    # Someone deleted earlier, with nothing left, isn't deleted again.
    offline.database.person_for(AUTH0_ISSUER, "auth0|bob", "bob@example.com", "bob", NOW)
    offline.delete("bob")
    no_sandbox.destroyed.clear()
    assert offline.finish() == ["ann"]
    _all_gone(offline, no_sandbox)
    assert no_sandbox.destroyed == ["ann"]


def test_the_server_finishes_a_stopped_deletion_as_it_starts_without_waiting_for_it(
    server: Starlette, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts: Accounts = server.state.accounts
    _ann(accounts.database)
    accounts.apps.import_folder(APPS / "stock", "ann", "store")
    # Stopped right after its first step.
    accounts.database.close_account("ann", NOW)
    go, done = threading.Event(), threading.Event()
    finished: list[list[str]] = []
    real = accounts.finish

    def finish() -> list[str]:
        go.wait(30)
        finished.append(real())
        done.set()
        return finished[-1]

    monkeypatch.setattr(accounts, "finish", finish)
    with TestClient(server) as client:
        # The server answers while the deletions are still to finish.
        assert client.get("/healthz").status_code == 200
        go.set()
        assert done.wait(30)
    assert finished == [["ann"]]
    assert accounts.database.apps("ann") == [] and not accounts.left("ann")


def test_a_finish_that_fails_is_logged_and_tried_at_the_next_start(
    offline: Accounts, no_sandbox: NoSandbox, caplog: pytest.LogCaptureFixture
) -> None:
    offline.database.close_account("ann", NOW)
    no_sandbox.away = True
    assert offline.finish() == []
    assert any(
        record.getMessage() == "unexpected: the deletion of ann wasn't finished"
        for record in caplog.records
    )
    no_sandbox.away = False
    assert offline.finish() == ["ann"]
    _all_gone(offline, no_sandbox)


def test_the_forgotten_identity_is_logged_for_the_operator_but_never_the_email(
    offline: Accounts, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="cliquet_server")
    offline.delete("ann")
    said = [record.getMessage() for record in caplog.records]
    assert f"forgot the identity of ann: issuer {AUTH0_ISSUER}, subject auth0|ann" in said
    assert not any("ann@example.com" in message for message in said)


def test_an_unknown_login_deletes_nothing(offline: Accounts, no_sandbox: NoSandbox) -> None:
    assert offline.delete("nobody") is None
    assert no_sandbox.destroyed == []
    assert [app.name for app in offline.database.apps("ann")] == ["one", "two"]


def test_the_login_is_kept_and_signing_in_again_starts_an_empty_account(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    server = oidc_server(tmp_path, executor)
    database = server.state.apps.database
    with TestClient(server) as client:
        oidc_callback(server, client, "ann")
        csrf, _ = login_form(client)
        assert choose_login(client, "ann", csrf).status_code == 303
        ann = database.grant("ann", Right.BUILD)
        server.state.builder.create_app("ann", "notes")
        member_of = database.create_app("kim", "club", NOW, email="kim@example.com")
        database.mark_published("kim", "club", NOW)
        database.share("kim", member_of.name, "ann@example.com", "member", NOW)
        server.state.accounts.delete("ann")
    with TestClient(server) as client:
        assert oidc_callback(server, client, "ann") == "/auth/choose-login"
        csrf, _ = login_form(client)
        assert choose_login(client, "ann", csrf).status_code == 400
        assert choose_login(client, "ann-again", csrf).status_code == 303
        home = client.get("/").text
    again = database.person_by_login("ann-again")
    assert again is not None and again.id != ann.id and again.rights == frozenset()
    assert database.visible_apps("ann-again") == [] and database.pending_shares(again.id) == []
    assert "notes" not in home and "kim/club" not in home
