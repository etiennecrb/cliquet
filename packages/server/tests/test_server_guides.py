import re

import anyio
import pytest
from fastmcp import Client as BuilderClient

from cliquet.manifest import MAX_TITLE
from cliquet.scaffold import sdk_pin
from cliquet.tools import MAX_APP_NAME, MAX_FULL_NAME, MAX_KEY
from cliquet_server.builder import Builder, BuilderError
from cliquet_server.builder.core import EXAMPLES as BUILDER_EXAMPLES
from cliquet_server.builder.server import INSTRUCTIONS

from .server_helpers import EXAMPLES, draft_example, me_server, text
from .test_server_builder import APP_ONLY

RULES = (
    "### Views and tabs",
    "### Friendly",
    "### Hierarchy",
    "### The accent",
)

TOPICS = ("start", "app", "tests", "views", "instructions", "publishing")


def test_every_topic_has_a_guide(builder: Builder) -> None:
    for topic in TOPICS:
        assert len(builder.read_guide(topic)) > 200

    with pytest.raises(BuilderError) as error:
        builder.read_guide("nope")
    for topic in TOPICS:
        assert topic in str(error.value)
    # Prefab's guide went with Prefab.
    with pytest.raises(BuilderError):
        builder.read_guide("prefab")


def test_start_is_the_default(builder: Builder) -> None:
    start = builder.read_guide("start")
    assert builder.read_guide() == start
    for name in (
        "create_app",
        "check_app",
        "run_tests",
        "start_preview",
        "try_call",
        "take_screenshots",
        "try_view",
        "show_preview",
        'read_guide("views")',
        "find_classes",
        "find_icons",
        "publish_app",
    ):
        assert name in start


def test_no_guide_speaks_of_a_shell(builder: Builder) -> None:
    forbidden = (
        "cliquet new",
        "cliquet test",
        "cliquet check",
        "cliquet manifest",
        "cliquet preview",
        "cliquet serve",
        "tool.uv.sources",
        "/tmp/",
        "browser tool",
        "](../",
    )
    words = ("git", "uv", "shell", "terminal")
    for topic in TOPICS:
        text = builder.read_guide(topic)
        for phrase in forbidden:
            assert phrase not in text, (topic, phrase)
        for line in text.splitlines():
            assert not line.startswith("```bash"), topic
        for word in words:
            assert re.search(rf"\b{word}\b", text) is None, (topic, word)


def test_the_guides_keep_the_app_rules(builder: Builder) -> None:
    assert "ctx.now" in builder.read_guide("app")
    assert "Refused" in builder.read_guide("app")
    assert "expect_refused" in builder.read_guide("tests")
    views = builder.read_guide("views")
    for part in ("-> Screen", "Screen(", "data-call", "data-view", "find_classes"):
        assert part in views, part
    assert "prefab" not in views.lower()
    assert "migrate.py" in builder.read_guide("publishing")


def test_the_instructions_guide_asks_for_a_remark_with_every_write(builder: Builder) -> None:
    guide = builder.read_guide("instructions")
    assert "## Ask for `app_remark` with every write" in guide
    assert "in each procedure that writes, say what this app's remark holds" in guide
    assert '"statement of March, line 14"' in guide
    assert "Never people's names or the values" in guide


def test_the_guides_state_the_limits_the_sdk_checks(builder: Builder) -> None:
    app, views = builder.read_guide("app"), builder.read_guide("views")
    assert f"an app's name is at most {MAX_APP_NAME} characters" in app
    assert f"A full tool name over {MAX_FULL_NAME} characters" in app
    assert f"starting with a letter, or over {MAX_KEY} characters" in app
    assert "of every field of a model they take or return" in app
    assert f"A title is one line of at most {MAX_TITLE} characters" in views
    assert "title=" in views and "follow the order the views are written" in views


def test_the_app_guide_pins_this_sdk(builder: Builder) -> None:
    assert sdk_pin() in builder.read_guide("app")


def test_every_tool_is_named_in_a_guide(builder: Builder) -> None:
    async def names() -> list[str]:
        async with BuilderClient(me_server(builder.apps)) as client:
            return [tool.name for tool in await client.list_tools()]

    tools = anyio.run(names)
    guides = "\n".join(builder.read_guide(topic) for topic in TOPICS)
    for name in tools:
        if name in {"read_guide", "delete_app"} | APP_ONLY:
            continue
        assert name in guides, name


# The old names, which no guide may use any more.
OLD_NAMES = (
    "app_info",
    "try_tool",
    "try_screen",
    "show_screen",
    "take_pictures",
    "show_view",
    "discard_changes",
    "`publish`",
    "`publish(",
    "list_my_apps",
    "open_app",
    "`read`",
    "`read(",
    "`write`",
    "`write(",
    "`show`",
    "`show(",
)
# Names in the guides' code spans that look like a tool, but are the SDK's or Python's.
NOT_TOOLS = {"model_dump", "expect_refused"}


def test_the_guides_name_only_tools_that_exist(builder: Builder) -> None:
    async def names() -> set[str]:
        async with BuilderClient(me_server(builder.apps)) as client:
            return {tool.name for tool in await client.list_tools()}

    tools = anyio.run(names)
    texts = {topic: builder.read_guide(topic) for topic in TOPICS}
    texts["server"] = INSTRUCTIONS
    for topic, written in texts.items():
        prose = re.sub(r"```.*?```", "", written, flags=re.DOTALL)
        # A tool's name is a verb and a noun: `name(` with an underscore in a code span.
        for name in re.findall(r"`([a-z]+_[a-z_]+)\(", prose):
            assert name in tools or name in NOT_TOOLS, (topic, name)
        assert re.search(r"(?<![a-z_])guide\(", prose) is None, topic
        for old in OLD_NAMES:
            assert old not in written, (topic, old)
        assert "confirm=" not in written.replace("confirm=<the app's name>", ""), topic


def test_find_classes(builder: Builder) -> None:
    stats = builder.find_classes("stat")
    assert stats.startswith("daisyUI's classes")
    assert "stat-value" in stats and "sm:stats-horizontal" in stats
    assert "btn-primary" not in stats
    assert "layout-stack" in builder.find_classes("layout-")
    # A breakpoint that never applies on a screen isn't offered.
    cards = builder.find_classes("card")
    assert "sm:card-side" in cards and "lg:card" not in cards
    assert "card-title" in builder.find_classes()
    assert builder.find_classes("zzzz").startswith("No class holds 'zzzz'.")


def test_find_icons(builder: Builder) -> None:
    films = builder.find_icons("film")
    assert films.startswith("Lucide's icons")
    names = [line.split(":")[0] for line in films.splitlines()[1:]]
    assert {"clapperboard", "film"} <= set(names)
    assert "clapperboard: movie, film" in films, "each icon with its tags"
    assert "more: look for a narrower word" in builder.find_icons("arrow")
    assert builder.find_icons("zzzz").startswith("No icon holds 'zzzz'.")


def test_read_example_lists_and_reads(builder: Builder) -> None:
    listing = builder.read_example("meeting-rooms")
    assert "app.py" in listing
    assert "data/bookings.csv" in listing

    source = builder.read_example("meeting-rooms", "app.py")
    assert "App(" in source
    assert "tool.uv.sources" not in source


def test_read_example_serves_every_example_folder() -> None:
    assert sorted(BUILDER_EXAMPLES) == sorted(path.name for path in EXAMPLES.iterdir())


def test_read_example_stays_inside_the_example(builder: Builder) -> None:
    with pytest.raises(BuilderError):
        builder.read_example("meeting-rooms", "../ledger/app.py")
    with pytest.raises(BuilderError):
        builder.read_example("nope")


def test_the_guide_is_served_over_mcp(builder: Builder) -> None:
    async def main() -> None:
        async with BuilderClient(me_server(builder.apps)) as client:
            start = await client.session.call_tool("read_guide", {})
            assert text(start).startswith("# Building a Cliquet app")
            # Once, as text: never again as structured content, nor with a schema saying so.
            assert len(start.content) == 1 and start.structured_content is None
            classes = await client.session.call_tool("find_classes", {"text": "badge"})
            assert "badge-soft" in text(classes)
            found = await client.session.call_tool("find_icons", {"text": "film"})
            assert "clapperboard" in text(found)
            example = await client.session.call_tool("read_example", {"example": "meeting-rooms"})
            assert "app.py" in text(example)
            listed = (await client.session.list_tools()).tools
            texts = {"read_guide", "read_example", "find_classes", "find_icons"}
            assert all(tool.output_schema is None for tool in listed if tool.name in texts)

    anyio.run(main)


def test_the_tests_guide_s_sample_runs(builder: Builder) -> None:
    draft_example(builder, "rooms", EXAMPLES / "meeting-rooms")
    builder.delete_file("etienne", "rooms", "tests/test_meeting_rooms.py")
    match = re.search(r"```python\n(.*?)```", builder.read_guide("tests"), re.DOTALL)
    assert match is not None
    builder.write_file("etienne", "rooms", "tests/test_guide.py", match.group(1))

    result = builder.run_tests("etienne", "rooms")

    assert "6 passed" in result["output"]
    assert result["passed"] is False
    assert "never refused" in result["output"]


def _design_rules(text: str) -> str:
    """The "Design rules" section of a views guide."""
    start = text.index("## Design rules")
    return text[start : text.index("\n## ", start)]


def test_the_start_guide_points_at_the_design_rules(builder: Builder) -> None:
    assert "Design rules" in builder.read_guide("start")
    builder_rules = _design_rules(builder.read_guide("views"))
    for heading in RULES:
        assert heading in builder_rules


def test_the_guides_explain_views_tabs_and_pages(builder: Builder) -> None:
    rules = _design_rules(builder.read_guide("views"))
    for phrase in (
        "One job per view",
        "one per audience or workflow",
        "hold sections of one destination",
        "short flow where only one step should show",
        "The main thing fits a phone's first screen",
    ):
        assert phrase in rules, phrase


def test_the_guides_name_the_hierarchy_and_the_examples_follow_it(builder: Builder) -> None:
    rules = _design_rules(builder.read_guide("views"))
    for part in ("<header><h2>", "`stat`", "`card-title`", "text-base-content/60", "`btn-primary`"):
        assert part in rules, part
    for example in sorted(path.name for path in EXAMPLES.iterdir()):
        source = (EXAMPLES / example / "app.py").read_text()
        # Each screen opens with its header, and the app has an accent.
        assert re.search(r"<header>\s*<h2>", source), example
        assert '<p class="text-base-content/60">' in source, example
        assert re.search(r'accent="#[0-9a-f]{6}"', source), example
