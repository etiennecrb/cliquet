"""Trying a draft on its preview: start and stop it, call, click through, show and take
screenshots of its views."""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from mcp_types import TextContent

from cliquet.mcp_server import relayed
from cliquet.sandbox import version_id
from cliquet.screenshot import Theme
from cliquet.tools import ASK_FOR_FILE, BUILT_IN, NoSuchRole, app_role
from cliquet_server.apps import PublishRefused, SandboxStarting, restore
from cliquet_server.db import AppInfo
from cliquet_server.executor import BuildProblems, CaptureFailed, Picture, SandboxError

from .errors import BuilderError
from .previews import (
    DATA_CHANGED,
    MIGRATION_CHANGED,
    PREVIEW_ROUTES,
    PreviewEnded,
    PreviewSession,
    migration_digest,
    preview_fixed,
    rewrite_calls,
)
from .restoring import preview_note
from .workshop import Build, Workshop, migration_changes

NO_PREVIEW = "No preview is running for {app}. Call start_preview first."
ENDED = "This preview has ended. Show the view again with show_preview."
# What a click says on a screen laid out by code the draft no longer has. The person reads it.
OUT_OF_DATE = (
    "The draft changed since this view was shown, and this click wasn't made. "
    "Ask your assistant to show the view again."
)
# Why a click on a built-in tool that writes isn't made again on the preview.
NOT_MADE_AGAIN = (
    "It wasn't made again: the commits and transactions it names are those of the sandbox's "
    "copy, not the preview's. Make it with try_call."
)
# The built-in tools that change data: undo, and the transactions.
_WRITES = frozenset(name for name, tool in BUILT_IN.items() if tool.writes)


@dataclass(frozen=True)
class ViewTried:
    """What `try_view` gave: the sandbox's picture, calls and error, and, for each of its
    calls that change data, whether it was made again on the preview (`made`), whether that
    was kept (`ok`), and its answer or why it wasn't made (`text`)."""

    png: bytes
    calls: list[dict[str, Any]]
    error: str | None
    kept: list[dict[str, Any]]
    # How tall the view's screen is, as the sandbox says, when it says.
    height: int | None
    # What to say about the preview, if anything: that the data or migrate.py changed.
    notice: str | None = None


class Trying:
    """The builder's tools that work on a draft's preview."""

    def __init__(self, workshop: Workshop) -> None:
        self.workshop = workshop
        self.apps = workshop.apps
        self.drafts = workshop.drafts
        self.previews = workshop.previews

    def start_preview(self, login: str, app: str) -> dict[str, Any]:
        """Run the draft on a new copy of the current data, in place of any preview open."""
        info = self.workshop.find(login, app)
        build = self.workshop.built(login, app, "The preview can't be started")
        fingerprint = self._fingerprint(info, login, app)
        try:
            data, migrated = self.trial_data(build, login, app)
            self.previews.start(login, app, build, data, self._app_py(login, app), fingerprint)
        except SandboxError as error:
            raise BuilderError(f"The preview can't be started: {error}") from error
        started: dict[str, Any] = {
            "app": app,
            "files": [
                {"path": f"data/{name}", "size": len(content)}
                for name, content in sorted(data.items())
            ],
            "link": self.workshop.build_url(app, "preview"),
            "next": "Try calls with try_call, click through a view with try_view, take "
            "screenshots, or show a view to the person with show_preview. Give them the link to "
            "click through the draft in their browser.",
        }
        restores = self.drafts.restoring(login, app)
        if restores is not None and build.migration is None:
            started["restore"] = preview_note(self.apps.repo(login, app), restores)
        if migrated is not None:
            started["migration"] = (
                "The draft's migrate.py ran on the preview's copy of the data. "
                f"{migration_changes(migrated)} The live data is unchanged: publish_app runs "
                "the migration on it."
            )
        return started

    def stop_preview(self, login: str, app: str) -> dict[str, Any]:
        """Close the app's preview, and throw its data away."""
        self.workshop.find(login, app)
        stopped = self.previews.stop(login, app)
        return {
            "app": app,
            "stopped": stopped,
            "next": "The preview is closed. Call start_preview to start another."
            if stopped
            else f"No preview was running for {app}.",
        }

    def draft_preview(self, info: AppInfo) -> tuple[PreviewSession, str | None] | None:
        """The app's preview for its web page and the page's calls, running the draft's code
        as it is now, and what the page says about it, if anything.

        It builds the draft, but never starts a preview. When the draft doesn't build, the
        preview keeps the last version that did, and the page says why.
        """
        login, app = info.login, info.name
        session = self.previews.find(login, app)
        if session is None:
            return None
        try:
            self._follow(session, "The draft can't be built", info)
        except BuilderError as error:
            return session, f"{error} The preview runs the last version that built."
        except SandboxStarting:
            return session, (
                "Your sandbox is still starting, after a deploy. The preview runs the last "
                "version that built until it is ready."
            )
        return session, self._notice(session, info, login, app)

    def take_screenshots(
        self,
        login: str,
        app: str,
        role: str | None = None,
        view: str | None = None,
        theme: Theme = "light",
    ) -> tuple[list[Picture], str | None]:
        """Take two screenshots of each draft view, or of one view, on the preview's data, in
        `theme`, and say what the preview's notice says, if anything."""
        session, build, notice = self._preview(login, app, "The screenshots can't be taken")
        chosen = acting_role(build.manifest, role)
        try:
            with self.apps.sandbox(login) as client:
                with self._on(session):
                    data = session.data()
                pictures = client.pictures(
                    login, app, build.version, chosen, data, [view] if view else None, theme
                )
        except CaptureFailed as error:
            then = _next_picture(app, chosen, view, theme)
            raise BuilderError(f"The screenshots can't be taken: {error}. {then}") from error
        except SandboxError as error:
            raise BuilderError(f"The screenshots can't be taken: {error}") from error
        return pictures, notice

    def try_view(
        self,
        login: str,
        app: str,
        view: str,
        steps: list[dict[str, Any]],
        role: str | None = None,
        width: int = 768,
        theme: Theme = "light",
    ) -> ViewTried:
        """Run a script on a draft view in the sandbox, on a copy of the preview's data, drawn
        in `theme`, then make its action calls again on the preview, which keeps what they
        change."""
        if not 320 <= width <= 1280:
            raise BuilderError("A view is tried at a width from 320 to 1280 pixels.")
        session, build, notice = self._preview(login, app, "The view can't be tried")
        chosen = acting_role(build.manifest, role)
        with self._on(session):
            sent = session.data()
        try:
            with self.apps.sandbox(login) as client:
                scripted = client.script(
                    login, app, build.version, chosen, sent, view, steps, width, theme
                )
        except SandboxError as error:
            raise BuilderError(f"The view can't be tried: {error}") from error
        kept = self._replay(session, build, chosen, scripted.calls)
        return ViewTried(
            scripted.png, scripted.calls, scripted.error, kept, scripted.height, notice
        )

    def show_preview(
        self,
        login: str,
        app: str,
        view: str,
        role: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Lay out a view on the preview, with its calls rewritten for `preview_call`, and say
        what the preview's notice says, if anything."""
        session, build, notice = self._preview(login, app, "The view can't be shown")
        chosen = acting_role(build.manifest, role)
        views = [one["name"] for one in build.manifest["views"]]
        if view not in views:
            raise BuilderError(
                f"{app} has no view {view}. Its views are: {', '.join(views) or 'none'}."
            )
        with self._on(session):
            result = session.call(chosen, view, dict(arguments or {}), draws_screens=True)
        if result.is_error:
            raise BuilderError(_result_text(result))
        assert result.structured_content is not None
        return rewrite_calls(result.structured_content, session.id, build.app_py, chosen), notice

    def preview_call(
        self,
        login: str,
        preview: str,
        app_py: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
        role: str | None = None,
    ) -> Any:
        """Run one call from a shown view on its preview, which runs the draft as it is now, or
        the last version that built: to an action, or to a view, which comes back laid out
        again with its calls going to `preview_call`.

        When app.py changed since the one `app_py` names laid the screen out, a call to an
        action or a query isn't made: the screen may not fit the new code. A view is laid out
        again by the new code, so the screen is shown again, unless the screen's role is gone.
        """
        session = self.previews.get(preview)
        if session is None or session.login != login:
            raise BuilderError(ENDED)
        try:
            build = self._follow(session, "The call can't be made")
        except BuilderError:
            # As on the draft's page. The person can't fix the draft: the assistant's own
            # tools say why it doesn't build.
            build = session.build
        view = _is_view(build.manifest, tool)
        stale = app_py != build.app_py
        if stale and not view:
            raise BuilderError(OUT_OF_DATE)
        try:
            chosen = acting_role(build.manifest, role)
        except BuilderError:
            if stale:
                raise BuilderError(OUT_OF_DATE) from None
            raise
        with self._on(session, ENDED):
            result = session.call(chosen, tool, dict(arguments or {}), draws_screens=True)
        # Only a view's answer is a screen: any other is the app's own data, left as it is.
        if not view:
            return result
        return relayed(result, PREVIEW_ROUTES, preview_fixed(session.id, build.app_py, chosen))

    def try_call(
        self,
        login: str,
        app: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        """Run one action or query, or a built-in tool, on the preview, which keeps what it
        changes. The answer has the preview's notice, if it has one."""
        session, build, notice = self._preview(login, app, "The call can't be tried")
        chosen = acting_role(build.manifest, role)
        _not_a_view(build.manifest, tool)
        _no_file(build.manifest, tool, arguments or {})
        with self._on(session):
            result = session.call(chosen, tool, dict(arguments or {}))
        answer = {
            "ok": not result.is_error,
            "text": _result_text(result),
            "result": result.structured_content,
            "note": "On the preview: nothing is live.",
        }
        return {**answer, "notice": notice} if notice is not None else answer

    def trial_data(
        self, build: Build, login: str, app: str
    ) -> tuple[dict[str, bytes], list[str] | None]:
        """The current data, as the draft's migration leaves it when there is one, or as the
        restore it holds leaves it, and the files the migration changed, or None without one."""
        if build.migration is None:
            restores = self.drafts.restoring(login, app)
            if restores is None:
                return self.workshop.current_data(login, app, build.manifest), None
            repo = self.apps.repo(login, app)
            head = repo.head()
            assert head is not None
            current = repo.data(repo.data_names(head), head)
            try:
                restored = restore(repo, restores, head, current, build.manifest["files"])
            except PublishRefused as error:
                raise BuilderError(f"The preview can't be started: {error}") from error
            return restored, None
        migrated, changed, problem = self.workshop.try_migration(login, app, build)
        if problem is not None:
            raise BuilderError(f"The migration didn't pass. Call check_app to see why: {problem}")
        assert migrated is not None
        return migrated.data, changed

    def _session(self, login: str, app: str) -> tuple[AppInfo, PreviewSession | None]:
        """The app, and its preview if one is running."""
        info = self.workshop.find(login, app)
        return info, self.previews.find(login, app)

    def _follow(self, session: PreviewSession, what: str, info: AppInfo | None = None) -> Build:
        """The draft as it is now, which the preview then runs.

        Its code is read only when the builder wrote it since the preview's build was read,
        and the sandbox builds it only when it differs from what the preview runs. While the
        code is unchanged, the draft is too: starting or removing one counts as a write.

        Code that didn't build isn't built again until it is written again or checked: the
        preview says why from what it kept. A sandbox that failed is asked again.
        """
        login, app = session.login, session.app
        current = session.build
        revision = self.drafts.code_revision(login, app)
        if revision == current.revision:
            return current
        failure, checks = session.failure, session.checks
        if failure is not None and failure[0] == revision:
            raise BuilderError(failure[1])
        self.workshop.ensure_draft(login, app, info)
        revision = self.drafts.code_revision(login, app)
        code = self.drafts.code(login, app)
        version = version_id(code)
        if version == current.version:
            build = replace(current, revision=revision)
        else:
            try:
                build = self.workshop.built(login, app, what, code, version, revision)
            except BuilderError as error:
                if isinstance(error.__cause__, BuildProblems) and session.checks == checks:
                    session.failure = (revision, str(error))
                raise
        session.follow(build)
        return session.build

    def _preview(self, login: str, app: str, what: str) -> tuple[PreviewSession, Build, str | None]:
        """The app's preview, running the draft as it is now, and what to say after a tool
        that used it: that the current data, or migrate.py, changed since it started."""
        info, session = self._session(login, app)
        if session is None:
            raise BuilderError(NO_PREVIEW.format(app=app))
        build = self._follow(session, what, info)
        return session, build, self._notice(session, info, login, app)

    @contextmanager
    def _on(self, session: PreviewSession, ended: str | None = None) -> Generator[None]:
        """Turn a preview that ended during a call into what the assistant reads."""
        try:
            yield
        except PreviewEnded as error:
            raise BuilderError(ended or NO_PREVIEW.format(app=session.app)) from error
        except NoSuchRole as error:
            raise BuilderError(
                f"{error.sentence} Call read_file to see its roles in app.py."
            ) from error

    def _replay(
        self, session: PreviewSession, build: Build, role: str | None, calls: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Make a script's action calls again on the preview, in order, as `try_call` makes
        one: each is a write of its own, through the invariants.

        Only the draft's actions, by its own manifest: a query changes nothing. A call the
        sandbox refused changed nothing there, so it isn't made again. Each is made with the
        arguments it had, at the time it is made again. A click on a built-in tool that writes,
        an undo or a transaction's, is listed as not made again: the commits and transactions
        it names are the sandbox copy's.

        When the preview ends, or a new version of the draft drops the role, the calls kept
        so far stay kept, that call says why it wasn't made, and the rest aren't made.
        """
        actions = {action["name"] for action in build.manifest["actions"]}
        kept: list[dict[str, Any]] = []
        for number, call in enumerate(calls, 1):
            tool = call["tool"]
            if call["error"] is not None or tool not in actions | _WRITES:
                continue
            answer = {"call": number, "tool": tool}
            if tool in _WRITES:
                kept.append({**answer, "made": False, "ok": False, "text": NOT_MADE_AGAIN})
                continue
            try:
                with self._on(session):
                    result = session.call(role, tool, dict(call["arguments"]))
            except BuilderError as error:
                text = f"It wasn't made: {error}"
                kept.append({**answer, "made": False, "ok": False, "text": text})
                break
            text = _result_text(result)
            kept.append({**answer, "made": True, "ok": not result.is_error, "text": text})
        return kept

    def _notice(self, session: PreviewSession, info: AppInfo, login: str, app: str) -> str | None:
        """What to say when the preview no longer starts from what the draft would start it."""
        notices = []
        if session.fingerprint != self._fingerprint(info, login, app):
            notices.append(DATA_CHANGED)
        if session.migration != migration_digest(self.drafts.migration(login, app)):
            notices.append(MIGRATION_CHANGED)
        return " ".join(notices) or None

    def _fingerprint(self, info: AppInfo, login: str, app: str) -> str:
        """What names the current data: the repository's head, or how many times the builder
        wrote the draft's data."""
        if info.published_at is not None:
            return "head:" + (self.apps.repo(login, app).head() or "")
        return f"draft:{self.drafts.data_revision(login, app)}"

    def _app_py(self, login: str, app: str) -> Path:
        return self.drafts.folder(login, app) / "app.py"


def acting_role(manifest: dict[str, Any], role: str | None) -> str | None:
    """The role to act with, or a BuilderError saying what to pass: the only role when the
    app has one and none is given."""
    name, roles = manifest["name"], manifest["roles"]
    if not roles and role is not None:
        raise BuilderError(f"{name} has no roles: leave role out.")
    if role is None and len(roles) > 1:
        raise BuilderError(f"{name} has several roles: {', '.join(roles)}. Pass one as role.")
    try:
        return app_role(manifest, roles[0] if role is None and roles else role)
    except NoSuchRole as error:
        raise BuilderError(error.sentence) from None


def _result_text(result: Any) -> str:
    """The text of an MCP tool result."""

    return "\n".join(part.text for part in result.content if isinstance(part, TextContent))


def _is_view(manifest: dict[str, Any], name: str) -> bool:
    """Whether `name` is one of the app's views."""
    return any(view["name"] == name for view in manifest["views"])


def _not_a_view(manifest: dict[str, Any], name: str) -> None:
    """Refuse a view's name: a call on the preview reaches the app's actions and queries, and
    the built-in tools the role gets. A view is laid out with try_view or show_preview."""
    if _is_view(manifest, name):
        raise BuilderError(
            f"{name} is a view, not an action or a query. Click through it with try_view, or "
            "show it with show_preview."
        )


def _no_file(manifest: dict[str, Any], name: str, arguments: dict[str, Any]) -> None:
    """Refuse a call that gives an action a file, or asks for one: the preview takes no
    upload, and the app's tests give an action its file."""
    actions = {spec["name"]: spec for spec in manifest["actions"]}
    argument = actions.get(name, {}).get("file_argument")
    if name == ASK_FOR_FILE or (argument is not None and arguments.get(argument) is not None):
        raise BuilderError(
            "try_call can't give an action a file: the preview takes no upload. Try it in the "
            "app's tests instead, where `call` takes a file's bytes or a Path, with run_tests."
        )


def _next_picture(app: str, role: str | None, view: str | None, theme: Theme) -> str:
    """What to call when a view's picture can't be taken."""
    tried = try_view_call(app, view or "<view>", role, 390, theme)
    if view is None:
        as_role = f', role="{role}"' if role else ""
        alone = f'take_screenshots(app="{app}"{as_role}, view="<view>"{_as_theme(theme)})'
        return f"Take that view alone with {alone}, or look at it as wide as a phone with {tried}."
    return f"Look at it as wide as a phone with {tried}."


def try_view_call(
    app: str, view: str, role: str | None, width: int, theme: Theme, steps: str = "[]"
) -> str:
    """The try_view call that draws this view for this role at this width, in this theme."""
    as_role = f', role="{role}"' if role else ""
    return (
        f'try_view(app="{app}", view="{view}", steps={steps}{as_role}, width={width}'
        f"{_as_theme(theme)})"
    )


def _as_theme(theme: Theme) -> str:
    """A call's theme argument, left out for the light theme, which is the default."""
    return "" if theme == "light" else f', theme="{theme}"'


def cut_note(
    app: str,
    view: str,
    role: str | None,
    width: int,
    theme: Theme,
    height: int | None,
    shown: int,
) -> str:
    """What to say of a picture that shows only the top of its screen, or nothing."""
    if height is None or not 0 < shown < height:
        return ""
    tried = try_view_call(app, view, role, width, theme, "[...]")
    return (
        f"The picture is cut: it shows the top {shown:,} of the screen's {height:,} pixels. "
        f"To see the rest, call {tried} with steps that click a tab or a link to the part you "
        "want. A screen this long is hard to use: split it into tabs, steps or views."
    )
