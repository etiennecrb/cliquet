"""Deleting a person's account: everything they own, and every access they hold.

A person deletes their own account from the console, and the operator anyone's with
`cliquet-server delete-account`: both call `Accounts.delete`. So does a sign-in that makes a
person the account waiting for their email, for the empty account it closed. Only the login
stays, reserved for ever, with the person's name in the history of other owners' apps, which
is never rewritten. The identity at the sign-in provider is only forgotten: Cliquet never
calls the provider, so the operator deletes the person there.

The deletion is safe to stop at any point and to run again. Its first step closes the
account in one transaction, so nobody reaches anything of it from then on. Each later step
removes files before the rows that describe them, so a stop leaves rows that say what is
left, and the next run, or the server's next start, finishes it.
"""

import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from cliquet_server.apps import Apps
from cliquet_server.builder import Builder
from cliquet_server.builder.drafts import Drafts
from cliquet_server.db import Forgotten
from cliquet_server.inputs import References

_log = logging.getLogger(__name__)


class Accounts:
    """The one way an account is deleted, from the console or the server's command line."""

    def __init__(
        self, apps: Apps, builder: Builder | None = None, inputs: References | None = None
    ) -> None:
        self.apps = apps
        self.database = apps.database
        self.builder = builder
        # The references to the files the person uploads for an action, which go too.
        self.inputs = inputs
        # Drafts left by a builder that is off now go too.
        self._drafts = builder.drafts if builder is not None else Drafts.of(apps.settings)

    def delete(
        self, login: str, *, forgotten: Callable[[Forgotten], None] | None = None
    ) -> Forgotten | None:
        """Delete the account with this login and everything it owns, and say what was
        forgotten of the person's identity: None when nobody ever had the login. Run again on
        a deleted account, it finishes what a stop left, and forgets nothing more.

        `forgotten` is told the identity as soon as it is forgotten, before the rest is
        removed, so a stop later still leaves the operator what to delete at the provider.
        """
        closed = self.database.close_account(login, self.apps.now())
        if closed is None:
            return None
        if closed.subject is not None:
            # The one place that keeps it, so the operator can delete the person at the
            # provider whichever way the account was deleted. Never the email.
            _log.info(
                "forgot the identity of %s: issuer %s, subject %s",
                login,
                closed.issuer,
                closed.subject,
            )
        if forgotten is not None:
            forgotten(closed)
        # Open uploads go first, so none finishes into a draft once its app is gone.
        if self.builder is not None:
            self.builder.uploads.drop(login)
        if self.inputs is not None:
            self.inputs.drop(login)
        # Published or not: each app goes with its preview, draft and repository, its row
        # last. The sandbox is left alone, since the whole of it goes next.
        for info in self.database.apps(login):
            if self.builder is not None:
                self.builder.remove_app(login, info.name, in_sandbox=False)
            else:
                self.apps.delete(login, info.name, in_sandbox=False)
        self.apps.executor.destroy(login)
        # The folders under the login go after the sandbox: until then they say, with the
        # sandbox's record, that something is left, even under an executor that keeps none.
        for folder in self._folders(login):
            shutil.rmtree(folder, ignore_errors=True)
        self.database.delete_sandbox(login)
        _log.info("deleted the account %s", login)
        return closed

    def left(self, login: str) -> bool:
        """Whether something of a deleted account is still there, which a stop left: an app,
        the record of a sandbox, or a folder."""
        return bool(
            self.database.apps(login)
            or self.database.sandbox(login) is not None
            or any(folder.exists() for folder in self._folders(login))
        )

    def finish(self) -> list[str]:
        """Finish every deletion a stop cut off, as the server starts, and give their logins.
        One that fails is logged, and the next start tries it again."""
        with_rows = set(self.database.deleted_logins_with_rows())
        finished = []
        for login in self.database.deleted_logins():
            if login not in with_rows and not any(f.exists() for f in self._folders(login)):
                continue
            try:
                self.delete(login)
            except Exception:
                _log.exception("unexpected: the deletion of %s wasn't finished", login)
                continue
            finished.append(login)
        return finished

    def _folders(self, login: str) -> list[Path]:
        """The folders an owner's apps and drafts are kept in, made or not."""
        return [self.apps.owner_folder(login), *self._drafts.owner_folders(login)]
