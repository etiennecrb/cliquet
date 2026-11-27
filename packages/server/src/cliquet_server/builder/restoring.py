"""Restoring an earlier version of an app: restore_version puts its code in the draft, and
publish_app makes it live, with every check."""

from typing import Any

from cliquet_server.repos import AppRepo, CantRestore, Publish

from .errors import BuilderError
from .publishing import Publishing, data_sentence, version_row
from .workshop import Workshop

# What refuses a restore while the draft holds changes of its own.
UNPUBLISHED = (
    "The draft of {app} has changes that aren't published. Publish them, or throw them away "
    "with discard_draft(app), then call restore_version again."
)
# What the older version may do to the data, as start_preview says it.
DROPS_FIELDS = (
    "The older version may drop fields of the data it doesn't know, the next time it writes them."
)


class Restoring:
    """The builder's tool that puts an earlier version in the draft, and the console's restore,
    which publishes it at once."""

    def __init__(self, workshop: Workshop, publishing: Publishing) -> None:
        self.workshop = workshop
        self.apps = workshop.apps
        self.drafts = workshop.drafts
        self.publishing = publishing

    def restore_version(self, login: str, app: str, version: str) -> dict[str, Any]:
        """Put the code of an earlier version in the draft, as the publish that made it live
        holds it, without its migration. Nothing is live until publish_app."""
        info = self.workshop.find(login, app)
        if info.published_at is None:
            raise BuilderError(f"{app} was never published, so it has no version to restore.")
        with self.drafts.locked(login, app):
            # A draft that holds a restore only is no change of the person's: it is replaced.
            if self.drafts.restoring(login, app) is None and self.workshop.changed(login, info):
                raise BuilderError(UNPUBLISHED.format(app=app))
            repo = self.apps.repo(login, app)
            publishes = repo.publishes()
            chosen = _find(publishes, version.strip())
            if chosen is None:
                raise BuilderError(
                    f"{app} has no version {version.strip()[:40]!r}. Call read_app_overview: its "
                    "versions name each one by its version and its commit."
                )
            if chosen is publishes[0]:
                raise BuilderError(
                    f"{chosen.commit[:7]} published the live version of {app}, so there is "
                    "nothing to restore. Name an earlier publish by its commit, from "
                    "read_app_overview's versions."
                )
            head = repo.head()
            assert head is not None
            try:
                restored = repo.restored_data(chosen.commit, head)
            except CantRestore as error:
                raise BuilderError(
                    f"{error} Undo those changes first, or publish a new version with a "
                    "migration instead. Nothing was changed."
                ) from error
            self.drafts.start(login, app, repo.code(chosen.commit), restores=chosen.commit)
        undone = publishes[: publishes.index(chosen)]
        current = repo.data(repo.data_names(head), head)
        return {
            "app": app,
            "restores": version_row(chosen),
            "undoes": [version_row(one) for one in undone],
            "data": data_sentence(current, restored),
            "files": sorted(self.drafts.sizes(login, app)),
            "next": "Call start_preview to try the older version on a copy of today's data, "
            "then check_app, and publish_app(app, remark) to make it live, with every check. "
            "discard_draft(app) leaves the live version as it is.",
        }

    def restore_and_publish(self, login: str, app: str, commit: str) -> dict[str, Any]:
        """Restore the version that `commit` published and publish it at once, as the console's
        "Restore this version" does. When it isn't published, the draft is the live version
        again, as it was.

        It is refused while the draft holds anything but the live version, a restore an
        assistant is trying included, so that it never throws away the draft.
        """
        info = self.workshop.find(login, app)
        if self.workshop.changed(login, info):
            raise BuilderError(
                f"The draft of {app} has changes that aren't published, or a version your "
                "assistant is trying. Publish them or discard them first, then restore again."
            )
        restored = self.restore_version(login, app, commit)["restores"]
        note = f"Restore version {restored['version']}"
        try:
            answer = self.publishing.publish_app(login, app, note)
        except BaseException:
            self._drop(login, app, restored["commit"])
            raise
        if not answer["published"]:
            self._drop(login, app, restored["commit"])
        return answer

    def _drop(self, login: str, app: str, commit: str) -> None:
        """Put the draft back to the live version, if it still holds the restore of `commit`."""
        with self.drafts.locked(login, app):
            if self.drafts.restoring(login, app) == commit:
                self.drafts.remove(login, app)


def preview_note(repo: AppRepo, commit: str) -> str:
    """What start_preview says of a draft that restores the version `commit` published."""
    publishes = repo.publishes()
    chosen = next(one for one in publishes if one.commit == commit)
    first = publishes[publishes.index(chosen) - 1].commit
    since = repo.changes_since(first)
    made = f"{since} change{'s were' if since != 1 else ' was'}"
    return (
        f"This draft restores version {chosen.version}, which {commit[:7]} published on "
        f"{chosen.time}. The preview runs it on a copy of today's data, as publish_app will "
        f"leave it. {made} made to the data since {first[:7]}, the first publish it "
        f"undoes. {DROPS_FIELDS}"
    )


def _find(publishes: list[Publish], version: str) -> Publish | None:
    """The publish a version's name names: its version, or its commit, in full or by its
    first 7 characters or more."""
    for one in publishes:
        if one.version == version:
            return one
    if len(version) >= 7:
        found = [one for one in publishes if one.commit.startswith(version)]
        if len(found) == 1:
            return found[0]
    return None
