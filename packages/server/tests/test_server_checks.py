import re

import anyio
import pytest
from fastmcp import Client as BuilderClient

from cliquet_server.builder import Builder, BuilderError

from .server_helpers import draft_example, draft_files, me_server

TEMPLATE_DOCSTRING = '"""Tasks: a list of items, kept by an agent."""'


def test_check_app_on_the_template(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")

    checked = builder.check_app("etienne", "tasks")

    assert checked["ok"] is True
    assert checked["problems"] == []
    tools = {tool["name"] for tool in checked["definition"]["tools"]}
    assert "add_item" in tools
    assert "Item names are unique" in checked["definition"]["rules"]
    assert "app.py.lock" in draft_files(builder.drafts, "etienne", "tasks")
    assert checked["next"] == (
        "Run the tests with run_tests. Before publish_app, tell the person what changes and "
        "data say."
    )


def test_check_app_reports_the_definition_s_problems(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.edit_file("etienne", "tasks", "app.py", TEMPLATE_DOCSTRING, "")

    checked = builder.check_app("etienne", "tasks")

    assert checked["ok"] is False
    assert any("docstring" in problem for problem in checked["problems"])
    assert "definition" not in checked


def test_check_app_refuses_a_name_some_mcp_clients_refuse(builder: Builder) -> None:
    draft_example(builder, "shop")
    plain = '@shop.view(roles=["owner", "staff"])\ndef shelves('
    named = '@shop.view(roles=["owner", "staff"])\ndef résultats('
    builder.edit_file("etienne", "shop", "app.py", plain, named)

    checked = builder.check_app("etienne", "shop")

    assert checked["ok"] is False
    (problem,) = checked["problems"]
    assert "`résultats` isn't a name every MCP client accepts" in problem
    assert "Rename the function to `resultats`" in problem
    assert 'pass title="Résultats" to its view decorator' in problem

    # What the problem says to do passes.
    titled = '@shop.view(roles=["owner", "staff"], title="Résultats")\ndef resultats('
    builder.edit_file("etienne", "shop", "app.py", named, titled)
    checked = builder.check_app("etienne", "shop")
    assert checked["ok"] is True, checked["problems"]
    assert "resultats" in {tool["name"] for tool in checked["definition"]["tools"]}

    # A field of a model a tool returns reaches the client by its alias, which follows the rule.
    camel = '    in_stock: Annotated[int, Field(alias="inStock")]\n'
    builder.edit_file("etienne", "shop", "app.py", "    in_stock: int\n", camel)
    checked = builder.check_app("etienne", "shop")
    assert checked["ok"] is False
    (problem,) = checked["problems"]
    assert "`inStock` isn't a name every MCP client accepts" in problem
    assert "Rename the alias of the field `in_stock` of the model `Reorder`" in problem


def test_check_app_refuses_a_tool_s_full_name_too_long_for_some_clients(builder: Builder) -> None:
    draft_example(builder, "shop")
    # mcp__shop__ leaves 53 of the 64 characters some clients accept.
    long = "s" * 54
    builder.edit_file("etienne", "shop", "app.py", "def shelves(", f"def {long}(")

    checked = builder.check_app("etienne", "shop")

    assert checked["ok"] is False
    (problem,) = checked["problems"]
    assert f"only up to 64 characters, and mcp__shop__{long} is 65." in problem
    assert "Shorten the function's name to at most 53 characters." in problem


def test_create_app_refuses_a_name_too_long_for_the_built_in_tools(builder: Builder) -> None:
    # With app_rollback_transaction, a name of 34 characters passes the 64 of a full name.
    with pytest.raises(BuilderError, match="at most 33 lower case letters, digits, - or _"):
        builder.create_app("etienne", "t" * 34)
    builder.create_app("etienne", "t" * 33)
    checked = builder.check_app("etienne", "t" * 33)
    assert checked["ok"] is True, checked["problems"]


def test_check_app_shows_the_icon_and_refuses_an_unknown_one(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    plain = 'App("tasks", files=[ITEMS])'
    builder.edit_file("etienne", "tasks", "app.py", plain, plain[:-1] + ', icon="list-todo")')

    checked = builder.check_app("etienne", "tasks")

    assert checked["ok"] is True
    assert (checked["definition"]["accent"], checked["definition"]["icon"]) == (None, "list-todo")

    builder.edit_file("etienne", "tasks", "app.py", '"list-todo"', '"list-tod"')
    refused = builder.check_app("etienne", "tasks")

    assert refused["ok"] is False
    (problem,) = refused["problems"]
    assert "icon='list-tod' isn't one of Lucide's icons. Did you mean list-todo" in problem
    assert "find_icons" in problem


@pytest.mark.parametrize(
    ("path", "old", "new", "found"),
    [
        ("app.py", "import csv\n", "import csv\nimport random\n", "import random isn't allowed"),
        ("app.py", "import csv\n", "import csv\nimport subprocess\n", "import subprocess isn't"),
        (
            "app.py",
            "import csv\n",
            "import csv\nimport sys\n\nsys.path.append('lib')\n",
            "sys.path.append('lib') changes sys.path",
        ),
        (
            "tests/test_app.py",
            "from datetime import UTC, datetime\n",
            "from datetime import UTC, datetime\n\nFIRST = open('data/items.csv').read()\n",
            "'data/items.csv' names the app's data/ folder",
        ),
        (
            "app.py",
            "    note: str\n",
            "    note: str = Field(serialization_alias='text')\n",
            "Item.note has serialization_alias='text'",
        ),
    ],
    ids=["randomness", "subprocess", "sys.path", "tests reading data", "result"],
)
def test_check_app_and_run_tests_refuse_what_the_guides_rule_out(
    builder: Builder, path: str, old: str, new: str, found: str
) -> None:
    builder.create_app("etienne", "tasks")
    builder.edit_file("etienne", "tasks", path, old, new)

    checked = builder.check_app("etienne", "tasks")
    tested = builder.run_tests("etienne", "tasks")

    assert checked["ok"] is False
    # Each names the file and the line.
    (problem,) = checked["problems"]
    assert re.fullmatch(rf"{re.escape(path)}:\d+: {re.escape(found)}.*", problem)
    assert tested["passed"] is False
    assert f"- {problem}" in tested["output"]


def test_check_app_checks_the_first_data(builder: Builder) -> None:
    draft_example(builder, "shop")
    movements = builder.read_file("etienne", "shop", "data/movements.csv")["text"]
    builder.write_file(
        "etienne",
        "shop",
        "data/movements.csv",
        movements + "2026-10-02,POT-GLS,-99,sale,R-9999,24.00\n",
    )

    checked = builder.check_app("etienne", "shop")

    assert checked["ok"] is False
    assert any("Stock never goes below zero" in problem for problem in checked["problems"])


def test_check_app_reports_an_invariant_that_fails(builder: Builder) -> None:
    draft_example(builder, "shop")
    source = builder.read_file("etienne", "shop", "app.py")["text"]
    failing = (
        '\n\n@shop.invariant("Never checked")\n'
        "def fails(ctx: InvariantContext) -> list[str]:\n"
        '    raise RuntimeError("it went boom")\n'
    )
    builder.write_file("etienne", "shop", "app.py", source + failing)

    checked = builder.check_app("etienne", "shop")

    assert checked["ok"] is False
    told = "An invariant failed on the current data: RuntimeError: it went boom, in the invariant"
    assert any(problem.startswith(told) for problem in checked["problems"])


def test_check_app_reports_data_no_one_declared(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "data/other.csv", "name\none\n")

    checked = builder.check_app("etienne", "tasks")

    assert checked["ok"] is False
    assert any("data/other.csv" in problem for problem in checked["problems"])
    assert any("App(files=[...])" in problem for problem in checked["problems"])


def test_check_app_checks_live_data_once_published(shop, builder: Builder) -> None:
    checked = builder.check_app("etienne", "shop")

    assert checked["ok"] is True


def test_run_tests_pass(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")

    result = builder.run_tests("etienne", "tasks")

    assert result["passed"] is True
    assert "passed" in result["output"]
    assert result["next"] == (
        "Start a preview with start_preview to try the app, or call check_app and publish_app."
    )


def test_run_tests_fail(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.write_file(
        "etienne",
        "tasks",
        "tests/test_extra.py",
        "def test_wrong() -> None:\n    assert 1 == 2\n",
    )

    result = builder.run_tests("etienne", "tasks")

    assert result["passed"] is False
    assert "test_wrong" in result["output"]
    assert result["next"] == "Read the output, fix the app or the tests, then run them again."


def test_run_tests_only(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.write_file(
        "etienne",
        "tasks",
        "tests/test_extra.py",
        "def test_wrong() -> None:\n    assert 1 == 2\n",
    )

    result = builder.run_tests("etienne", "tasks", only="test_wrong")

    assert result["passed"] is False
    assert "1 failed" in result["output"]


def test_run_tests_when_the_app_can_t_be_built(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.edit_file("etienne", "tasks", "app.py", TEMPLATE_DOCSTRING, "")

    result = builder.run_tests("etienne", "tasks")

    assert result["passed"] is False
    assert result["output"].startswith("The app can't be built")
    assert result["next"] == "Call check_app, fix app.py, then run the tests again."


def test_the_tools_over_mcp(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")

    async def main() -> None:
        async with BuilderClient(me_server(builder.apps)) as client:
            checked = await client.call_tool("check_app", {"app": "tasks"})
            assert checked.data["ok"] is True

    anyio.run(main)
