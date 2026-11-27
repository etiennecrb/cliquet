from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cliquet import (
    ActionContext,
    App,
    Caller,
    InvariantContext,
    QueryContext,
    Screen,
)
from cliquet.errors import AppError
from cliquet.icons import find_icons


def test_decorators_register_and_return_the_function() -> None:
    app = App("demo", files=["data.csv"], roles=["owner", "reader"])

    def change(ctx: ActionContext, value: int) -> None:
        """Change something."""

    def rule(ctx: InvariantContext) -> list[str]:
        return []

    def ask(ctx: QueryContext) -> int:
        """Ask something."""
        return 1

    def screen(ctx: QueryContext, month: str = "") -> Screen:
        return Screen("<p>{{ month }}</p>", args={"month": month}, month=month)

    assert app.action(roles=["owner"])(change) is change
    assert app.invariant("A rule")(rule) is rule
    assert app.query(roles=["owner", "reader"])(ask) is ask
    assert app.view(roles=["reader"])(screen) is screen

    action = app.find_action(change)
    query = app.find_query(ask)
    invariant = app.find_invariant(rule)
    assert action is not None and action.roles == ("owner",)
    assert query is not None and query.roles == ("owner", "reader")
    assert invariant is not None and invariant.description == "A rule"
    view = app.find_view(screen)
    assert view is not None and view.roles == ("reader",)


def test_decorators_work_without_arguments() -> None:
    app = App("notes", files=["notes.md"])

    @app.action
    def add(ctx: ActionContext, text: str) -> None:
        """Add a note."""

    @app.query
    def count(ctx: QueryContext) -> int:
        """Count notes."""
        return 0

    @app.view
    def overview(ctx: QueryContext) -> Screen:
        return Screen("<p>Notes</p>")

    assert app.roles == ()
    action, query = app.find_action(add), app.find_query(count)
    assert action is not None and action.roles is None
    assert query is not None and query.roles is None
    assert app.views[0].roles is None


def test_a_query_can_call_another_query() -> None:
    app = App("demo", files=["data.csv"], roles=["owner"])

    @app.query(roles=["owner"])
    def base(ctx: QueryContext) -> int:
        """Base."""
        return 20

    @app.query(roles=["owner"])
    def derived(ctx: QueryContext) -> int:
        """Derived."""
        return base(ctx) + 1

    ctx = QueryContext(Caller("sam", "owner"), datetime(2026, 11, 10, tzinfo=UTC))
    assert derived(ctx) == 21


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"name": "My App"}, "the name must be lower case"),
        ({"files": []}, "declare at least one data file"),
        ({"files": ["../secret.csv"]}, "can.t be a data file"),
        ({"files": ["/etc/passwd"]}, "can.t be a data file"),
        ({"files": ["a.csv", "a.csv"]}, "declared twice"),
        ({"files": ["**/*.csv", "**/*.csv"]}, "declared twice"),
        ({"files": ["**/*"]}, "its last part is only a wildcard"),
        ({"files": ["2026/*"]}, "its last part is only a wildcard"),
        ({"files": ["*/*.csv"]}, "more than one \\*\\* or more than one \\*"),
        ({"files": ["../*.csv"]}, "can.t be declared: its part '..' starts with a dot"),
        ({"roles": []}, "Leave roles out instead"),
        ({"roles": ["Owner"]}, "role 'Owner'"),
        ({"roles": ["owner", "owner"]}, "a role is declared twice"),
        ({"accent": "teal"}, "write the accent as #rrggbb"),
        ({"accent": "#0f766"}, "write the accent as #rrggbb"),
        ({"accent": "#facc15"}, "white text on #facc15 is hard to read"),
        ({"timezone": "Paris"}, 'is not a time zone. Write its IANA name, such as "Europe/Paris"'),
        ({"timezone": "europe/paris"}, "is not a time zone"),
        ({"timezone": ["Europe/Paris"]}, "is not a time zone"),
        ({"language": "de"}, 'written in "en" or "fr" only. Choose one, or leave it out'),
        ({"language": "French"}, 'written in "en" or "fr" only'),
    ],
)
def test_app_definition_problems(kwargs: dict[str, object], message: str) -> None:
    args: dict[str, Any] = {"name": "demo", "files": ["data.csv"], "roles": ["owner"]}
    args.update(kwargs)
    with pytest.raises(AppError, match=message):
        App(args.pop("name"), **args)


def test_a_name_and_a_pattern_that_overlap_are_declared_together() -> None:
    app = App("ledger", files=["ledger.beancount", "**/*.beancount"])
    assert app.files == ("ledger.beancount", "**/*.beancount")


def test_a_bad_accent_is_refused() -> None:
    with pytest.raises(AppError, match="such as #0f766e"):
        App("demo", files=["data.csv"], accent="#fde047")
    assert App("demo", files=["data.csv"], accent="#0F766E").accent == "#0f766e"
    assert App("demo", files=["data.csv"]).accent is None


def test_an_app_s_icon_is_one_of_lucide_s_names() -> None:
    app = App("films", files=["films.csv"], accent="#be123c", icon="clapperboard")
    assert app.icon == "clapperboard"
    assert App("demo", files=["data.csv"]).icon is None
    # A misspelt name gets the names spelt like it; a subject gets the icons filed under it.
    with pytest.raises(AppError, match="Did you mean clapperboard") as misspelt:
        App("films", files=["films.csv"], icon="clapperbord")
    assert "find_icons" in str(misspelt.value)
    with pytest.raises(AppError, match="isn't one of Lucide's icons") as subject:
        App("films", files=["films.csv"], icon="movie")
    assert "clapperboard" in str(subject.value)
    with pytest.raises(AppError, match="isn't one of Lucide's icons"):
        App("films", files=["films.csv"], icon=["film"])  # ty: ignore[invalid-argument-type]


def test_find_icons_searches_names_then_tags() -> None:
    found = find_icons("film")
    assert found[0] == "film", "a name that holds the text comes first"
    assert "clapperboard" in found, "Lucide files clapperboard under film"
    assert {"piggy-bank", "banknote"} <= set(find_icons("Money"))
    assert find_icons("") == [] and find_icons("zzzz") == []


def test_an_app_s_timezone_is_utc_unless_it_declares_one() -> None:
    assert App("demo", files=["data.csv"]).timezone == "UTC"
    assert App("demo", files=["data.csv"], timezone="Asia/Tokyo").timezone == "Asia/Tokyo"


def test_now_must_be_timezone_aware() -> None:
    caller = Caller("sam", "owner")
    with pytest.raises(ValueError, match="timezone-aware"):
        ActionContext(caller, datetime(2026, 11, 10))
    with pytest.raises(ValueError, match="timezone-aware"):
        QueryContext(caller, datetime(2026, 11, 10))


def test_invariant_context_points_to_the_previous_state(tmp_path: Path) -> None:
    ctx = InvariantContext(Caller("sam", "owner"), tmp_path, "add_note")
    assert ctx.before == tmp_path and ctx.action == "add_note"
