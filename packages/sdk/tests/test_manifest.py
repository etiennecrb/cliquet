import json
import sys
import textwrap
import unicodedata
from pathlib import Path

import pytest
from conftest import APPS, EXAMPLES, HEADER, WriteApp, module_name

from cliquet.errors import AppError
from cliquet.files import data_name_problem
from cliquet.manifest import (
    build_manifest,
    load_app,
    lock_source_problem,
    read_script_metadata,
    sdk_source_problem,
    uv_environment,
    without_uv_settings,
)

VALID = """
\"\"\"Monthly totals, kept by an agent.\"\"\"

from datetime import date
from decimal import Decimal
from typing import Annotated

from cliquet import (
    ActionContext, App, BaseModel, Field, InvariantContext, QueryContext, Refused, Screen
)

demo = App("demo", files=["data.csv"], roles=["owner", "reader"])


class Total(BaseModel):
    month: str
    total: Decimal


@demo.action(roles=["owner"])
def add(ctx: ActionContext, date_: Annotated[date, Field(alias="date")], amount: Decimal) -> None:
    \"\"\"Add an amount.\"\"\"


@demo.invariant("Amounts are positive")
def positive(ctx: InvariantContext) -> list[str]:
    return []


@demo.query(roles=["owner", "reader"])
def totals(ctx: QueryContext, month: str | None = None) -> list[Total]:
    \"\"\"Totals per month.\"\"\"
    return []


@demo.view(roles=["owner", "reader"])
def overview(ctx: QueryContext, month: str | None = None) -> Screen:
    \"\"\"The totals, on a screen.\"\"\"
    return Screen("<h2>Totals for {{ month }}</h2>", args={"month": month}, month=month)
"""


def manifest(path: Path) -> dict:
    return build_manifest(path, module_name(path))


def problems(path: Path) -> str:
    with pytest.raises(AppError) as error:
        manifest(path)
    return str(error.value)


def test_builds_a_manifest(write_app: WriteApp) -> None:
    m = manifest(write_app(VALID))
    json.dumps(m)  # the manifest is plain JSON
    assert m["name"] == "demo"
    assert m["files"] == ["data.csv"]
    assert m["roles"] == ["owner", "reader"]
    assert m["script"]["dependencies"] == ["cliquet-sdk>=0.0.1,<0.1"]

    (add,) = m["actions"]
    assert add["name"] == "add"
    assert add["description"] == "Add an amount."
    assert add["roles"] == ["owner"]
    assert set(add["input_schema"]["properties"]) == {"date", "amount"}  # the alias is public
    assert add["input_schema"]["additionalProperties"] is False

    assert m["invariants"] == [{"name": "positive", "description": "Amounts are positive"}]

    (totals,) = m["queries"]
    assert totals["roles"] == ["owner", "reader"]
    assert totals["input_schema"].get("required", []) == []
    assert totals["output_schema"]["type"] == "array"
    assert totals["output_schema"]["items"] == {"$ref": "#/$defs/Total"}

    # A view's layout depends on the data, so the manifest names it and its arguments only.
    (overview,) = m["views"]
    assert set(overview) == {"name", "title", "description", "roles", "input_schema"}
    assert overview["name"] == "overview"
    assert overview["title"] is None  # the app chose no wording for its tab
    assert overview["description"] == "The totals, on a screen."
    assert overview["roles"] == ["owner", "reader"]
    assert set(overview["input_schema"]["properties"]) == {"month"}
    assert overview["input_schema"].get("required", []) == []
    assert overview["input_schema"]["additionalProperties"] is False
    assert "prefab_version" not in m
    assert (m["accent"], m["icon"]) == (None, None)
    assert m["title"] is None  # the app's pages read its name


def test_the_manifest_names_the_app_s_accent_and_icon(write_app: WriteApp) -> None:
    source = VALID.replace(
        'App("demo", files=["data.csv"],',
        'App("demo", files=["data.csv"], accent="#1E3A8A", icon="piggy-bank",',
    )
    m = manifest(write_app(source))
    assert (m["accent"], m["icon"]) == ("#1e3a8a", "piggy-bank")


def test_the_manifest_names_the_app_as_people_read_it(write_app: WriteApp) -> None:
    source = VALID.replace(
        'App("demo", files=["data.csv"],',
        'App("demo", files=["data.csv"], title=" FoodBank HQ ",',
    )
    assert manifest(write_app(source))["title"] == "FoodBank HQ"


@pytest.mark.parametrize(
    ("title", "message"),
    [
        ('""', "title is empty. Write the app's name as people read it, or leave title out."),
        (
            '"x" * 41',
            "title is 41 characters long, and the app's name as people read it is at most 40.",
        ),
        ('"Food\\nBank"', "title has a line break or a control character."),
        ("3", "title=3 isn't text."),
    ],
)
def test_an_app_s_title_is_one_short_line(write_app: WriteApp, title: str, message: str) -> None:
    source = VALID.replace(
        'App("demo", files=["data.csv"],', f'App("demo", files=["data.csv"], title={title},'
    )
    found = problems(write_app(source))
    assert message in found
    assert "App('demo'): title" in found  # the problem names the app


def test_roles_follow_the_app_order(write_app: WriteApp) -> None:
    source = VALID.replace(
        '@demo.query(roles=["owner", "reader"])', '@demo.query(roles=["reader", "owner"])'
    )
    assert manifest(write_app(source))["queries"][0]["roles"] == ["owner", "reader"]


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            '@demo.action(roles=["owner"])',
            '@demo.action(roles=["admin"])',
            "role 'admin' is not declared",
        ),
        ('@demo.action(roles=["owner"])', "@demo.action(roles=[])", "lets nobody use it"),
        ('"""Add an amount."""', "pass", "add a docstring"),
        ("amount: Decimal)", "amount)", "annotate `amount`"),
        ("amount: Decimal)", "*amounts: Decimal)", "name each argument"),
        ("def add(ctx: ActionContext,", "def add(ctx: QueryContext,", "`ctx: ActionContext`"),
        ("amount: Decimal) -> None:", "amount: Decimal):", "`-> None`"),
        (
            "amount: Decimal) -> None:",
            "amount: Decimal, app_request_id: str) -> None:",
            "starts with app_",
        ),
        (
            "month: str | None = None) -> list[Total]:",
            "month: str | None = None):",
            "annotate what it returns",
        ),
        (
            "def positive(ctx: InvariantContext) -> list[str]:",
            "def positive(ctx: InvariantContext) -> bool:",
            "`-> list[str]`",
        ),
        ('@demo.invariant("Amounts are positive")', '@demo.invariant("  ")', "describe the rule"),
        (
            '    """Totals per month."""',
            '    """Totals per month."""\n    import time\n    time.time()',
            "reads the system clock",
        ),
        (
            '    """Totals per month."""',
            '    """Totals per month."""\n    import uuid\n    uuid.uuid4()',
            "uuid.uuid4 is random",
        ),
        ("from decimal import Decimal", "import random\nfrom decimal import Decimal", "random"),
        (
            "from decimal import Decimal",
            "import subprocess\nfrom decimal import Decimal",
            "program",
        ),
        (
            "from decimal import Decimal",
            "import sys\nsys.path.insert(0, 'lib')\nfrom decimal import Decimal",
            "changes sys.path",
        ),
    ],
)
def test_reports_problems(write_app: WriteApp, old: str, new: str, message: str) -> None:
    assert old in VALID
    assert message in problems(write_app(VALID.replace(old, new, 1)))


def test_an_action_may_return_a_model(write_app: WriteApp) -> None:
    source = VALID.replace("amount: Decimal) -> None:", "amount: Decimal) -> Total:")
    (add,) = manifest(write_app(source))["actions"]
    assert add["output_schema"]["title"] == "Total"
    assert add["output_schema"]["type"] == "object"
    assert set(add["output_schema"]["properties"]) == {"month", "total"}


def test_an_action_may_say_it_destroys(write_app: WriteApp) -> None:
    (add,) = manifest(write_app(VALID))["actions"]
    assert add["destructive"] is False
    source = VALID.replace(
        '@demo.action(roles=["owner"])', '@demo.action(roles=["owner"], destructive=True)'
    )
    (add,) = manifest(write_app(source))["actions"]
    assert add["destructive"] is True


def test_an_action_returning_none_has_no_output_schema(write_app: WriteApp) -> None:
    (add,) = manifest(write_app(VALID))["actions"]
    assert add["output_schema"] is None


def test_an_action_needs_a_return_annotation(write_app: WriteApp) -> None:
    found = problems(write_app(VALID.replace("amount: Decimal) -> None:", "amount: Decimal):")))
    assert "annotate what it returns: `-> None`, or a model such as `-> Imported`" in found


RESULTS = """
from pydantic import ConfigDict, computed_field, field_serializer


class Line(BaseModel):
    month: str
    total: Decimal{line}


class Report(BaseModel):
    lines: dict[str, list[Line | None]]


@demo.query(roles=["owner", "reader"])
def report(ctx: QueryContext) -> Report:
    \"\"\"The report.\"\"\"
    return Report(lines={{}})


@demo.query(roles=["owner", "reader"])
def lines(ctx: QueryContext) -> list[Line]:
    \"\"\"The lines.\"\"\"
    return []
"""


@pytest.mark.parametrize(
    ("line", "found", "starts"),
    [
        (
            "\n\n    @field_serializer('total')\n    def text(self, total: Decimal) -> str:\n"
            "        return str(total)",
            "Line.text is a field_serializer",
            "@field_serializer",
        ),
        (
            "\n\n    @computed_field\n    @property\n    def year(self) -> str:\n"
            "        return self.month[:4]",
            "Line.year is a computed_field",
            "@computed_field",
        ),
        (
            " = Field(serialization_alias='sum')",
            "Line.total has serialization_alias='sum'",
            "total: Decimal",
        ),
    ],
)
def test_a_result_must_read_back_from_its_json(
    write_app: WriteApp, line: str, found: str, starts: str
) -> None:
    path = write_app(VALID + RESULTS.format(line=line))
    reported = [str(p) for p in problems_of(path)]
    # Once, though two queries return the model, nested in one of them.
    (problem,) = [p for p in reported if found in p]
    assert "a test's query rebuilds it from that JSON" in problem
    where = problem.split(": ")[0]
    file, number = where.split(":")
    assert file == path.name
    assert path.read_text().splitlines()[int(number) - 1].lstrip().startswith(starts)


def problems_of(path: Path) -> list:
    with pytest.raises(AppError) as error:
        manifest(path)
    return error.value.problems


@pytest.mark.parametrize(
    "line",
    [
        " = Field(alias='sum')",
        " = Field(serialization_alias='sum', validation_alias='sum')",
        "\n    model_config = ConfigDict(validate_by_name=True)\n"
        "    other: Decimal = Field(alias='else_', serialization_alias='other')",
    ],
)
def test_a_result_that_reads_back_passes(write_app: WriteApp, line: str) -> None:
    queries = manifest(write_app(VALID + RESULTS.format(line=line)))["queries"]
    assert [q["name"] for q in queries] == ["totals", "report", "lines"]


def test_only_results_must_read_back(write_app: WriteApp) -> None:
    source = VALID.replace(
        "class Total(BaseModel):",
        "class Filter(BaseModel):\n    month: str = Field(serialization_alias='Month')\n\n\n"
        "class Total(BaseModel):",
    ).replace(
        "month: str | None = None) -> list[Total]:", "f: Filter | None = None) -> list[Total]:"
    )
    manifest(write_app(source))


def test_a_test_that_reads_data_is_refused(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_demo.py").write_text("open('data/data.csv')\n")
    path = tmp_path / "app_reading_data.py"
    path.write_text(HEADER + textwrap.dedent(VALID))
    found = problems(path)
    assert "tests/test_demo.py:1: 'data/data.csv' names the app's data/ folder" in found


def test_an_action_result_must_be_json(write_app: WriteApp) -> None:
    source = VALID.replace("amount: Decimal) -> None:", "amount: Decimal) -> type:")
    assert "its result can't be described as JSON" in problems(write_app(source))


def test_the_app_s_own_names_are_reserved(write_app: WriteApp) -> None:
    for name in ("app_history", "App_Totals"):
        source = VALID.replace("def totals(", f"def {name}(")
        kept = f"'{name}' starts with app_, which is kept for the tools every Cliquet app has"
        assert kept in problems(write_app(source))
    action = VALID.replace(
        "amount: Decimal) -> None:", "amount: Decimal, app_transaction: str) -> None:"
    )
    assert "`app_transaction` starts with app_" in problems(write_app(action))
    alias = 'month: Annotated[str | None, Field(alias="APP_month")] = None)'
    query = VALID.replace("month: str | None = None)", alias)
    assert "`APP_month` starts with app_" in problems(write_app(query))


def test_a_result_s_fields_are_not_cliquet_s(write_app: WriteApp) -> None:
    # Cliquet adds app_data_version to a query's answer, beside the app's own fields.
    one = VALID.replace(") -> list[Total]:", ") -> Total:").replace("return []", "return None")
    named = one.replace("    total: Decimal\n", "    total: Decimal\n    app_data_version: str\n")
    told = "`app_data_version` starts with app_, which is kept for what Cliquet adds to an answer"
    assert told in problems(write_app(named))
    alias = '    total: Decimal\n    seen: Annotated[str, Field(alias="app_seen")]\n'
    aliased = one.replace("    total: Decimal\n", alias)
    assert "`app_seen` starts with app_" in problems(write_app(aliased))
    # A list's items are the app's own: Cliquet adds nothing to them.
    in_a_list = named.replace(") -> Total:", ") -> list[Total]:")
    assert manifest(write_app(in_a_list))["queries"][0]["name"] == "totals"


def test_other_names_are_the_app_s(write_app: WriteApp) -> None:
    # The built-in tools and the arguments Cliquet adds all start with app_, so an app
    # may use their plain names, and the prefix Cliquet used before.
    source = VALID.replace("def totals(", "def history(").replace(
        "amount: Decimal) -> None:", "amount: Decimal, transaction: str, request_id: str) -> None:"
    )
    m = manifest(write_app(source))
    assert [q["name"] for q in m["queries"]] == ["history"]
    assert {"transaction", "request_id"} <= set(m["actions"][0]["input_schema"]["properties"])
    source = VALID.replace("def totals(", "def cliquet_totals(").replace(
        "amount: Decimal) -> None:", "amount: Decimal, cliquet_note: str) -> None:"
    )
    m = manifest(write_app(source))
    assert [q["name"] for q in m["queries"]] == ["cliquet_totals"]
    assert "cliquet_note" in m["actions"][0]["input_schema"]["properties"]


def test_a_view_s_title_is_its_tab_s_wording(write_app: WriteApp) -> None:
    plain = '@demo.view(roles=["owner", "reader"])'
    titled = '@demo.view(roles=["owner", "reader"], title=" Résultats du mois ")'
    (overview,) = manifest(write_app(VALID.replace(plain, titled)))["views"]
    assert overview["name"] == "overview"
    assert overview["title"] == "Résultats du mois"
    # A title is in any language, and as long as a tab's wording: 40 characters.
    longest = "Résultats : " + "é" * 28
    titled = f'@demo.view(roles=["owner", "reader"], title="{longest}")'
    assert manifest(write_app(VALID.replace(plain, titled)))["views"][0]["title"] == longest
    # An accent written as its own character counts with its letter, as a person counts.
    apart = unicodedata.normalize("NFD", longest)
    assert len(apart) > 40
    titled = f'@demo.view(roles=["owner", "reader"], title="{apart}")'
    assert manifest(write_app(VALID.replace(plain, titled)))["views"][0]["title"] == longest


@pytest.mark.parametrize(
    ("title", "message"),
    [
        ('""', "title is empty. Write the tab's wording, or leave title out."),
        ('"   "', "title is empty"),
        ('"\\u200b"', "title is empty"),  # a zero-width space draws nothing
        (
            '"x" * 41',
            "title is 41 characters long, and the tab's wording is at most 40. Shorten it.",
        ),
        (
            '"Sales\\ntoday"',
            "title has a line break or a control character. Write the tab's wording on one line.",
        ),
        ("3", "title=3 isn't text. Write the tab's wording, such as title=\"Sales\"."),
    ],
)
def test_a_view_s_title_is_one_short_line(write_app: WriteApp, title: str, message: str) -> None:
    titled = f'@demo.view(roles=["owner", "reader"], title={title})'
    source = VALID.replace('@demo.view(roles=["owner", "reader"])', titled)
    found = problems(write_app(source))
    assert message in found
    assert "overview: title" in found  # the problem names the view


def test_a_tool_s_name_is_one_every_mcp_client_accepts(write_app: WriteApp) -> None:
    # A view named in the person's language: the name becomes ASCII, and the wording a title.
    view = problems(write_app(VALID.replace("def overview(", "def résultats(")))
    assert (
        "résultats: `résultats` isn't a name every MCP client accepts. Rename the function to "
        "`resultats`: use lower-case ASCII letters, digits and _, starting with a letter; "
        'for the tab\'s wording, pass title="Résultats" to its view decorator.'
    ) in view
    several = problems(write_app(VALID.replace("def overview(", "def résultats_du_mois(")))
    assert "to `resultats_du_mois`" in several and 'title="Résultats du mois"' in several
    # An action or a query has no tab, so nothing is said of a title.
    query = problems(write_app(VALID.replace("def totals(", "def totaux_été(")))
    assert "Rename the function to `totaux_ete`" in query and "title=" not in query
    action = problems(write_app(VALID.replace("def add(", "def addAmount(")))
    assert "`addAmount` isn't a name every MCP client accepts" in action
    assert "Rename the function to `add_amount`" in action and "title=" not in action
    # A name that starts with something else than a letter.
    private = problems(write_app(VALID.replace("def totals(", "def _totals(")))
    assert "Rename the function to `totals`" in private
    # No name is suggested that would be refused in turn, as one of Cliquet's.
    kept = problems(write_app(VALID.replace("def totals(", "def _app_totals(")))
    assert "`_app_totals` isn't a name every MCP client accepts. Rename the function: use" in kept
    # A script with no ASCII form: the rule alone, and the wording as the title.
    other = problems(write_app(VALID.replace("def overview(", "def 結果(")))
    assert "`結果` isn't a name every MCP client accepts. Rename the function: use" in other
    assert 'pass title="結果" to its view decorator' in other
    # An ASCII name that is only in the wrong case needs no title.
    upper = problems(write_app(VALID.replace("def overview(", "def Overview(")))
    assert "Rename the function to `overview`" in upper and "title=" not in upper


def test_an_argument_s_name_is_one_every_mcp_client_accepts(write_app: WriteApp) -> None:
    action = VALID.replace("amount: Decimal) -> None:", "amount: Decimal, quantité: int) -> None:")
    assert (
        "add: `quantité` isn't a name every MCP client accepts. Rename the argument to "
        "`quantite`: use lower-case ASCII letters, digits and _, starting with a letter"
    ) in problems(write_app(action))
    alias = 'month: Annotated[str | None, Field(alias="theMonth")] = None) -> list[Total]:'
    query = VALID.replace("month: str | None = None) -> list[Total]:", alias)
    assert "Rename the argument's alias to `the_month`" in problems(write_app(query))
    view = "def overview(ctx: QueryContext, month: str | None = None) -> Screen:"
    found = problems(write_app(VALID.replace(view, view.replace("month: str", "Month: str"))))
    assert "overview: `Month` isn't a name every MCP client accepts" in found
    assert "title=" not in found
    # An argument's Python name may end with _, as one that hides a type does.
    assert "date_" in VALID
    manifest(write_app(VALID))
    # A name that isn't ASCII gave a wording: a title keeps it, and the fix passes.
    hint = 'letter; for its wording, write its type as Annotated[..., Field(title="Quantité")].'
    assert hint in problems(write_app(action))
    titled = 'quantite: Annotated[int, Field(title="Quantité")]) -> None:'
    (add,) = manifest(write_app(action.replace("quantité: int) -> None:", titled)))["actions"]
    assert add["input_schema"]["properties"]["quantite"]["title"] == "Quantité"
    # Some clients accept a key in a tool's schema of 64 characters at most.
    long = "q" * 65
    found = problems(write_app(action.replace("quantité", long)))
    assert (
        f"add: `{long}` is 65 characters long, and some MCP clients accept a name only up to "
        "64. Shorten the argument."
    ) in found
    manifest(write_app(action.replace("quantité", "q" * 64)))
    # A name both too long and not plain: what it suggests would be refused, so it says the rule.
    found = problems(write_app(action.replace("quantité", f"é{long}")))
    assert "Rename the argument: use lower-case ASCII letters, digits and _" in found
    assert "starting with a letter, 64 characters at most" in found
    # The wording of an argument with an alias is its alias's, which renaming it keeps.
    aliased = 'quantité: Annotated[int, Field(alias="quantity")]) -> None:'
    found = problems(write_app(action.replace("quantité: int) -> None:", aliased)))
    assert "Rename the argument to `quantite`" in found and "title=" not in found


# An app whose model `Line` has a field named in the person's language.
LINES = VALID.replace(
    "class Total(BaseModel):",
    "class Line(BaseModel):\n    quantité: int\n\n\n"
    "class Order(BaseModel):\n    lines: list[Line]\n\n\n"
    "class Total(BaseModel):",
)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # In what an action, a query or a view takes, at any depth.
        ("amount: Decimal) -> None:", "amount: Decimal, lines: list[Line]) -> None:"),
        ("amount: Decimal) -> None:", "amount: Decimal, line: Line | None = None) -> None:"),
        ("amount: Decimal) -> None:", "amount: Decimal, either: Line | int = 0) -> None:"),
        ("amount: Decimal) -> None:", "amount: Decimal, by_sku: dict[str, Line]) -> None:"),
        ("amount: Decimal) -> None:", "amount: Decimal, order: Order) -> None:"),
        ("month: str | None = None) -> Screen:", "line: Line | None = None) -> Screen:"),
        # In what an action or a query returns.
        ("amount: Decimal) -> None:", "amount: Decimal) -> Order:"),
        ("-> list[Total]:", "-> list[Line]:"),
        ("-> list[Total]:", "-> Order | None:"),
    ],
)
def test_a_field_s_name_is_one_every_mcp_client_accepts(
    write_app: WriteApp, old: str, new: str
) -> None:
    path = write_app(LINES.replace(old, new))
    # Said for the file: the problem names the model, wherever it is used.
    assert problems(path) == (
        f"{path.name}: `quantité` isn't a name every MCP client accepts. Rename the field of "
        "the model `Line` to `quantite`: use lower-case ASCII letters, digits and _, starting "
        'with a letter; for its wording, write its type as Annotated[..., Field(title="Quantité")].'
    )
    # What the problem says to do passes.
    fixed = LINES.replace("quantité: int", 'quantite: Annotated[int, Field(title="Quantité")]')
    manifest(write_app(fixed.replace(old, new)))


TAKES_LINES = ("amount: Decimal) -> None:", "amount: Decimal, lines: list[Line]) -> None:")


def test_a_field_s_problem_is_said_once(write_app: WriteApp) -> None:
    # A model that an action takes and a query returns: one problem, not one for each tool.
    source = LINES.replace(*TAKES_LINES).replace("-> list[Total]:", "-> list[Line]:")
    assert problems(write_app(source)).count("isn't a name every MCP client accepts") == 1


def test_a_field_s_alias_is_its_name_for_a_client(write_app: WriteApp) -> None:
    alias = LINES.replace("quantité: int", 'quantity: int = Field(alias="Quantity")')
    assert (
        "`Quantity` isn't a name every MCP client accepts. Rename the alias of the field "
        "`quantity` of the model `Line` to `quantity`: use lower-case ASCII letters, digits "
        "and _, starting with a letter."
    ) in problems(write_app(alias.replace(*TAKES_LINES)))
    # What a tool returns goes by the alias it is serialized with.
    total = '    total: Decimal = Field(serialization_alias="theTotal")\n'
    returned = VALID.replace("    total: Decimal\n", total)
    found = problems(write_app(returned))
    assert "Rename the alias of the field `total` of the model `Total` to `the_total`" in found
    # A Python name no client gets is the app's own.
    hidden = LINES.replace("quantité: int", 'quantité: int = Field(alias="quantity")')
    manifest(write_app(hidden.replace(*TAKES_LINES)))


@pytest.mark.parametrize("model", ["class Line(TypedDict):", "@dataclass\nclass Line:"])
def test_every_kind_of_model_has_its_fields_named_so(write_app: WriteApp, model: str) -> None:
    imports = "from dataclasses import dataclass\nfrom typing import Annotated, TypedDict"
    source = LINES.replace("class Line(BaseModel):", model).replace(
        "from typing import Annotated", imports
    )
    found = problems(write_app(source.replace(*TAKES_LINES)))
    assert "Rename the field of the model `Line` to `quantite`" in found


def test_what_a_client_gets_as_data_stays_free(write_app: WriteApp) -> None:
    fields = (
        # A dict's keys and a choice's values are data, `app_` is the app's in a model, and a
        # field named as a JSON schema's keyword is one field.
        'quantity: dict[str, int]\n    season: Literal["Été", "Hiver"] = "Été"\n'
        "    app_total: int = 0\n    properties: int = 0\n    App_Kind: int = 0"
    )
    source = LINES.replace("quantité: int", fields).replace(
        "from typing import Annotated", "from typing import Annotated, Literal"
    )
    found = problems(write_app(source.replace(*TAKES_LINES)))
    # Only a capital is refused, and in a model, app_kind is a name the app may have.
    assert found.count("isn't a name") == 1
    assert "`App_Kind` isn't a name every MCP client accepts" in found
    assert "of the model `Line` to `app_kind`" in found
    # A field's name is 64 characters at most, as an argument's.
    long = "q" * 65
    source = LINES.replace("quantité", long).replace(*TAKES_LINES)
    assert (
        f"`{long}` is 65 characters long, and some MCP clients accept a name only up to 64. "
        "Shorten the field of the model `Line`."
    ) in problems(write_app(source))


def test_a_tool_s_full_name_fits_every_mcp_client(write_app: WriteApp) -> None:
    # mcp__demo__ leaves 53 characters of the 64 some clients accept.
    fits, long = "t" * 53, "t" * 54
    m = manifest(write_app(VALID.replace("def totals(", f"def {fits}(")))
    assert [q["name"] for q in m["queries"]] == [fits]
    assert (
        f"{long}: the name is too long. Some MCP clients accept a tool's full name only up to "
        f"64 characters, and mcp__demo__{long} is 65. Shorten the function's name to at most "
        "53 characters."
    ) in problems(write_app(VALID.replace("def totals(", f"def {long}(")))
    for old in ("def add(", "def overview("):
        assert "the name is too long" in problems(write_app(VALID.replace(old, f"def {long}(")))
    # When the app's name is the longer of the two, the problem says to shorten that one.
    app, tool = "d" * 33, "t" * 25
    source = VALID.replace('App("demo"', f'App("{app}"').replace("def totals(", f"def {tool}(")
    assert (
        f"and mcp__{app}__{tool} is 65. Shorten the app's name, the longer of the two, to at "
        "most 32 characters, or the function's to at most 24 characters."
    ) in problems(write_app(source))


def test_an_app_s_name_leaves_room_for_the_built_in_tools(write_app: WriteApp) -> None:
    # The longest built-in tool, app_rollback_transaction, must fit too: 33 characters at most.
    fits, long = "d" * 33, "d" * 34
    assert manifest(write_app(VALID.replace('App("demo"', f'App("{fits}"')))["name"] == fits
    with pytest.raises(AppError) as error:
        manifest(write_app(VALID.replace('App("demo"', f'App("{long}"')))
    assert (
        "the name is 34 characters long, and an app's name is at most 33. Some MCP clients "
        f"accept a tool's full name only up to 64 characters, and mcp__{long}__"
        "app_rollback_transaction, a tool every Cliquet app has, is 65. Shorten the app's name."
    ) in str(error.value)


def test_a_view_takes_ctx_and_returns_a_screen(write_app: WriteApp) -> None:
    view = "def overview(ctx: QueryContext, month: str | None = None) -> Screen:"
    no_ctx = problems(write_app(VALID.replace(view, "def overview() -> Screen:")))
    assert "take `ctx: QueryContext` as the first argument" in no_ctx
    no_result = view.replace("-> Screen:", "-> str:")
    found = problems(write_app(VALID.replace(view, no_result)))
    assert "annotate `-> Screen`: the screen it lays out, from cliquet" in found
    untyped = problems(write_app(VALID.replace(view, view.replace("month: str | None", "month"))))
    assert "annotate `month` with its type" in untyped
    kept = view.replace("month: str", "app_month: str")
    assert "`app_month` starts with app_" in problems(write_app(VALID.replace(view, kept)))


def test_each_argument_of_a_view_has_a_default(write_app: WriteApp) -> None:
    # A view opens from the app's tabs with no arguments.
    view = "def overview(ctx: QueryContext, month: str | None = None) -> Screen:"
    required = view.replace("month: str | None = None", "month: str, day: int = 1")
    found = problems(write_app(VALID.replace(view, required)))
    assert "give `month` a default: a view opens from the app's tabs with no arguments" in found
    assert "give `day` a default" not in found


def test_a_view_needs_a_docstring(write_app: WriteApp) -> None:
    source = VALID.replace('    """The totals, on a screen."""\n', "")
    assert "overview: add a docstring" in problems(write_app(source))


def test_a_view_shares_its_name_with_no_tool(write_app: WriteApp) -> None:
    source = VALID.replace("def overview(", "def totals(")
    assert "two actions, queries or views are named 'totals'" in problems(write_app(source))


def test_an_app_without_views_lists_none(write_app: WriteApp) -> None:
    m = manifest(write_app(VALID[: VALID.index("@demo.view")]))
    assert m["views"] == []


def test_reports_every_problem_at_once(write_app: WriteApp) -> None:
    source = VALID.replace('"""Add an amount."""', "pass").replace("-> Screen:", "-> None:")
    text = problems(write_app(source))
    assert "add a docstring" in text and "annotate `-> Screen`" in text


def test_names_must_be_unique(write_app: WriteApp) -> None:
    source = (
        VALID
        + '''

@demo.query(roles=["owner"])
def add(ctx: QueryContext) -> int:
    """Clash."""
    return 0
'''
    )
    assert "two actions, queries or views are named 'add'" in problems(write_app(source))


def test_requires_a_script_block_pinning_the_sdk(write_app: WriteApp) -> None:
    assert "PEP 723 block" in problems(write_app(VALID, header=""))
    header = '# /// script\n# dependencies = ["beancount"]\n# ///\n'
    assert "add the SDK to the block's dependencies" in problems(write_app(VALID, header=header))


def test_requires_exactly_one_app(write_app: WriteApp) -> None:
    path = write_app(VALID + '\nother = App("other", files=["x.csv"], roles=["owner"])\n')
    with pytest.raises(AppError, match="define exactly one App"):
        load_app(path, module_name(path))
    path = write_app("x = 1\n")
    with pytest.raises(AppError, match=r"found none"):
        load_app(path, module_name(path))


def test_loading_reuses_a_module_already_imported(write_app: WriteApp) -> None:
    path = write_app(VALID)
    name = module_name(path)
    first = load_app(path, name)
    assert load_app(path, name).module is first.module
    other = write_app(VALID)
    with pytest.raises(AppError, match="already used"):
        load_app(other, name)
    del sys.modules[name]


def test_read_script_metadata() -> None:
    source = (
        '# /// script\n# requires-python = ">=3.14"\n# dependencies = ["a", "b"]\n# ///\nx = 1\n'
    )
    assert read_script_metadata(source) == {"requires-python": ">=3.14", "dependencies": ["a", "b"]}
    assert read_script_metadata("x = 1\n") is None


def test_the_stock_example() -> None:
    m = build_manifest(APPS / "stock" / "app.py", "cliquet_test_stock")
    actions = {a["name"]: a for a in m["actions"]}
    queries = {q["name"]: q for q in m["queries"]}
    assert actions["record_sale"]["roles"] == ["owner", "staff"]
    assert actions["set_price"]["roles"] == ["owner"]
    assert queries["sales"]["roles"] == ["owner"]
    lines = actions["receive_delivery"]["input_schema"]["properties"]["lines"]
    assert lines["items"] == {"$ref": "#/$defs/DeliveryLine"}
    assert queries["sales"]["output_schema"]["items"] == {"$ref": "#/$defs/ProductSales"}
    assert len(m["invariants"]) == 3


NO_ROLES = """
\"\"\"Notes, for anyone.\"\"\"

from cliquet import ActionContext, App, QueryContext, Screen

notes = App("notes", files=["notes.md"])


@notes.action
def add(ctx: ActionContext, text: str) -> None:
    \"\"\"Add a note.\"\"\"


@notes.query
def count(ctx: QueryContext) -> int:
    \"\"\"How many notes there are.\"\"\"
    return 0


@notes.view
def overview(ctx: QueryContext) -> Screen:
    \"\"\"The notes, on a screen.\"\"\"
    return Screen("<h2>Notes</h2>")
"""


def test_an_app_without_roles_lets_anyone_use_everything(write_app: WriteApp) -> None:
    m = manifest(write_app(NO_ROLES))
    assert m["roles"] == []
    assert m["actions"][0]["roles"] is None
    assert m["queries"][0]["roles"] is None
    assert m["views"][0]["roles"] is None


def test_a_role_may_not_take_a_word_of_the_app_s_address(write_app: WriteApp) -> None:
    source = NO_ROLES.replace(
        'App("notes", files=["notes.md"])', 'App("notes", files=["notes.md"], roles=["mcp"])'
    )
    assert "role 'mcp': the name is taken by Cliquet. Choose another." in problems(
        write_app(source)
    )
    m = manifest(write_app(source.replace('roles=["mcp"]', 'roles=["agent"]')))
    assert m["roles"] == ["agent"]


def test_roles_must_be_declared_before_a_function_names_them(write_app: WriteApp) -> None:
    source = NO_ROLES.replace("@notes.action\n", '@notes.action(roles=["owner"])\n')
    assert "the app declares no roles" in problems(write_app(source))


def test_a_single_role_is_the_default(write_app: WriteApp) -> None:
    source = NO_ROLES.replace('files=["notes.md"])', 'files=["notes.md"], roles=["owner"])')
    m = manifest(write_app(source))
    assert m["roles"] == ["owner"]
    assert m["actions"][0]["roles"] == ["owner"]
    assert m["queries"][0]["roles"] == ["owner"]
    assert m["views"][0]["roles"] == ["owner"]


def test_with_several_roles_each_function_names_its_roles(write_app: WriteApp) -> None:
    source = VALID.replace('@demo.action(roles=["owner"])', "@demo.action")
    text = problems(write_app(source))
    assert "the app has several roles (owner, reader). Say which may use it" in text


def test_requires_a_docstring_describing_the_app(write_app: WriteApp) -> None:
    source = VALID.replace('"""Monthly totals, kept by an agent."""\n', "")
    assert "add a docstring at the top of app.py" in problems(write_app(source))
    assert manifest(write_app(VALID))["description"] == "Monthly totals, kept by an agent."


def test_argument_descriptions_and_constraints_reach_the_schema(write_app: WriteApp) -> None:
    source = VALID.replace(
        "amount: Decimal) -> None:",
        'amount: Annotated[Decimal, Field(gt=0, description="Euros")]) -> None:',
    )
    amount = manifest(write_app(source))["actions"][0]["input_schema"]["properties"]["amount"]
    assert amount["description"] == "Euros"
    assert {"exclusiveMinimum": 0.0, "type": "number"} in amount["anyOf"]


SDK = Path("/opt/cliquet/packages/sdk")


def block(*lines: str) -> str:
    return "".join(f"# {line}\n" if line else "#\n" for line in ["/// script", *lines, "///"])


def test_an_sdk_with_a_local_source_is_fine() -> None:
    source = block(
        'dependencies = ["cliquet-sdk>=0.0.1,<0.1", "beancount>=3.2,<4"]',
        "",
        "[tool.uv.sources]",
        'cliquet-sdk = { path = "/opt/cliquet/packages/sdk", editable = true }',
    )
    assert sdk_source_problem(source, SDK) is None


def test_an_sdk_without_a_source_gets_the_line_to_add() -> None:
    problem = sdk_source_problem(block('dependencies = ["cliquet-sdk>=0.0.1,<0.1"]'), SDK)
    assert problem is not None
    assert "package index" in problem
    assert '# cliquet-sdk = { path = "/opt/cliquet/packages/sdk", editable = true }' in problem
    assert "# [tool.uv.sources]" in problem


def test_an_sdk_without_a_known_folder_gets_a_placeholder() -> None:
    problem = sdk_source_problem(block('dependencies = ["cliquet-sdk"]'), None)
    assert problem is not None and '"/path/to/cliquet/packages/sdk"' in problem


def test_only_a_block_without_uv_settings_gets_the_sdk_folder_filled_in() -> None:
    plain = block('dependencies = ["cliquet-sdk"]')
    settings = block('dependencies = ["cliquet-sdk"]', "", "[tool.uv]", "no-sources = true")
    assert without_uv_settings(plain)
    assert not without_uv_settings(settings)
    assert not without_uv_settings("x = 1\n")
    # Without a known folder, the variable that names one helps only where it is filled in.
    plain_problem = sdk_source_problem(plain, None)
    assert plain_problem is not None and "CLIQUET_SDK_PATH" in plain_problem
    settings_problem = sdk_source_problem(settings, None)
    assert settings_problem is not None and "CLIQUET_SDK_PATH" not in settings_problem


def test_a_source_that_is_not_a_folder_is_refused() -> None:
    source = block(
        'dependencies = ["cliquet-sdk"]',
        "",
        "[tool.uv.sources]",
        'cliquet-sdk = { index = "somewhere" }',
    )
    assert sdk_source_problem(source, SDK) is not None


@pytest.mark.parametrize(
    ("extra", "said"),
    [
        # A folder uv may still skip, or use only where a marker holds, is no folder.
        ("[tool.uv]\nno-sources = true", "sets no-sources"),
        ('[tool.uv]\nno-sources-package = ["cliquet-sdk"]', "sets no-sources-package"),
        ('[tool.uv]\noverride-dependencies = ["cliquet-sdk==9"]', "sets override-dependencies"),
        ("", "a direct reference"),
    ],
)
def test_a_folder_for_the_sdk_doesn_t_excuse_other_uv_settings(extra: str, said: str) -> None:
    dependencies = '"cliquet-sdk"' if extra else '"cliquet-sdk", "evil @ https://example.com/e.whl"'
    lines = [
        f"dependencies = [{dependencies}]",
        "",
        "[tool.uv.sources]",
        'cliquet-sdk = { path = "/opt/cliquet/packages/sdk", editable = true }',
        *extra.splitlines(),
    ]
    problem = sdk_source_problem(block(*lines), SDK)
    assert problem is not None and said in problem and "PyPI" in problem


@pytest.mark.parametrize(
    "source",
    [
        'cliquet-sdk = { path = "/opt/cliquet/packages/sdk", marker = "sys_platform == \'nope\'" }',
        'cliquet-sdk = { path = "/opt/cliquet/packages/sdk", extra = "x" }',
        'cliquet-sdk = [{ path = "/opt/cliquet/packages/sdk" }]',
    ],
)
def test_a_folder_that_only_holds_sometimes_is_refused(source: str) -> None:
    lines = ['dependencies = ["cliquet-sdk"]', "", "[tool.uv.sources]", source]
    problem = sdk_source_problem(block(*lines), SDK)
    assert problem is not None and "package index" in problem


def test_a_lock_takes_the_sdk_from_a_folder() -> None:
    for lock in [*EXAMPLES.glob("*/app.py.lock"), *APPS.glob("*/app.py.lock")]:
        assert lock_source_problem(lock.read_text()) is None
    elsewhere = (
        'version = 1\n\n[[package]]\nname = "cliquet-sdk"\nversion = "0.0.1"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    problem = lock_source_problem(elsewhere)
    assert problem is not None and "Remove app.py.lock" in problem
    assert lock_source_problem(elsewhere.replace("cliquet-sdk", "Cliquet_SDK")) == problem
    unreadable = lock_source_problem("[[package]\n")
    assert unreadable is not None and "can't be read" in unreadable


def test_uv_runs_with_no_configuration_and_every_source(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("UV_NO_SOURCES", "UV_NO_SOURCES_PACKAGE", "UV_CONFIG_FILE"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("UV_CACHE_DIR", "/tmp/kept")
    env = uv_environment()
    assert not {"UV_NO_SOURCES", "UV_NO_SOURCES_PACKAGE", "UV_CONFIG_FILE"} & set(env)
    assert env["UV_NO_CONFIG"] == "1" and env["UV_CACHE_DIR"] == "/tmp/kept"


def test_depending_on_cliquet_is_refused_even_with_the_sdk() -> None:
    source = block(
        'dependencies = ["cliquet-sdk", "Cliquet>=0.0.1"]',
        "",
        "[tool.uv.sources]",
        'cliquet-sdk = { path = "/opt/cliquet/packages/sdk", editable = true }',
    )
    problem = sdk_source_problem(source, SDK)
    assert problem is not None
    assert "`cliquet`" in problem and "isn't this SDK" in problem
    assert '"cliquet-sdk>=0.0.1,<0.1"' in problem


def test_an_app_without_the_sdk_or_a_block_is_refused() -> None:
    missing = sdk_source_problem(block('dependencies = ["beancount"]'), SDK)
    assert missing is not None and '"cliquet-sdk>=0.0.1,<0.1"' in missing
    no_block = sdk_source_problem("x = 1\n", SDK)
    assert no_block is not None and "# /// script" in no_block


@pytest.mark.parametrize("name", ["stock.csv", "2026/january.csv", "a-b_c.d", "a/" * 7 + "b"])
def test_data_names_that_pass(name: str) -> None:
    assert data_name_problem(name) is None


@pytest.mark.parametrize(
    "name",
    [
        "/x",
        "../x",
        "a/../x",
        ".git/config",
        "a/.hidden",
        "a//b",
        "a\\b",
        "a\nb",
        "a/" * 8 + "b",
        "a" * 201,
        "",
        "a/",
        "x" * 101,
    ],
)
def test_data_names(name: str) -> None:
    problem = data_name_problem(name)
    assert problem is not None
    assert repr(name) in problem
    assert "stock.csv" in problem
