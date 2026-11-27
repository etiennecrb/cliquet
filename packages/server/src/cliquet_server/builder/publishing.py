"""Checking a draft and publishing it: check_app, run_tests, publish_app and read_app_overview,
and what a new version changes."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cliquet.context import Caller
from cliquet.errors import shortened
from cliquet.files import declares, is_pattern
from cliquet.store import Entry, Moved
from cliquet_server import app_errors, sharing
from cliquet_server.app_auth import app_address, me_address, tokens_link
from cliquet_server.apps import (
    AppStarting,
    Migrated,
    PublishRefused,
    UnknownApp,
    left_behind,
    restore,
)
from cliquet_server.db import AppInfo
from cliquet_server.diffs import LONG_LINE, FileDiff, change_entry, read_diff
from cliquet_server.engine import AppFailed
from cliquet_server.executor import BuildProblems, SandboxError
from cliquet_server.manifest import label, rule_texts, title, tool_rows
from cliquet_server.repos import Publish, changed, remark_of

from .errors import BuilderError
from .files import MAX_READ_CHARS
from .workshop import Build, Workshop, migration_changes

# How an agent shows the person where the app is: what they open, and what they copy.
SHOW_ADDRESSES = (
    "Present web pages as Markdown links, and the MCP addresses, assistant_url and agent_url, "
    "as code to copy, never as links."
)

# What a publish answer adds when it ended the app's preview.
PREVIEW_ENDED = "The preview ended: call start_preview to try more."

# Why a publish refused: an edit to the code or the migration, made during its checks.
DRAFT_CHANGED = (
    "The draft changed while publishing, so nothing was published. Call publish_app again."
)


class _LiveUnloaded(BuilderError):
    """The live version can't be loaded, so what a new one changes isn't known."""

    def __init__(self, app: str, error: Exception) -> None:
        """Say why, for check_app, which can be called again, and for publish_app, which goes on."""
        self.unknown = (
            f"What changes isn't known: the live version of {app} can't be loaded ({error})."
        )
        super().__init__(f"{self.unknown} Try again in a minute.")


@dataclass(frozen=True)
class _Checked:
    """Every check publish runs, and what the version changes when they all pass."""

    checks: list[dict[str, Any]]
    build: Build | None
    changes: list[str] | None = None
    data: str | None = None
    # What the draft's migration made of the live data, which the publish takes.
    migrated: Migrated | None = None
    # The publish whose version the draft restores, if it restores one.
    restores: str | None = None

    @property
    def ok(self) -> bool:
        return all(check["ok"] for check in self.checks) and self.changes is not None


class Publishing:
    """The builder's tools that check a draft on the real data, publish it, and describe the
    live version."""

    def __init__(self, workshop: Workshop) -> None:
        self.workshop = workshop
        self.apps = workshop.apps
        self.drafts = workshop.drafts
        self.previews = workshop.previews

    def read_app_overview(self, login: str, app: str) -> dict[str, Any]:
        """One app: its web pages, MCP addresses, tools, rules, members, latest changes and
        errors."""
        info = self.workshop.find(login, app)
        if info.published_at is None:
            return {
                "app": app,
                "published": False,
                "note": "Not published yet. Nothing is live until you call publish_app.",
            }
        try:
            live = self.apps.live(login, app)
        except SandboxError as error:
            # Its tools and its history need the live app: its errors and why it can't start
            # don't, and they are what the owner needs then.
            kept = error.error_id
            return {
                "app": app,
                "published": True,
                "web_url": self.workshop.web_url(login, app),
                "download_url": self._download_url(login, app),
                "problem": f"The live version can't be started: {error}",
                "errors": app_errors.described(self.apps, login, app),
                # Cliquet's own failure keeps no error: the app's code isn't to blame.
                "next": "Call read_app_overview again in a minute."
                if kept is None
                else f"Read the start's error with read_error(app, {kept!r}), fix the draft, "
                "then check, test and publish it.",
            }
        manifest = live.runtime.manifest
        roles = manifest["roles"]
        owner = Caller(login, roles[0] if roles else None)
        addresses = self._addresses(login, app, roles)
        return {
            "app": app,
            "published": True,
            "version": live.version,
            "description": manifest["description"],
            **addresses,
            "tools": tool_rows(manifest),
            "rules": rule_texts(manifest),
            **self._people(login, app, roles),
            # The builder is the owner's, who sees every change.
            "latest_changes": _changes(live.runtime.history(owner)),
            # The calls the app's code failed, kept for its owner.
            "errors": app_errors.described(self.apps, login, app),
            # What restore_version takes, the live version first.
            "versions": [
                version_row(one) for one in self.apps.repo(login, app).publishes(limit=10)
            ],
            "next": f"The web pages: {_page_links(app, manifest, addresses)}. {SHOW_ADDRESSES}",
        }

    def check_app(self, login: str, app: str) -> dict[str, Any]:
        """Build the draft, then check its definition, current data and views.

        It also says what the version would change, as publish_app would. It runs no test. The
        preview builds the draft again at its next follow, even when its code didn't build.
        """
        self.previews.forget_failure(login, app)
        self._begin_live(login, app)
        try:
            build = self.workshop.build(login, app)
            info = self.workshop.find(login, app)
            manifest = build.manifest
            definition = {
                "name": manifest["name"],
                "description": manifest["description"],
                "accent": manifest.get("accent"),
                "icon": manifest.get("icon"),
                "roles": manifest["roles"],
                "files": manifest["files"],
                "tools": tool_rows(manifest),
                "rules": rule_texts(manifest),
            }
            restores = self.drafts.restoring(login, app)
            problems, data, _ = self._version_problems(login, app, build, info, restores)
        except BuildProblems as error:
            return {
                "ok": False,
                "problems": error.problems,
                "next": "Fix app.py, then call check_app again.",
            }
        except SandboxError as error:
            raise BuilderError(
                f"The sandbox can't be reached: {error}. Try again in a minute."
            ) from error
        ok = not problems
        answer: dict[str, Any] = {
            "ok": ok,
            "problems": problems,
            "definition": definition,
            "changes": None,
            "data": data,
            "next": "Run the tests with run_tests. Before publish_app, tell the person what "
            "changes and data say."
            if ok
            else "Fix the problems, then call check_app again.",
        }
        try:
            answer["changes"] = manifest_changes(self._live_manifest(login, app, info), manifest)
        except BuilderError as error:
            # A live-side problem: the draft is not to blame.
            answer["changes_unknown"] = str(error)
        if build.migration is not None:
            answer["migration"] = data
        if restores is not None:
            answer["restores"] = restores
        return answer

    def run_tests(self, login: str, app: str, only: str | None = None) -> dict[str, Any]:
        """Run the draft's tests in its own environment in the sandbox.

        It builds the draft for real, so the preview builds it again at its next follow, as
        after `check_app`.
        """
        self.previews.forget_failure(login, app)
        try:
            build = self.workshop.build(login, app)
            with self.apps.sandbox(login) as client:
                result = client.test(login, app, build.version, only)
        except BuildProblems as error:
            output = "The app can't be built:\n" + "\n".join(
                f"- {problem}" for problem in error.problems
            )
            return {
                "passed": False,
                "output": output,
                "next": "Call check_app, fix app.py, then run the tests again.",
            }
        except SandboxError as error:
            raise BuilderError(
                f"The sandbox can't be reached: {error}. Try again in a minute."
            ) from error
        return {
            "passed": result.passed,
            "output": result.output,
            "next": "Start a preview with start_preview to try the app, or call check_app and "
            "publish_app."
            if result.passed
            else "Read the output, fix the app or the tests, then run them again.",
        }

    def publish_app(self, login: str, app: str, remark: str) -> dict[str, Any]:
        """Run every check, and make the draft live if they all pass."""
        if not remark or len(remark) > 200 or "\n" in remark or "\r" in remark:
            raise BuilderError("The remark is one line of 1 to 200 characters.")
        before = self.workshop.find(login, app).published_at
        checked = self._checks(login, app)
        if not checked.ok:
            return {
                "app": app,
                "published": False,
                "checks": checked.checks,
                "next": "Fix this, then call publish_app again. Nothing was published.",
            }
        assert checked.build is not None
        # Held across the whole step, slow as it is: see `Drafts`.
        with self.drafts.locked(login, app):
            info = self.workshop.find(login, app)
            if info.published_at != before:
                # What the checks found to change is against a version no longer live.
                raise BuilderError(
                    f"{app} was published by another call during this one's checks, so nothing "
                    "was published. Call publish_app again."
                )
            if self.drafts.restoring(login, app) != checked.restores or self._draft_changed(
                login, app, checked.build
            ):
                # What the checks read isn't what the draft holds, and the draft is started
                # again from what goes live: the edit would be lost.
                raise BuilderError(DRAFT_CHANGED)
            data = self.drafts.data(login, app) if info.published_at is None else None
            try:
                self.apps.publish(
                    login,
                    app,
                    checked.build.code,
                    note=remark,
                    data=data,
                    migration=checked.build.migration,
                    migrated=checked.migrated,
                    restores=checked.restores,
                )
            except PublishRefused as error:
                raise BuilderError("Not published: " + str(error)) from error
            except Moved as error:
                raise BuilderError(
                    "The app's data changed while publishing, so nothing was published. "
                    "Call publish_app again."
                ) from error
            ended = self.previews.end_at_publish(login, app)
            self.drafts.start(login, app, self.apps.repo(login, app).code())
        try:
            live = self.apps.live(login, app)
        except UnknownApp as error:
            raise BuilderError(f"{app} went live, then was deleted.") from error
        except AppStarting as error:
            raise BuilderError(
                f"{app} went live, and is starting again. Don't publish again: call "
                "read_app_overview in a minute for its addresses."
            ) from error
        except SandboxError as error:
            raise BuilderError(
                f"{app} went live, but can't be started now: {error}. Don't publish again: call "
                "read_app_overview for its addresses and its errors."
            ) from error
        manifest = live.runtime.manifest
        addresses = self._addresses(login, app, manifest["roles"])
        restored = {} if checked.restores is None else {"restores": checked.restores}
        return {
            "app": app,
            "published": True,
            **restored,
            "version": live.version,
            **addresses,
            "changes": checked.changes,
            "data": checked.data if data is None else _first_data(data),
            "next": "Give the person the web page first: "
            f"{_page_links(app, manifest, addresses)}. {SHOW_ADDRESSES} "
            "To use it from their assistant, the person connects it once to assistant_url, "
            f"then asks it to open {app}. An agent that runs on its own, such as a routine, "
            "connects to agent_url with a token limited to this app, made at tokens_url. "
            "Both are MCP endpoints, not browser links. "
            "An agent with a session at agent_url must connect again to see the new tools, "
            "and an assistant opens the app again." + (f" {PREVIEW_ENDED}" if ended else ""),
        }

    def _draft_changed(self, login: str, app: str, build: Build) -> bool:
        """Whether the draft's code or migration differs from what the checks read. The code
        is read again only when it was written since."""
        if self.drafts.migration(login, app) != build.migration:
            return True
        if self.drafts.code_revision(login, app) == build.revision:
            return False
        return self.drafts.code(login, app) != build.code

    def _data_problems(
        self,
        login: str,
        app: str,
        build: Build,
        info: AppInfo,
        data: dict[str, bytes] | None = None,
    ) -> list[str]:
        """The problems in the current data and the views, as check_app reports them."""
        problems = []
        manifest = build.manifest
        if info.published_at is None:
            for name in self.drafts.data_names(login, app):
                if not declares(manifest["files"], name):
                    problems.append(
                        f"data/{name} isn't one of the app's files. "
                        "Declare it in App(files=[...]), or delete it."
                    )
        if data is None:
            data = self.workshop.current_data(login, app, manifest)
        try:
            checked = build.engine.check(datetime.now(UTC), data)
        except AppFailed as error:
            return [*problems, f"An invariant failed on the current data: {error}"]
        if checked.refusal is not None:
            problems.append(f"The current data breaks a rule:\n{checked.refusal}")
        problems += [str(problem) for problem in checked.views]
        return problems

    def _version_problems(
        self, login: str, app: str, build: Build, info: AppInfo, restores: str | None = None
    ) -> tuple[list[str], str, Migrated | None]:
        """The migration's, the data's and the views' problems, what publish does to the
        data, in one sentence, and what the migration made of the live data, if there is
        one. A draft that `restores` a version is checked on the data its publish puts
        back."""
        migrated = None
        if build.migration is not None:
            if info.published_at is None:
                problem = (
                    "migrate.py is for an app that is live. Before the first publish, "
                    "write the first data under data/ instead."
                )
                return (
                    [problem],
                    "migrate.py is only for an app that is live: see the problems.",
                    None,
                )
            migrated, names, problem = self.workshop.try_migration(login, app, build)
            if problem is not None:
                return [problem], "The migration doesn't pass yet: see the problems.", None
            data = migration_changes(names)
        elif info.published_at is None:
            data = _first_data(self.drafts.data_names(login, app))
        elif restores is not None:
            repo = self.apps.repo(login, app)
            head = repo.head()
            assert head is not None
            current = repo.data(repo.data_names(head), head)
            try:
                restored = restore(repo, restores, head, current, build.manifest["files"])
            except PublishRefused as error:
                return error.reasons, "The data can't be restored: see the problems.", None
            data = data_sentence(current, restored)
            return self._data_problems(login, app, build, info, restored), data, None
        else:
            repo = self.apps.repo(login, app)
            left = left_behind(repo.data_names(), build.manifest["files"])
            if left:
                return left, "This version drops data files: see the problems.", None
            data = "The live data is kept as it is."
        new = None if migrated is None else migrated.data
        return self._data_problems(login, app, build, info, new), data, migrated

    def _begin_live(self, login: str, app: str) -> None:
        """Begin the live version's start, if there is one, for `_live_manifest` to wait for
        later: it runs during the build and the tests, which use up the call's deadline."""
        if self.workshop.find(login, app).published_at is not None:
            self.apps.begin(login, app)

    def _live_manifest(self, login: str, app: str, info: AppInfo) -> dict[str, Any] | None:
        """The live version's manifest, or None before the first publish.

        A live version that can't be loaded raises a BuilderError that says so: it is never
        the draft's problem.
        """
        if info.published_at is None:
            return None
        try:
            # `_begin_live` began its start: one that failed since isn't tried again.
            return self.apps.live(login, app, again=False).runtime.manifest
        except (UnknownApp, SandboxError) as error:
            raise _LiveUnloaded(app, error) from error

    def _checks(self, login: str, app: str) -> _Checked:
        """The build, the tests, the migration, and the data and views, as publish runs them."""
        # Read before the code: a change to it since ends the restore, and is no part of it.
        restores = self.drafts.restoring(login, app)
        self._begin_live(login, app)
        try:
            build = self.workshop.build(login, app)
        except BuildProblems as error:
            failed = {"check": "The app builds", "ok": False, "detail": "\n".join(error.problems)}
            return _Checked([failed], None)
        except SandboxError as error:
            raise BuilderError(
                f"The sandbox can't be reached: {error}. Try again in a minute."
            ) from error
        checks = [{"check": "The app builds", "ok": True, "detail": ""}]
        try:
            with self.apps.sandbox(login) as client:
                run = client.test(login, app, build.version)
            info = self.workshop.find(login, app)
            problems, data, migrated = self._version_problems(login, app, build, info, restores)
        except SandboxError as error:
            raise BuilderError(
                f"The sandbox can't be reached: {error}. Try again in a minute."
            ) from error
        if not run.passed:
            checks.append({"check": "The tests pass", "ok": False, "detail": run.output[-3000:]})
            return _Checked(checks, build)
        checks.append({"check": "The tests pass", "ok": True, "detail": ""})
        if problems:
            checks.append(
                {
                    "check": "The data and the screens pass",
                    "ok": False,
                    "detail": "\n".join(problems),
                }
            )
            return _Checked(checks, build)
        checks.append({"check": "The data and the screens pass", "ok": True, "detail": ""})
        try:
            changes = manifest_changes(self._live_manifest(login, app, info), build.manifest)
        except _LiveUnloaded as error:
            # A live version that can't load, as after an SDK that refuses it, is fixed only
            # by publishing: what changes can't be known then.
            changes = [error.unknown]
        return _Checked(checks, build, changes, data, migrated, restores)

    def _addresses(self, login: str, app: str, roles: list[str]) -> dict[str, Any]:
        """Where the live app is reached: its web pages, the person's own endpoint, where their
        assistant opens it by name, its own endpoint, for an agent that runs on its own with a
        limited token, made at tokens_url, and its download."""
        base = self.workshop.settings.base_url
        page = self.workshop.web_url(login, app)
        return {
            "web_url": page,
            "web_urls": {role: f"{page}/{role}" for role in roles} if roles else {"-": f"{page}/-"},
            "assistant_url": me_address(base),
            "agent_url": app_address(base, login, app),
            "tokens_url": f"{base}{tokens_link(login, app)}",
            "download_url": self._download_url(login, app),
        }

    def _download_url(self, login: str, app: str) -> str:
        """Where the owner, the only one the builder serves, downloads the app with its
        history."""
        return f"{self.workshop.settings.base_url}/manage/{login}/{app}/download"

    def read_change(self, login: str, app: str, commit: str) -> dict[str, Any]:
        """What one change did to the published app's data, as the History tab shows it to
        the owner: its details, and each data file's diff."""
        if self.workshop.find(login, app).published_at is None:
            raise BuilderError(f"{app} isn't published yet, so its data has no history.")
        store = self.apps.repo(login, app).data_store()
        entry = change_entry(store, commit.strip())
        if entry is None:
            raise BuilderError(
                f"{app}'s history has no change {shortened(commit, 50)!r}. Pass a change's full "
                "commit id, as read_app_overview's latest_changes give it."
            )
        files, left_out = _file_changes(read_diff(store, entry.commit, max_chars=MAX_READ_CHARS))
        answer = {"app": f"{login}/{app}", **_change(entry), "files": files}
        if left_out:
            answer["cut"] = (
                f"The diffs are cut to fit the answer: {left_out:,} of their lines are left out."
            )
        return answer

    def _people(self, login: str, app: str, roles: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Who holds the app, by login, and the shares not yet accepted, by email, as
        `sharing.people` lists them. An app without roles names none."""
        members: dict[str, list[str]] = {}
        waiting: list[dict[str, Any]] = []
        for one in sharing.people(self.apps.database, login, app):
            if one.accepted:
                members.setdefault(one.who, []).append(one.role)
            else:
                waiting.append(
                    {"email": one.who, "role": one.role} if roles else {"email": one.who}
                )
        return {
            "members": [
                {"login": who, "roles": held} if roles else {"login": who}
                for who, held in members.items()
            ],
            "waiting": waiting,
        }


def _page_links(app: str, manifest: dict[str, Any], addresses: dict[str, Any]) -> str:
    """The app's web page as a Markdown link named by its title, then each role's page."""
    name = (title(manifest) or label(app)).replace("[", "\\[").replace("]", "\\]")
    links = [f"[{name}]({addresses['web_url']})"]
    if manifest["roles"]:
        links += [f"[{name} · {role}]({url})" for role, url in addresses["web_urls"].items()]
    return ", ".join(links)


def _first_data(names: Iterable[str]) -> str:
    """What the first publish does to the data, in one sentence."""
    first = sorted(names)
    return f"First data: {', '.join(first)}." if first else "The app starts with no data."


def version_row(one: Publish) -> dict[str, Any]:
    """One publish, as the builder's tools name a version: restore_version takes its version
    or its commit."""
    row = {"version": one.version, "commit": one.commit, "when": one.time, "note": one.note}
    return row if one.restores is None else {**row, "restores": one.restores}


def data_sentence(current: dict[str, bytes], restored: dict[str, bytes]) -> str:
    """What a restore does to the live data, in one sentence."""
    names = sorted(changed(current, restored))
    if not names:
        return "The live data is kept as it is."
    return f"The data from before the publishes it undoes is put back: {', '.join(names)}."


def _changes(entries: list[Entry]) -> list[dict[str, Any]]:
    """The ten latest changes, in the shape the builder's tools return."""
    return [_change(entry) for entry in entries[:10]]


def _change(entry: Entry) -> dict[str, Any]:
    """One change, by `app_history`'s keys: read_change takes its commit. A publish's remark is
    its note."""
    return {
        "commit": entry.commit,
        "time": entry.time,
        "caller": entry.caller,
        "role": entry.role,
        # The API token the change was made with, by its name then: the builder is the
        # owner's, who may read it.
        "agent": entry.agent,
        "action": entry.action,
        "remark": remark_of(entry),
        "files": entry.files,
    }


def _file_changes(files: list[FileDiff]) -> tuple[list[dict[str, Any]], int]:
    """What a change did to each data file, its diff as unified text with each line cut long
    marked, and how many of its lines are left out: the diffs share `MAX_READ_CHARS`. A binary
    file has its sizes."""
    rows: list[dict[str, Any]] = []
    for file in files:
        row: dict[str, Any] = {"file": file.name, "status": file.status}
        if file.binary:
            rows.append({**row, "bytes_before": file.before, "bytes_after": file.after})
            continue
        diff = "\n".join(line.text + (LONG_LINE if line.cut else "") for line in file.lines)
        rows.append({**row, "added": file.added, "removed": file.removed, "diff": diff})
    return rows, sum(file.left_out for file in files)


_KINDS = (("action", "actions"), ("query", "queries"), ("view", "views"))


def manifest_changes(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
    """What a new version changes for those who use the app, one sentence each."""
    changes: list[str] = []
    first = old is None
    if first:
        changes.append("This is the app's first version.")
        old = {
            "actions": [],
            "queries": [],
            "views": [],
            "invariants": [],
            "roles": [],
            "files": [],
        }

    for kind, key in _KINDS:
        old_tools = _by_name(old.get(key, []))
        new_tools = _by_name(new.get(key, []))
        for name, tool in new_tools.items():
            if name not in old_tools:
                changes.append(f"New {kind} {name}, for {_roles(tool.get('roles'))}.")
                continue
            was = old_tools[name]
            if tool.get("input_schema") != was.get("input_schema"):
                changes.append(f"The arguments of {kind} {name} changed.")
            if kind != "view" and tool.get("output_schema") != was.get("output_schema"):
                changes.append(f"The result of {kind} {name} changed.")
            if tool.get("roles") != was.get("roles"):
                changes.append(
                    f"{kind.capitalize()} {name} is now for {_roles(tool.get('roles'))}. "
                    f"It was for {_roles(was.get('roles'))}."
                )
        for name in old_tools:
            if name not in new_tools:
                changes.append(f"Removed {kind} {name}.")

    old_rules = _by_name(old.get("invariants", []))
    new_rules = _by_name(new.get("invariants", []))
    for name, rule in new_rules.items():
        if name not in old_rules:
            changes.append(f"New rule: {rule.get('description', '')}.")
        elif rule.get("description", "") != old_rules[name].get("description", ""):
            changes.append(
                f"Rule reworded: {rule.get('description', '')}. "
                f"It was: {old_rules[name].get('description', '')}."
            )
    for name, rule in old_rules.items():
        if name not in new_rules:
            changes.append(
                f"Removed rule: {rule.get('description', '')}. Changes it refused will now pass."
            )

    if not first:
        old_roles = old.get("roles", [])
        new_roles = new.get("roles", [])
        for role in new_roles:
            if role not in old_roles:
                changes.append(f"New role {role}.")
        for role in old_roles:
            if role not in new_roles:
                changes.append(f"Removed role {role}.")

        old_files = old.get("files", [])
        new_files = new.get("files", [])
        for name in new_files:
            if name not in old_files:
                what = "data files matching" if is_pattern(name) else "data file"
                changes.append(f"New {what} {name}.")
        for name in old_files:
            if name not in new_files:
                what = "the files matching " if is_pattern(name) else ""
                changes.append(f"The app no longer keeps {what}{name}.")

    if first:
        return changes
    if not changes:
        return ["No change to the tools, the rules, the roles or the data files."]
    return changes


def _by_name(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in items}


def _roles(roles: list[str] | None) -> str:
    return ", ".join(roles) if roles else "everyone"
