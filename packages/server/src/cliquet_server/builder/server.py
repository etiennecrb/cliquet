"""The builder's tools, thin over `Builder`, served on the person's endpoint to the people
who may build: `add_builder` adds them to a FastMCP server, and `BuildersOnly` keeps them to
those people."""

import json
from collections.abc import Callable, Collection, Sequence
from functools import partial, wraps
from typing import Annotated, Any

import anyio
import mcp_types as mt
from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.resources import Resource, ResourceResult
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool, ToolResult
from fastmcp.utilities.types import Image
from mcp_types import CallToolRequestParams, CallToolResult, TextContent
from pydantic import Field

from cliquet import views
from cliquet.errors import described, report_unexpected, shortened
from cliquet.screens import FORMAT
from cliquet.screenshot import Theme
from cliquet.tools import MAX_APP_NAME
from cliquet_server.apps import Apps, AppStarting, SandboxStarting
from cliquet_server.arguments import App, Arguments, Role
from cliquet_server.callers import caller, draws_screens, token_newcomer
from cliquet_server.db import Right, may
from cliquet_server.executor import png_height
from cliquet_server.sandbox_time import OverBudget
from cliquet_server.settings import Settings
from cliquet_server.tooling import DESTROYS, READS, WRITES, answer, idempotent

from .core import Builder, Example, Topic
from .errors import BuilderError
from .files import MAX_READ_CHARS, MAX_READ_LINES, READ_LINES, LineRange
from .panel import panel_html, panel_uri, panel_uris
from .trying import cut_note

# Follows the person's endpoint's own instructions, for a person who may build.
INSTRUCTIONS = """\
The person may also build apps here. The builder's tools work on a draft and its preview: \
nothing is live until publish_app. Start with read_guide("start"). try_call, try_view, \
show_preview and take_screenshots use the preview, which start_preview starts; \
read_app_data, write_app_data and show_app_view use the live app's data. When the person has \
data of their own, call ask_for_file; never retype it with write_file. Publish an app, delete \
one, or discard a draft only once the person agreed: before a publish, show them what \
check_app says it changes."""

# What a person who may not build is told when they call one of the builder's tools.
NOT_A_BUILDER = "Building apps isn't open to you on this server: ask its operator."

# The arguments several of the builder's tools share. A path is one `check_path` takes.
DraftPath = Annotated[
    str, Field(description="app.py, instructions.md, tests/<name>.py, migrate.py or data/<name>.")
]
View = Annotated[str, Field(description="A view's name.")]
# A try_view's steps, as the views guide tells them.
STEPS = (
    'Each is {"click": label}, {"fill": label, "with": text}, '
    '{"choose": label, "option": text} or {"wait": ms}.'
)


# What each tool does, by `cliquet_server.tooling`'s hints. A tool that may remove what the
# person keeps, a draft's file, the live version or its data, is destructive. A preview is a
# throwaway copy, so what a tool changes or drops on one isn't. Building the draft writes its
# app.py.lock, so a tool that builds it writes. Reading a published app's draft may start it
# from the live code, which changes nothing the person sees, so a tool that does still reads.


def builder_enabled(settings: Settings) -> bool:
    """True when this server has a way to know who is building."""
    return settings.dev_login is not None or settings.auth == "oidc"


# The tools that run in the owner's sandbox long enough to count against its runs per hour:
# each builds the draft, runs its tests, or draws in the sandbox's browser. Trying a call on a
# running preview is a call like any other.
SANDBOX_RUNS = (
    "publish_app",
    "check_app",
    "start_preview",
    "run_tests",
    "take_screenshots",
    "try_view",
)
# The tools that run in the owner's sandbox: refused once the month's sandbox time is used.
# The others, deleting an app included, read and write the server's own files.
IN_SANDBOX = frozenset((*SANDBOX_RUNS, "try_call", "show_preview", "preview_call"))


class BuildersOnly(Middleware):
    """Show and run the builder's tools, `tools`, and its panels only for a person who
    may build, as the database says at each request, and append the builder's instructions
    to the server's for them. Any other call of one is refused. A newcomer who will build
    once they have a login sees them too, and each refuses them until then.

    Each call of a builder's tool has one deadline for every start it waits for,
    `Apps.builder_wait` from when it begins: a call that waits for its sandbox, then for its
    app, still answers before an MCP client gives up on it. One that runs in the owner's
    sandbox, `IN_SANDBOX`, is refused once the month's sandbox time that holds it is used.
    """

    def __init__(self, apps: Apps, tools: Collection[str], instructions: str) -> None:
        self.apps, self.tools, self.instructions = apps, frozenset(tools), instructions

    def _builds(self) -> bool:
        """Whether the person behind the request may build, or will once a newcomer chooses
        a login: never when there is nobody."""
        newcomer = token_newcomer()
        if newcomer is not None:
            return newcomer.builds
        try:
            # One who must verify their email still sees the tools, each refusing them.
            return may(caller(self.apps, listing=True), Right.BUILD)
        except ToolError:
            return False

    def _instructed(self, given: str | None) -> str | None:
        """The server's instructions, with the builder's after them for a builder."""
        if not self._builds():
            return given
        return f"{given}\n\n{self.instructions}" if given else self.instructions

    async def on_initialize(
        self,
        context: MiddlewareContext[mt.InitializeRequest],
        call_next: CallNext[mt.InitializeRequest, mt.InitializeResult | None],
    ) -> mt.InitializeResult | None:
        """The answer to a client that opens a session, as before 2026-07-28."""
        result = await call_next(context)
        if result is None:
            return None
        return result.model_copy(update={"instructions": self._instructed(result.instructions)})

    async def on_discover(
        self,
        context: MiddlewareContext[mt.DiscoverRequest],
        call_next: CallNext[mt.DiscoverRequest, mt.DiscoverResult | dict[str, Any]],
    ) -> mt.DiscoverResult | dict[str, Any]:
        """The answer to a client that asks what the server is, from 2026-07-28."""
        result = await call_next(context)
        if not isinstance(result, mt.DiscoverResult):
            return result
        return result.model_copy(update={"instructions": self._instructed(result.instructions)})

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Every tool for a builder, and the others only for anyone else."""
        listed = await call_next(context)
        if self._builds():
            return listed
        return [tool for tool in listed if tool.name not in self.tools]

    async def on_list_resources(
        self,
        context: MiddlewareContext[mt.ListResourcesRequest],
        call_next: CallNext[mt.ListResourcesRequest, Sequence[Resource]],
    ) -> Sequence[Resource]:
        """Every resource for a builder, and all but the builder's panels for anyone else."""
        listed = await call_next(context)
        if self._builds():
            return listed
        return [resource for resource in listed if str(resource.uri) not in panel_uris()]

    async def on_read_resource(
        self,
        context: MiddlewareContext[mt.ReadResourceRequestParams],
        call_next: CallNext[mt.ReadResourceRequestParams, ResourceResult],
    ) -> ResourceResult:
        """Read the resource, unless it is a builder's panel and the person may not build."""
        if str(context.message.uri) in panel_uris() and not self._builds():
            raise ResourceError(NOT_A_BUILDER)
        return await call_next(context)

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Run a builder's tool for a builder, within its deadline, which its thread, if any,
        inherits, and refuse it to anyone else. Any other tool runs as it is."""
        if context.message.name not in self.tools:
            return await call_next(context)
        # A token that names no person is refused in its own words, such as a newcomer's, who
        # chooses a login first.
        person = caller(self.apps)
        if not may(person, Right.BUILD):
            raise ToolError(NOT_A_BUILDER)
        over = self.apps.over_budget(person.login) if context.message.name in IN_SANDBOX else None
        if over is not None:
            raise ToolError(str(over))
        with self.apps.builder_call():
            return await call_next(context)


def owned_app(apps: Apps, app: str) -> tuple[str, str]:
    """The caller's login and the name of their app, named alone or as their own owner/name:
    an app of anyone else's is refused."""
    mine = caller(apps).login
    owner, slash, name = app.strip().rpartition("/")
    if not slash:
        return mine, name
    if owner != mine:
        raise ToolError(
            f"Only {owner} builds {app}. Name one of your own apps, alone or as "
            f"{mine}/<name>: list_apps lists them."
        )
    return mine, name


def builder_tool(
    mcp: FastMCP,
    names: set[str],
    refusals: tuple[type[Exception], ...] = (),
    output_schema: Any = None,
    **options: Any,
) -> Callable[[Any], Any]:
    """Register a tool on `mcp`, kept among the builder's, `names`, for `BuildersOnly`, and
    answer what its body raises as the assistant reads it: a refusal, a `BuilderError` or one
    of `refusals`, in its own words, a start it waited for too long in the start's, and anything
    else as a crash. An object's schema would say only that it is one, so a tool that answers
    one leaves it out: its answer still carries the object as structured content. Its
    description, its docstring's unless given, is one paragraph: a docstring's line breaks are
    only the source's."""

    def named(function: Any) -> Any:
        name = options.get("name") or function.__name__
        names.add(name)

        @wraps(function)
        async def answered(*args: Any, **kwargs: Any) -> Any:
            try:
                return await function(*args, **kwargs)
            except ToolError:
                raise
            except (BuilderError, OverBudget, *refusals) as error:
                raise ToolError(str(error)) from error
            except SandboxStarting as error:
                raise ToolError(SANDBOX_STARTING) from error
            except AppStarting as error:
                raise ToolError(APP_STARTING) from error
            except Exception as error:
                raise crashed(name, error) from None

        described = " ".join((options.get("description") or function.__doc__ or "").split())
        made = Tool.from_function(
            answered, output_schema=output_schema, **{**options, "description": described}
        )
        mcp.add_tool(made)
        return answered

    return named


def add_builder(mcp: FastMCP, apps: Apps, core: Builder) -> frozenset[str]:
    """Add the builder's tools and its upload panel to `mcp`, and give the tools' names, for
    `BuildersOnly` to keep them to the people who may build. The server draws screens with the
    renderer it serves itself."""
    names: set[str] = set()
    tool = partial(builder_tool, mcp, names)

    owned = partial(owned_app, apps)

    async def run(action: Any, *args: Any) -> Any:
        """Run a `Builder` method in one of the builder's own worker threads."""
        call = partial(action, *args)
        return await anyio.to_thread.run_sync(call, limiter=apps.builder_calls)

    def noticed(content: list[Any], notice: str | None) -> list[Any]:
        """A preview tool's content, then what to say about the preview, if anything."""
        return [*content, notice] if notice is not None else content

    @tool(title="Read an app's overview", annotations=READS)
    async def read_app_overview(app: App) -> dict[str, Any]:
        """Describe one app: its web pages, MCP addresses, tools, rules, members, versions,
        latest changes and latest errors."""
        return await run(core.read_app_overview, *owned(app))

    @tool(title="Read a change", annotations=READS)
    async def read_change(
        app: App,
        commit: Annotated[
            str, Field(description="Its full commit, from read_app_overview's latest_changes.")
        ],
    ) -> dict[str, Any]:
        """Read what one change did to the live app's data files: each one's diff."""
        return await run(core.read_change, *owned(app), commit)

    @tool(title="Read an app's error", annotations=READS)
    async def read_error(
        app: App, error: Annotated[str, Field(description="Its id, such as e-1a2b3c4d5e6f.")]
    ) -> dict[str, Any]:
        """Read one error of the published app, with its traceback: a call its code failed, or
        a start."""
        return await run(core.read_error, *owned(app), error)

    @tool(title="Create an app", annotations=idempotent(WRITES))
    async def create_app(
        app: Annotated[
            str,
            Field(
                description=f"Such as reading-list: at most {MAX_APP_NAME} lower case letters, "
                "digits, - or _, starting with a letter."
            ),
        ],
    ) -> dict[str, Any]:
        """Start a new app from the small template."""
        return await run(core.create_app, *owned(app))

    @tool(title="List an app's files", annotations=READS)
    async def list_files(app: App) -> dict[str, Any]:
        """The draft's files with their sizes, and the live data files."""
        return await run(core.list_files, *owned(app))

    @tool(
        title="Read an app's file",
        annotations=READS,
        description=f"Read a file from line `offset`, counted from 1: {READ_LINES:,} lines by "
        f"default, {MAX_READ_LINES:,} at most, and {MAX_READ_CHARS:,} characters at most. A "
        "published app's data file is read from the live app, any other file from the draft.",
    )
    async def read_file(
        app: App,
        path: DraftPath,
        offset: Annotated[int, Field(ge=1)] = 1,
        limit: Annotated[int, Field(ge=1, le=MAX_READ_LINES)] = READ_LINES,
    ) -> dict[str, Any]:
        """Read lines of a file."""
        return await run(core.read_file, *owned(app), path, offset, limit)

    @tool(title="Write a draft's file", annotations=idempotent(DESTROYS))
    async def write_file(app: App, path: DraftPath, content: str) -> dict[str, Any]:
        """Create or replace a draft's file."""
        return await run(core.write_file, *owned(app), path, content)

    @mcp.resource(
        panel_uri(),
        name="Cliquet upload",
        description="Where the person uploads a data file to the draft.",
        mime_type=views.MIME_TYPE,
    )
    def upload_panel() -> str:
        return panel_html()

    @tool(title="Ask for a data file", annotations=READS, meta={"ui": {"resourceUri": panel_uri()}})
    async def ask_for_file(
        app: App, path: Annotated[str, Field(description="Such as data/ledger.beancount.")]
    ) -> CallToolResult:
        """Let the person upload a data file to the draft, without you copying it: in the
        conversation, or from the link it gives."""
        asked = await run(core.ask_for_file, *owned(app), path)
        if draws_screens():
            how = (
                "The person now sees a panel in the conversation where they choose the file and "
                "upload it. Once it is in, the panel sends you its path and size, or asks the "
                "person to tell you. If it doesn't open their file chooser, give them the link "
                "instead."
            )
        else:
            how = "Give the person this link."
        asked["next"] = f"{how} {asked['next']}"
        return answer(json.dumps(asked, indent=2), asked)

    @tool(title="Upload a data file", annotations=WRITES, meta={"ui": {"visibility": ["app"]}})
    async def upload_file(
        app: str,
        path: str,
        size: Annotated[int, Field(ge=0)],
        offset: Annotated[int, Field(ge=0)],
        data: str,
        upload: str | None = None,
    ) -> CallToolResult:
        """Used by the panel ask_for_file shows. Don't call it yourself."""
        # The panel sends the file in parts: `size` is the whole file's, in bytes, `offset`
        # where this part starts, `data` the part in base64, and `upload` the id the first
        # part's answer gave. The last part puts the file in the draft.
        login, app = owned(app)
        # The file passes through the host, never through the model: a client that draws no
        # panel may still list the tool to its model, which would retype the person's data.
        if not draws_screens():
            raise ToolError(
                "upload_file is for the panel ask_for_file shows. Give the person the link "
                "from ask_for_file instead."
            )
        taken = await run(core.upload_file, login, app, path, size, offset, data, upload)
        if taken["done"]:
            text = f"{path} is in the draft: {size:,} bytes."
        else:
            text = f"{taken['received']:,} of {size:,} bytes received."
        return answer(text, taken)

    @tool(title="Edit a draft's file", annotations=DESTROYS)
    async def edit_file(
        app: App,
        path: str,
        new: Annotated[str, Field(description="Empty removes `old` or the lines.")],
        old: str = "",
        replace_all: bool = False,
        first_line: Annotated[int | None, Field(ge=1)] = None,
        last_line: Annotated[int | None, Field(ge=1)] = None,
        first_text: str | None = None,
        last_text: str | None = None,
        size: Annotated[
            int | None, Field(description="The file's size in bytes, from read_file.")
        ] = None,
    ) -> dict[str, Any]:
        """Replace the exact text `old` by `new` in a draft's file, or lines first_line to
        last_line, given with first_text and last_text, their whole text as read_file gave it."""
        lines = None
        if (
            first_line is not None
            and last_line is not None
            and first_text is not None
            and last_text is not None
        ):
            lines = LineRange(first_line, last_line, first_text, last_text)
        elif any(given is not None for given in (first_line, last_line, first_text, last_text)):
            raise ToolError(
                "A range of lines takes first_line, last_line, first_text and last_text: the "
                "whole text of its first and last lines, as read_file returned them."
            )
        return await run(
            core.edit_file,
            *owned(app),
            path,
            old,
            new,
            replace_all,
            lines,
            size,
        )

    @tool(title="Delete a draft's file", annotations=idempotent(DESTROYS))
    async def delete_file(app: App, path: str) -> dict[str, Any]:
        """Remove a file from the draft."""
        return await run(core.delete_file, *owned(app), path)

    @tool(title="Discard a draft", annotations=idempotent(DESTROYS))
    async def discard_draft(app: App) -> dict[str, Any]:
        """Throw away the changes that aren't published: the draft is the live version again."""
        return await run(core.discard_draft, *owned(app))

    @tool(title="Restore an earlier version", annotations=idempotent(WRITES))
    async def restore_version(
        app: App,
        version: Annotated[str, Field(description="A version or commit from read_app_overview.")],
    ) -> dict[str, Any]:
        """Put an earlier version's code in the draft, to try and publish: see
        read_guide("publishing")."""
        return await run(core.restore_version, *owned(app), version)

    @tool(title="Delete an app", annotations=idempotent(DESTROYS))
    async def delete_app(
        app: App, confirm: Annotated[str, Field(description="The app's name again.")]
    ) -> dict[str, Any]:
        """Delete an app for good, with its data, history, draft and shares: keep a copy first
        from read_app_overview's download_url. Only when the person asks."""
        return await run(core.delete_app, *owned(app), confirm)

    @tool(title="Read a guide to building", annotations=READS)
    async def read_guide(topic: Topic = "start") -> str:
        """Read a guide: start gives the workflow."""
        return await run(core.read_guide, topic)

    @tool(title="Read an example app", annotations=READS)
    async def read_example(example: Example, path: str | None = None) -> str:
        """List an example's files, or read the one at `path`."""
        return await run(core.read_example, example, path)

    @tool(title="Find classes for a view", annotations=READS)
    async def find_classes(
        text: Annotated[str, Field(description="Such as card, stat, badge or s-.")] = "",
    ) -> str:
        """List the classes a view may use whose name holds the text: daisyUI's and Cliquet's
        layout- classes. A view refuses any other."""
        return await run(core.find_classes, text)

    @tool(title="Find icons", annotations=READS)
    async def find_icons(
        text: Annotated[str, Field(description="A word, such as film, money or delete.")],
    ) -> str:
        """List Lucide's icons whose name or tags hold the text, for App(..., icon=...) or a
        view's data-icon."""
        return await run(core.find_icons, text)

    @tool(title="Publish an app", annotations=DESTROYS)
    async def publish_app(
        app: App,
        remark: Annotated[
            str, Field(min_length=1, max_length=200, description="One line for the app's history.")
        ],
    ) -> dict[str, Any]:
        """Make the draft live if every check passes, the tests too, which can take minutes. It
        runs migrate.py on the live data, ends the preview, and starts the draft again from the
        new version. It only changes the app on this Cliquet server."""
        return await run(core.publish_app, *owned(app), remark)

    @tool(title="Check a draft", annotations=idempotent(WRITES))
    async def check_app(app: App) -> dict[str, Any]:
        """Build the draft, check its definition, its data and its views, and say what
        publishing changes. It runs no tests: run_tests does."""
        return await run(core.check_app, *owned(app))

    @tool(title="Start a preview", annotations=idempotent(WRITES))
    async def start_preview(app: App) -> dict[str, Any]:
        """Run the draft on a copy of the current data, to try things. It replaces any running
        preview and its data."""
        return await run(core.start_preview, *owned(app))

    @tool(title="Stop a preview", annotations=idempotent(WRITES))
    async def stop_preview(app: App) -> dict[str, Any]:
        """Stop the app's preview, and throw its copy of the data away."""
        return await run(core.stop_preview, *owned(app))

    @tool(title="Take screenshots of views", annotations=idempotent(WRITES))
    async def take_screenshots(
        app: App,
        role: Role = None,
        view: Annotated[str | None, Field(description="One view only.")] = None,
        theme: Theme = "light",
    ) -> ToolResult:
        """Take screenshots of each of the draft's views on the preview, at two widths."""
        login, app = owned(app)
        shots, notice = await run(core.take_screenshots, login, app, role, view, theme)
        content: list[Any] = []
        dark = _in_theme(theme)
        for picture in shots:
            kind = "as wide as a conversation" if picture.width >= 500 else "as wide as a phone"
            note = cut_note(
                app, picture.view, role, picture.width, theme, picture.height, picture.shown
            )
            line = f"{picture.view}, {kind} ({picture.width} pixels){dark}: {note}"
            content.append(line.rstrip())
            content.append(Image(data=picture.png, format="png"))
        content.append(
            "Look at each screenshot. If something looks wrong, fix the view and take them again."
            + ("" if dark else ' Before you publish, take them once with theme="dark" too.')
        )
        return ToolResult(content=blocks(noticed(content, notice)))

    @tool(title="Try a view on the preview", annotations=WRITES)
    async def try_view(
        app: App,
        view: View,
        steps: Annotated[list[dict[str, Any]], Field(description=STEPS)],
        role: Role = None,
        width: Annotated[int, Field(ge=320, le=1280, description="In pixels.")] = 768,
        theme: Theme = "light",
    ) -> ToolResult:
        """Click through a draft view, then make its actions again on the preview, which
        keeps what they change."""
        login, app = owned(app)
        tried = await run(core.try_view, login, app, view, steps, role, width, theme)
        text = [_try_view_text(view, role, width, theme, steps, tried)]
        text += [cut_note(app, view, role, width, theme, tried.height, png_height(tried.png))]
        text = noticed([line for line in text if line], tried.notice)
        return ToolResult(content=blocks(["\n".join(text), Image(data=tried.png, format="png")]))

    @tool(title="Try a call on the preview", annotations=WRITES)
    async def try_call(
        app: App,
        tool: Annotated[
            str, Field(description="An action, a query, or a built-in tool such as app_history.")
        ],
        role: Role = None,
        arguments: Arguments = None,
    ) -> dict[str, Any]:
        """Call one of the app's actions or queries on the preview, which keeps what it changes."""
        login, app = owned(app)
        return await run(core.try_call, login, app, tool, arguments, role)

    @tool(
        title="Show a view of the preview",
        annotations=idempotent(WRITES),
        meta={"ui": {"resourceUri": views.renderer_uri()}},
    )
    async def show_preview(
        app: App, view: View, role: Role = None, arguments: Arguments = None
    ) -> CallToolResult:
        """Show a draft's view to the person in the conversation, on the preview."""
        login, app = owned(app)
        shown, notice = await run(core.show_preview, login, app, view, role, arguments)
        if not draws_screens():
            text = (
                "This assistant can't draw views. Use take_screenshots to look at the view "
                "yourself, and try_view to click through it. The person can also open the "
                "preview's link from start_preview in their browser."
            )
            return answer("\n\n".join(noticed([text], notice)))
        text = (
            f"The person now sees the view {view} of the draft, on the preview. They can click "
            "through it: what they change is kept in the preview, not in the live app. "
            "Don't repeat what it shows."
        )
        return answer("\n\n".join(noticed([text], notice)), shown)

    @tool(
        title="Call the preview from a view",
        annotations=WRITES,
        meta={"ui": {"visibility": ["app"]}},
    )
    async def preview_call(
        preview: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
        role: str | None = None,
        # A digest of the app.py that laid the view out. Missing only from a screen shown
        # before servers asked for it, whose preview ended.
        app_py: str = "",
    ) -> ToolResult:
        """Used by a view shown with show_preview. Don't call it yourself."""
        # One call from the view, on the preview it was shown on, as the role it was shown
        # as: `tool` is the app tool it calls, or the view it lays out again.
        login = caller(apps).login
        result = await run(core.preview_call, login, preview, app_py, tool, arguments, role)
        text = "\n".join(part.text for part in result.content if isinstance(part, TextContent))
        if result.is_error:
            raise ToolError(text)
        return ToolResult(
            content=list(result.content), structured_content=result.structured_content
        )

    @tool(title="Run a draft's tests", annotations=idempotent(WRITES))
    async def run_tests(
        app: App, only: Annotated[str | None, Field(description="A pytest -k expression.")] = None
    ) -> dict[str, Any]:
        """Run the draft's tests and return their output."""
        return await run(core.run_tests, *owned(app), only)

    return frozenset(names)


# What a tool answers once its call's deadline passed while it waited for a start, which goes on.
APP_STARTING = (
    "The app is still starting, which can take a few minutes after a deploy. "
    "Call the same tool again in a minute: the start goes on meanwhile."
)
SANDBOX_STARTING = (
    "Your sandbox is still starting, which can take a few minutes after a deploy. "
    "Call the same tool again in a minute: the start goes on meanwhile."
)


def blocks(parts: Sequence[str | Image]) -> list[TextContent | Image]:
    """A tool's content, each text its own block: FastMCP sends a list of plain strings as one
    JSON text, `["..."]`, which a model reads as code."""
    return [
        TextContent(type="text", text=part) if isinstance(part, str) else part for part in parts
    ]


def crashed(name: str, error: Exception) -> ToolError:
    """What a builder's tool, `name`, answers for a crash: logged as unexpected under an id
    the assistant is given with Cliquet's fixed words, never the crash's text."""
    error_id = report_unexpected(error, f"the builder's tool {name} failed: {described(error)[1]}")
    return ToolError(
        "Cliquet failed on this call, not the app. Try again in a moment. If it fails again, "
        f"give the person error {error_id} for the server's operator: read_error can't read it."
    )


def _in_theme(theme: Theme) -> str:
    """How an answer names a picture's theme: only the dark one, as light is the default."""
    return ", in the dark theme" if theme == "dark" else ""


def _try_view_text(
    view: str, role: str | None, width: int, theme: Theme, steps: list[Any], tried: Any
) -> str:
    lines = [
        f"{view}, as {role or 'everyone'}, at {width} pixels{_in_theme(theme)}, "
        f"after {len(steps)} steps.",
        "",
    ]
    if tried.calls:
        lines.append("The view made these calls, on a copy of the preview's data:")
        for number, call in enumerate(tried.calls, 1):
            result = call["result"]
            if call["error"] is not None:
                outcome = f"refused or failed: {call['error']}"
            elif isinstance(result, dict) and result.get("screen") == FORMAT:
                # A view laid out again: its screen is the picture, not text.
                outcome = f"laid out again with {_short(result.get('arguments') or {})}"
            else:
                outcome = f"done {_short(result)}"
            lines.append(f"{number}. {call['tool']} {_short(call['arguments'])}: {outcome}")
    else:
        lines.append("The view made no call.")
    lines += ["", f"Error: {tried.error}" if tried.error else "No error.", ""]
    if tried.kept:
        lines.append("Its calls that change data, on the preview:")
        for one in tried.kept:
            if not one["made"]:
                outcome = "not made again"
            else:
                kept = "kept in the preview" if one["ok"] else "not kept"
                outcome = f"made again with the same arguments, {kept}"
            text = shortened(one["text"], 400)
            lines.append(f"{one['call']}. {one['tool']}: {outcome}: {text}")
    else:
        lines.append("The view called no action that changed data. The preview is as it was.")
    return "\n".join(lines)


def _short(value: Any) -> str:
    return shortened(json.dumps(value, ensure_ascii=False), 400)
