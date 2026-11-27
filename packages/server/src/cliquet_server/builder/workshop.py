"""What the builder's jobs share: the apps, drafts and previews, and the lookups they all use."""

import hashlib
from dataclasses import dataclass
from typing import Any

from cliquet.files import LOCK, MIGRATION
from cliquet.sandbox import version_id
from cliquet_server.apps import Apps, Migrated, PublishRefused, migrate
from cliquet_server.db import AppInfo
from cliquet_server.engine import SandboxEngine
from cliquet_server.executor import BuildProblems, SandboxError
from cliquet_server.settings import Settings

from .drafts import Drafts
from .errors import BuilderError, no_such_app
from .previews import Previews


@dataclass(frozen=True)
class Build:
    """A draft built in the sandbox: its version, manifest, engine and code, and the draft's
    code revision (`Drafts.code_revision`) read before its code was."""

    version: str
    manifest: dict[str, Any]
    engine: SandboxEngine
    code: dict[str, bytes]
    migration: bytes | None
    revision: int = -1

    @property
    def app_py(self) -> str:
        """A digest of app.py, which lays the screens out and runs their calls: what a shown
        screen names. Its tests and instructions don't change it."""
        return hashlib.sha256(self.code.get("app.py", b"")).hexdigest()[:16]


class Workshop:
    """The owner's apps, drafts and previews, and how a draft is found and built."""

    def __init__(self, apps: Apps, settings: Settings, drafts: Drafts, previews: Previews) -> None:
        self.apps = apps
        self.settings = settings
        self.drafts = drafts
        self.previews = previews

    def find(self, login: str, app: str) -> AppInfo:
        """The owner's app, or a BuilderError saying how to find or start one."""
        info = self.apps.database.app(login, app)
        if info is None:
            raise BuilderError(no_such_app(app))
        return info

    def ensure_draft(self, login: str, app: str, info: AppInfo | None = None) -> AppInfo:
        """The app, with a draft: a published app's draft starts from its live code."""
        info = info or self.find(login, app)
        if not self.drafts.exists(login, app) and info.published_at is not None:
            self.drafts.start(login, app, self.apps.repo(login, app).code())
        return info

    def changed(self, login: str, info: AppInfo) -> bool:
        """Whether the draft differs from the live version."""
        if info.published_at is None:
            return True
        if not self.drafts.exists(login, info.name):
            return False
        if self.drafts.migration(login, info.name) is not None:
            return True
        if self.drafts.restoring(login, info.name) is not None:
            return True
        return (
            version_id(self.drafts.code(login, info.name))
            != self.apps.repo(login, info.name).version()
        )

    def build(
        self,
        login: str,
        app: str,
        code: dict[str, bytes] | None = None,
        version: str = "",
        revision: int = -1,
    ) -> Build:
        """Build the draft's code in the owner's sandbox. Raises `BuildProblems`.

        `code`, its `version` and the code `revision` read before it are the draft's, when the
        caller has read them already.
        """
        if code is None:
            self.ensure_draft(login, app)
            revision = self.drafts.code_revision(login, app)
            code = self.drafts.code(login, app)
            version = version_id(code)
        files = dict(code)
        lock = self.drafts.lock_file(login, app)
        if lock is not None:
            files[LOCK] = lock
        migration = self.drafts.migration(login, app)
        if migration is not None:
            files[MIGRATION] = migration
        with self.apps.sandbox(login) as client:
            built = client.put_version(login, app, version, files)
        if built.lock != lock:
            # Under the draft's lock, which a publish holds while the app goes live.
            self.drafts.write(login, app, LOCK, built.lock)
        engine = SandboxEngine(
            client,
            login,
            app,
            version,
            built.manifest,
            lambda: files,
            used=lambda: self.apps.busy(login),
        )
        return Build(version, built.manifest, engine, code, migration, revision)

    def built(
        self,
        login: str,
        app: str,
        what: str,
        code: dict[str, bytes] | None = None,
        version: str = "",
        revision: int = -1,
    ) -> Build:
        """The draft, built, or a BuilderError that starts with `what`."""
        try:
            return self.build(login, app, code, version, revision)
        except BuildProblems as error:
            raise BuilderError("The app can't be built. Call check_app to see why.") from error
        except SandboxError as error:
            raise BuilderError(f"{what}: {error}") from error

    def current_data(self, login: str, app: str, manifest: dict[str, Any]) -> dict[str, bytes]:
        """The app's current data: the live data once published, else the draft's."""
        info = self.find(login, app)
        if info.published_at is not None:
            return self.apps.repo(login, app).data(manifest["files"])
        return self.drafts.data(login, app)

    def try_migration(
        self, login: str, app: str, build: Build
    ) -> tuple[Migrated | None, list[str], str | None]:
        """Run the draft's migration on a copy of all live data. Keeps nothing.

        It returns what the migration made, which a publish takes while the data is the same,
        the files it changed, and why it failed, if it did.
        """
        repo = self.apps.repo(login, app)
        base = repo.head()
        current = repo.data(repo.data_names(base), base)
        try:
            new = migrate(build.engine, login, current)
        except PublishRefused as error:
            return None, [], str(error)
        names = sorted(file for file in {*current, *new} if current.get(file) != new.get(file))
        return Migrated(base, new), names, None

    def build_url(self, app: str, page: str) -> str:
        """The address of one of a draft's pages, under /build."""
        return f"{self.settings.base_url}/build/{app}/{page}"

    def web_url(self, login: str, app: str) -> str:
        """The address of the app's own page."""
        return f"{self.settings.base_url}/apps/{login}/{app}"


def migration_changes(names: list[str]) -> str:
    """What a migration does to the data, in one sentence, from the files it changed."""
    return (
        f"The migration rewrites: {', '.join(names)}."
        if names
        else "The migration changes nothing."
    )
