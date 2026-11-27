"""What the builder's tools do, with no MCP in sight.

`Builder` is the one object the MCP server and the web pages call. It keeps the apps and the
guides itself, and hands the rest to one collaborator per job: the draft's files
(`DraftFiles`), trying things on the preview (`Trying`), and checking and publishing
(`Publishing`). They share one `Workshop`.
"""

import importlib.resources
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal, get_args

from cliquet import icons, vocabulary
from cliquet.errors import shortened
from cliquet.scaffold import local_sdk, set_sdk_source, write_template
from cliquet.screenshot import Theme
from cliquet.tools import MAX_APP_NAME
from cliquet_server import app_errors
from cliquet_server.apps import Apps
from cliquet_server.db import APP_NAME, ERRORS_KEPT, ERRORS_KEPT_FOR, AppExists, AppInfo
from cliquet_server.executor import Picture
from cliquet_server.settings import Settings

from .drafts import Drafts, check_path
from .errors import BuilderError
from .files import READ_LINES, DraftFiles, LineRange
from .previews import Previews, PreviewSession
from .publishing import Publishing
from .restoring import Restoring
from .trying import Trying, ViewTried
from .uploads import Uploads
from .workshop import Workshop

# What read_example and read_guide take: their schemas list them.
Example = Literal["meeting-rooms", "environments", "ledger"]
Topic = Literal["start", "app", "tests", "views", "instructions", "publishing"]
EXAMPLES: tuple[str, ...] = get_args(Example)
TOPICS: tuple[str, ...] = get_args(Topic)


class Builder:
    """What the builder's tools do, for the owner whose login is given."""

    def __init__(self, apps: Apps, settings: Settings) -> None:
        self.apps = apps
        self.settings = settings
        self.drafts = Drafts.of(settings)
        self.previews = Previews()
        self.workshop = Workshop(apps, settings, self.drafts, self.previews)
        self.draft_files = DraftFiles(self.workshop)
        self.uploads = Uploads(settings.data_dir / "uploads", self.draft_files)
        self.trying = Trying(self.workshop)
        self.publishing = Publishing(self.workshop)
        self.restoring = Restoring(self.workshop, self.publishing)

    # The apps

    def read_app_overview(self, login: str, app: str) -> dict[str, Any]:
        """One app: its web pages, MCP addresses, tools, rules, members, latest changes and
        errors."""
        return self.publishing.read_app_overview(login, app)

    def read_change(self, login: str, app: str, commit: str) -> dict[str, Any]:
        """What one change did to the published app's data, for its owner: its diff."""
        return self.publishing.read_change(login, app, commit)

    def read_error(self, login: str, app: str, error: str) -> dict[str, Any]:
        """One error of the published app's code, in a call or in its start, kept for its
        owner: its whole record."""
        info = self.workshop.find(login, app)
        found = self.apps.database.error(login, info.name, error.strip(), self.apps.now())
        if found is None:
            raise BuilderError(
                f"{app} has no error with the id {shortened(error, 40)!r}. An error is kept "
                f"{ERRORS_KEPT_FOR.days} days, and an app's last {ERRORS_KEPT} at most: call "
                "read_app_overview to see the latest ones."
            )
        return app_errors.record(found)

    def create_app(self, login: str, app: str) -> dict[str, Any]:
        """Start a draft from the small template."""
        if not APP_NAME.fullmatch(app):
            raise BuilderError(
                f"An app's name is at most {MAX_APP_NAME} lower case letters, digits, - or _, "
                "starting with a letter. Choose a shorter or plainer name, such as reading-list."
            )
        most = self.settings.apps_per_owner
        if len(self.apps.database.apps(login)) >= most:
            raise BuilderError(
                f"You have {most} apps, the most an owner can have here. Change one of them "
                "instead, or ask the person whether to delete one they no longer need with "
                "delete_app."
            )
        files = self._template_files(app)
        try:
            self.apps.database.create_app(login, app, datetime.now(UTC))
        except AppExists:
            raise BuilderError(
                f"You already have an app named {app}. Change it with write_file, "
                "or choose another name."
            ) from None
        try:
            self.drafts.start(login, app, files)
        except BaseException:
            self.apps.database.delete_app(login, app)
            raise
        return {
            "app": app,
            "files": sorted(self.drafts.sizes(login, app)),
            "next": 'Call read_guide("app"), then change app.py with edit_file or write_file. '
            "Nothing is live until publish_app.",
        }

    def discard_draft(self, login: str, app: str) -> dict[str, Any]:
        """Put a published app's draft back to its live version."""
        info = self.workshop.find(login, app)
        if info.published_at is None:
            raise BuilderError(
                f"{app} was never published, so there is nothing to go back to: write app.py "
                "again to start over."
            )
        self.drafts.remove(login, app)
        return {"app": app, "next": "The draft is the live version again. Call list_files."}

    def delete_app(self, login: str, app: str, confirm: str) -> dict[str, Any]:
        """Delete an app, its data and its draft."""
        info = self.workshop.find(login, app)
        if confirm != app:
            raise BuilderError(f"To delete {app}, pass confirm={app!r}.")
        self.remove_app(login, app)
        return {"app": info.name, "deleted": True}

    def remove_app(self, login: str, app: str, *, in_sandbox: bool = True) -> None:
        """Remove an app, published or not, with its preview, its draft, its repository and
        its rows, the rows last. Not `in_sandbox`, its owner's sandbox is left alone, since
        the whole of it is destroyed next, as when an account is deleted."""
        if in_sandbox:
            # The sandbox first: one still starting leaves the app and its preview as they were.
            with self.apps.sandbox(login):
                pass
        # A publish holds the draft's lock until the app is live: delete it after.
        with self.drafts.locked(login, app):
            self.previews.stop(login, app)
            self.drafts.remove(login, app)
            self.apps.delete(login, app, in_sandbox=in_sandbox)

    # The guides

    def read_guide(self, topic: str = "start") -> str:
        """The guide for one topic."""
        if topic not in TOPICS:
            raise BuilderError(f"A guide topic is one of {', '.join(TOPICS)}, not {topic!r}.")
        path = importlib.resources.files("cliquet_server.builder") / "guides" / f"{topic}.md"
        return path.read_text(encoding="utf-8")

    def read_example(self, example: str, path: str | None = None) -> str:
        """List an example's files, or read one of them."""
        folder = self._example_folder(example)
        if path is None:
            return "\n".join(_example_names(folder))
        check_path(path)
        target = folder / path
        if not target.is_file():
            raise BuilderError(
                f"{path} isn't in the {example} example. It has:\n"
                + "\n".join(_example_names(folder))
            )
        text = target.read_text(encoding="utf-8")
        return set_sdk_source(text, None) if path == "app.py" else text

    def find_classes(self, text: str = "") -> str:
        """The classes a view may use whose name holds `text`: daisyUI's and Cliquet's."""
        return found_classes(text)

    def find_icons(self, text: str) -> str:
        """Lucide's icons whose name or tags hold `text`: for an app or a screen."""
        return found_icons(text)

    # The draft's files: `DraftFiles`

    def list_files(self, login: str, app: str) -> dict[str, Any]:
        """The draft's files, its live data, and whether it differs from the live version."""
        return self.draft_files.list_files(login, app)

    def read_file(
        self, login: str, app: str, path: str, offset: int = 1, limit: int = READ_LINES
    ) -> dict[str, Any]:
        """Read lines of a draft file, or of the live copy of a published app's data."""
        return self.draft_files.read_file(login, app, path, offset, limit)

    def write_file(self, login: str, app: str, path: str, content: str) -> dict[str, Any]:
        """Create or replace a draft file the owner may write."""
        return self.draft_files.write_file(login, app, path, content)

    def edit_file(
        self,
        login: str,
        app: str,
        path: str,
        old: str,
        new: str,
        replace_all: bool = False,
        lines: LineRange | None = None,
        size: int | None = None,
    ) -> dict[str, Any]:
        """Replace `old`, or a range of lines, by `new` in a draft file."""
        return self.draft_files.edit_file(login, app, path, old, new, replace_all, lines, size)

    def delete_file(self, login: str, app: str, path: str) -> dict[str, Any]:
        """Remove a draft file the owner may write."""
        return self.draft_files.delete_file(login, app, path)

    def ask_for_file(self, login: str, app: str, path: str) -> dict[str, Any]:
        """The link where the person uploads a data file into the draft, and what the upload
        panel shows."""
        return self.draft_files.ask_for_file(login, app, path)

    def upload_file(
        self,
        login: str,
        app: str,
        path: str,
        size: int,
        offset: int,
        data: str,
        upload: str | None = None,
    ) -> dict[str, Any]:
        """Take one part of a data file the upload panel sends; the last puts it in the draft."""
        return self.uploads.receive(login, app, path, size, offset, data, upload)

    def upload(self, info: AppInfo, path: str, source: BinaryIO, size: int) -> None:
        """Copy a data file the person sent from the upload page into the draft."""
        self.draft_files.upload(info, path, source, size)

    def uploaded(self, info: AppInfo) -> list[dict[str, Any]]:
        """The draft's data files, with their sizes, for the upload page."""
        return self.draft_files.uploaded(info)

    # The preview: `Trying`

    def start_preview(self, login: str, app: str) -> dict[str, Any]:
        """Run the draft on a new copy of the current data, in place of any preview open."""
        return self.trying.start_preview(login, app)

    def stop_preview(self, login: str, app: str) -> dict[str, Any]:
        """Close the app's preview, and throw its data away."""
        return self.trying.stop_preview(login, app)

    def draft_preview(self, info: AppInfo) -> tuple[PreviewSession, str | None] | None:
        """The app's preview for its web page and the page's calls, running the draft's code
        as it is now, and what the page says about it, if anything."""
        return self.trying.draft_preview(info)

    def take_screenshots(
        self,
        login: str,
        app: str,
        role: str | None = None,
        view: str | None = None,
        theme: Theme = "light",
    ) -> tuple[list[Picture], str | None]:
        """Take two screenshots of each draft view, or of one view, on the preview's data, in
        `theme`, with the preview's notice."""
        return self.trying.take_screenshots(login, app, role, view, theme)

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
        """Run a script on a draft view, in `theme`, then make its action calls again on the
        preview."""
        return self.trying.try_view(login, app, view, steps, role, width, theme)

    def show_preview(
        self,
        login: str,
        app: str,
        view: str,
        role: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Lay out a view on the preview, with its calls rewritten for `preview_call`, and the
        preview's notice."""
        return self.trying.show_preview(login, app, view, role, arguments)

    def preview_call(
        self,
        login: str,
        preview: str,
        app_py: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
        role: str | None = None,
    ) -> Any:
        """Run one call from a shown view on its preview, unless app.py changed since the one
        `app_py` names laid the view out."""
        return self.trying.preview_call(login, preview, app_py, tool, arguments, role)

    def try_call(
        self,
        login: str,
        app: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        """Run one action or query, or a built-in tool, on the preview."""
        return self.trying.try_call(login, app, tool, arguments, role)

    # Checks and publish: `Publishing`

    def check_app(self, login: str, app: str) -> dict[str, Any]:
        """Build the draft, then check its definition, current data and views."""
        return self.publishing.check_app(login, app)

    def run_tests(self, login: str, app: str, only: str | None = None) -> dict[str, Any]:
        """Run the draft's tests in its own environment in the sandbox."""
        return self.publishing.run_tests(login, app, only)

    def publish_app(self, login: str, app: str, remark: str) -> dict[str, Any]:
        """Run every check, and make the draft live if they all pass."""
        return self.publishing.publish_app(login, app, remark)

    # Restoring an earlier version: `Restoring`

    def restore_version(self, login: str, app: str, version: str) -> dict[str, Any]:
        """Put the code of an earlier version in the draft, to try it, then publish it."""
        return self.restoring.restore_version(login, app, version)

    def restore_and_publish(self, login: str, app: str, commit: str) -> dict[str, Any]:
        """Restore the version that `commit` published and publish it at once."""
        return self.restoring.restore_and_publish(login, app, commit)

    # Helpers

    def _template_files(self, name: str) -> dict[str, bytes]:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            write_template(path, name)
            return {
                file.relative_to(path).as_posix(): file.read_bytes()
                for file in sorted(path.rglob("*"))
                if file.is_file()
            }

    def _example_folder(self, example: str) -> Path:
        if example not in EXAMPLES:
            raise BuilderError(f"An example is one of {', '.join(EXAMPLES)}, not {example!r}.")
        sdk = local_sdk()
        # A checkout holds the SDK in packages/sdk, and the examples at its top.
        folder = sdk.parents[1] / "examples" / example if sdk is not None else None
        if folder is None or not folder.is_dir():
            raise BuilderError(
                f"The examples aren't installed on this server, so {example} can't be read. "
                'Follow read_guide("app") instead: it has the patterns an app needs.'
            )
        return folder


def _example_names(folder: Path) -> list[str]:
    """The files of an example, in the order the guide names them."""
    names = ["app.py", "instructions.md"]
    for part in ("tests", "data"):
        base = folder / part
        if base.is_dir():
            names += [
                f"{part}/{path.relative_to(base).as_posix()}"
                for path in sorted(base.rglob("*"))
                if path.is_file() and "__pycache__" not in path.parts
            ]
    return names


# At most this many icons in one answer: a word such as "arrow" holds hundreds.
_MAX_ICONS = 40
# The first tags of each, which Lucide lists from the closest.
_MAX_TAGS = 8


def found_icons(text: str) -> str:
    """The icons whose name or tags hold `text`, each with its tags: what `find_icons`
    answers."""
    names = icons.find_icons(text)
    if not names:
        return (
            f"No icon holds {text!r}. Look for one word for the app's subject, such as film, "
            "money, book, plant or calendar."
        )
    tags = icons.tags()
    lines = [f"{name}: {', '.join(tags[name][:_MAX_TAGS])}" for name in names[:_MAX_ICONS]]
    if len(names) > _MAX_ICONS:
        lines.append(f"... and {len(names) - _MAX_ICONS} more: look for a narrower word.")
    note = (
        f"Lucide's icons, each with the words it is filed under. Write the name as "
        f'App(..., icon="{names[0]}"), or in a view as <span data-icon="{names[0]}"></span>.'
    )
    return note + "\n" + "\n".join(lines)


def found_classes(text: str = "") -> str:
    """The classes whose name holds `text`, by component: what `find_classes` answers."""
    words = text.strip().lower()
    names = sorted(name for name in vocabulary.classes() if words in name)
    if not names:
        return (
            f"No class holds {text!r}. Look for a component's name, such as card, stat, badge, "
            f"table, list, tab, fieldset, input, btn, alert, or {vocabulary.PREFIX} for Cliquet's "
            "layout classes."
        )
    groups: dict[str, list[str]] = {}
    for name in names:
        bare = name.rpartition(":")[2]
        head = vocabulary.PREFIX if bare.startswith(vocabulary.PREFIX) else bare.split("-")[0]
        groups.setdefault(head, []).append(name)
    lines = [f"{head}: {' '.join(found)}" for head, found in groups.items()]
    if len(lines) > 60:
        lines = [*lines[:60], f"... and {len(lines) - 60} more groups: look for fewer words."]
    applies = " and ".join(
        f"{size}: applies from {width}px wide" for size, width in vocabulary.BREAKPOINTS.items()
    )
    never = ", ".join(f"{size}:" for size in vocabulary.NEVER_REACHED)
    note = (
        f"daisyUI's classes, and Cliquet's {vocabulary.PREFIX} classes. {applies}; {never} never "
        "apply on a screen."
    )
    return note + "\n" + "\n".join(lines)
