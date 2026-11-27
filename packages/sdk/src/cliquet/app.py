"""The `App` object and its decorators. Decorators only record metadata."""

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any, Concatenate, Protocol, overload
from zoneinfo import available_timezones

from cliquet.context import ActionContext, InvariantContext, QueryContext
from cliquet.errors import AppError, Problem
from cliquet.files import declared_problem
from cliquet.icons import icon_problem
from cliquet.tools import BUILT_IN, MAX_APP_NAME, MAX_FULL_NAME, full_name
from cliquet.vocabulary import LANGUAGES

if TYPE_CHECKING:
    from cliquet.screens import Screen

type Invariant = Callable[[InvariantContext], list[str]]
type View = Callable[..., Screen]

_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
# The words the server uses right under an app's address, `/apps/<login>/<app>/<word>`, where
# a role has its page: no role may be named after one. A word there that no role could hold, such
# as an app's own icon, `icon.svg`, needs no entry, and a test keeps this set and the server's
# role-shaped words equal.
RESERVED_ROLES = frozenset({"mcp"})
_ACCENT = re.compile(r"^#[0-9a-fA-F]{6}$")
# Button text on the accent is white. WCAG AA asks 4.5:1 for normal text.
_MIN_CONTRAST = 4.5


class ActionDecorator(Protocol):
    def __call__[**P, R](
        self, fn: Callable[Concatenate[ActionContext, P], R], /
    ) -> Callable[Concatenate[ActionContext, P], R]: ...


class QueryDecorator(Protocol):
    def __call__[**P, R](
        self, fn: Callable[Concatenate[QueryContext, P], R], /
    ) -> Callable[Concatenate[QueryContext, P], R]: ...


class ViewDecorator(Protocol):
    def __call__[**P](
        self, fn: Callable[Concatenate[QueryContext, P], Screen], /
    ) -> Callable[Concatenate[QueryContext, P], Screen]: ...


@dataclass(frozen=True)
class ActionSpec:
    fn: Callable[..., Any]
    roles: tuple[str, ...] | None
    destructive: bool


@dataclass(frozen=True)
class InvariantSpec:
    fn: Callable[..., Any]
    description: str


@dataclass(frozen=True)
class QuerySpec:
    fn: Callable[..., Any]
    roles: tuple[str, ...] | None


@dataclass(frozen=True)
class ViewSpec:
    fn: View
    roles: tuple[str, ...] | None
    # The wording of the view's tab, when the app chose it.
    title: str | None = None


class App:
    """A Cliquet app: its data files, its roles, and the functions registered on it.

    `files` are the only files in a working copy, and the only ones app code may read,
    write or create. Each is a name, such as "stock.csv", or a pattern of names: `*` matches
    any characters but `/` inside a part, and `**`, a whole part, any folders, none included,
    as `Path.glob` means them. So "**/*.beancount" declares "ledger.beancount" and
    "2026/01.beancount", whether they exist yet or not, and an action may create one. A pattern
    has at most one `**` and one `*`, and its last part has more than a wildcard.
    `roles` is optional. Without roles, anyone who can open the app may use all of it. With
    one role, functions get it by default. With several, each action, query and view must say
    which roles may use it. `accent` is the app's colour, as `#rrggbb`: its screens and pages
    use it for buttons and highlights. `icon` is the name of one of Lucide's icons, such as
    "clapperboard": the app's tile shows it, on the accent, in place of the app's first letter.
    `timezone` is the IANA name of the zone `ctx.now` is in, such as "Europe/Paris", the
    same for every caller. Without one, it is UTC. `title` is the app's name as people read
    it, such as "FoodBank HQ", on its tile and its pages. Without one, it is `name`, with
    a capital first letter and spaces for `-` and `_`. `language` is the language the app's
    screens are written in, "en" or "fr": a grid's own words, such as Search and Filters, and
    its numbers and dates are written in it. Without one, it is English.
    """

    def __init__(
        self,
        name: str,
        *,
        files: list[str],
        roles: list[str] | None = None,
        accent: str | None = None,
        icon: str | None = None,
        timezone: str | None = None,
        title: str | None = None,
        language: str | None = None,
    ) -> None:
        problems = _check_app(name, files, roles) + _check_accent(name, accent)
        problems += _check_icon(name, icon)
        problems += _check_timezone(name, timezone)
        problems += _check_language(name, language)
        if problems:
            raise AppError(problems)
        self.name = name
        self.accent = accent.lower() if accent else None
        self.icon = icon
        self.timezone = timezone or "UTC"
        self.language = language or "en"
        # The app's name as people read it, when the app chose it. The manifest checks it.
        self.title = title
        # The module that creates the app, such as `app`. The test harness serves its file.
        self.module_name: str = sys._getframe(1).f_globals.get("__name__", "")
        self.files = tuple(files)
        self.roles = tuple(roles or ())
        self.actions: list[ActionSpec] = []
        self.invariants: list[InvariantSpec] = []
        self.queries: list[QuerySpec] = []
        self.views: list[ViewSpec] = []

    def __repr__(self) -> str:
        return f"App({self.name!r})"

    @overload
    def action[**P, R](
        self, fn: Callable[Concatenate[ActionContext, P], R], /
    ) -> Callable[Concatenate[ActionContext, P], R]: ...
    @overload
    def action(
        self, /, *, roles: list[str] | None = None, destructive: bool = False
    ) -> ActionDecorator: ...
    def action(
        self,
        fn: Callable[..., Any] | None = None,
        /,
        *,
        roles: list[str] | None = None,
        destructive: bool = False,
    ) -> Any:
        """Register a change to the data, which the given roles may make.

        It returns None, or a result that says what it did, annotated like a query's.
        `destructive` says it removes what one call can't add back, so a client asks first.
        """

        def register(f: Callable[..., Any]) -> Callable[..., Any]:
            self.actions.append(ActionSpec(f, None if roles is None else tuple(roles), destructive))
            return f

        return register(fn) if fn is not None else register

    def invariant(self, description: str) -> Callable[[Invariant], Invariant]:
        """Register something that must always hold, shown to people by `description`."""

        def register(fn: Invariant) -> Invariant:
            self.invariants.append(InvariantSpec(fn, description))
            return fn

        return register

    @overload
    def query[**P, R](
        self, fn: Callable[Concatenate[QueryContext, P], R], /
    ) -> Callable[Concatenate[QueryContext, P], R]: ...
    @overload
    def query(self, /, *, roles: list[str] | None = None) -> QueryDecorator: ...
    def query(
        self, fn: Callable[..., Any] | None = None, /, *, roles: list[str] | None = None
    ) -> Any:
        """Register a question about the data, which the given roles may ask."""

        def register(f: Callable[..., Any]) -> Callable[..., Any]:
            self.queries.append(QuerySpec(f, None if roles is None else tuple(roles)))
            return f

        return register(fn) if fn is not None else register

    @overload
    def view[**P](
        self, fn: Callable[Concatenate[QueryContext, P], Screen], /
    ) -> Callable[Concatenate[QueryContext, P], Screen]: ...
    @overload
    def view(
        self, /, *, roles: list[str] | None = None, title: str | None = None
    ) -> ViewDecorator: ...
    def view(
        self,
        fn: Callable[..., Any] | None = None,
        /,
        *,
        roles: list[str] | None = None,
        title: str | None = None,
    ) -> Any:
        """Register a screen, which the given roles may open. It takes typed arguments, each
        with a default, and returns a `Screen`.

        `title` is the wording of its tab, such as "Résultats". Without one, the tab reads as
        the function's name, with spaces for `_` and a capital first letter.
        """

        def register(f: View) -> View:
            self.views.append(ViewSpec(f, None if roles is None else tuple(roles), title))
            return f

        return register(fn) if fn is not None else register

    def find_action(self, fn: object) -> ActionSpec | None:
        return next((a for a in self.actions if a.fn is fn), None)

    def find_query(self, fn: object) -> QuerySpec | None:
        return next((q for q in self.queries if q.fn is fn), None)

    def find_invariant(self, fn: object) -> InvariantSpec | None:
        return next((i for i in self.invariants if i.fn is fn), None)

    def find_view(self, fn: object) -> ViewSpec | None:
        return next((v for v in self.views if v.fn is fn), None)


def _check_app(name: str, files: list[str], roles: list[str] | None) -> list[Problem]:
    where = f"App({name!r})"
    problems = []
    if not _NAME.match(name):
        problems.append(Problem(where, "the name must be lower case letters, digits, - or _"))
    elif len(name) > MAX_APP_NAME:
        full = full_name(name, max(BUILT_IN, key=len))
        message = (
            f"the name is {len(name)} characters long, and an app's name is at most "
            f"{MAX_APP_NAME}. Some MCP clients accept a tool's full name only up to "
            f"{MAX_FULL_NAME} characters, and {full}, a tool every Cliquet app has, is "
            f"{len(full)}. Shorten the app's name."
        )
        problems.append(Problem(where, message))
    if not files:
        problems.append(Problem(where, "declare at least one data file in files=[...]"))
    for f in files:
        problem = declared_problem(f)
        if problem is not None:
            problems.append(Problem(where, problem))
    if len(set(files)) != len(files):
        problems.append(Problem(where, "a file is declared twice in files=[...]"))
    if roles is not None and not roles:
        problems.append(Problem(where, "roles=[] declares no role. Leave roles out instead."))
    roles = roles or []
    for role in roles:
        if not _NAME.match(role):
            problems.append(
                Problem(where, f"role {role!r}: use lower case letters, digits, - or _")
            )
        elif role in RESERVED_ROLES:
            problems.append(
                Problem(where, f"role {role!r}: the name is taken by Cliquet. Choose another.")
            )
    if len(set(roles)) != len(roles):
        problems.append(Problem(where, "a role is declared twice in roles=[...]"))
    return problems


def _check_accent(name: str, accent: str | None) -> list[Problem]:
    where = f"App({name!r})"
    if accent is None:
        return []
    if not isinstance(accent, str) or not _ACCENT.match(accent):
        return [Problem(where, f"accent={accent!r}: write the accent as #rrggbb, such as #0f766e")]
    contrast = contrast_with_white(accent)
    if contrast < _MIN_CONTRAST:
        message = (
            f"accent={accent!r}: white text on {accent.lower()} is hard to read "
            f"({contrast:.1f}:1, at least {_MIN_CONTRAST}:1 is needed). Choose a darker shade, "
            "such as #0f766e, #1d4ed8, #7c3aed, #be123c or #b45309."
        )
        return [Problem(where, message)]
    return []


def _check_icon(name: str, icon: str | None) -> list[Problem]:
    problem = icon_problem(icon) if icon is not None else None
    return [Problem(f"App({name!r})", problem)] if problem is not None else []


def _check_timezone(name: str, timezone: str | None) -> list[Problem]:
    if timezone is None or (isinstance(timezone, str) and timezone in _zone_names()):
        return []
    message = (
        f"timezone={timezone!r} is not a time zone. Write its IANA name, such as "
        '"Europe/Paris" or "America/New_York", or leave it out for UTC.'
    )
    return [Problem(f"App({name!r})", message)]


def _check_language(name: str, language: str | None) -> list[Problem]:
    if language is None or language in LANGUAGES:
        return []
    known = " or ".join(f'"{one}"' for one in LANGUAGES)
    message = (
        f"language={language!r}: Cliquet's screens are written in {known} only. "
        "Choose one, or leave it out for English."
    )
    return [Problem(f"App({name!r})", message)]


@cache
def _zone_names() -> frozenset[str]:
    # UTC needs no tz database, which some machines, such as Windows, lack.
    return frozenset(available_timezones() | {"UTC"})


def contrast_with_white(color: str) -> float:
    """The WCAG contrast ratio of white text on a `#rrggbb` colour."""

    def channel(hex_pair: str) -> float:
        c = int(hex_pair, 16) / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(color[i : i + 2]) for i in (1, 3, 5))
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return 1.05 / (luminance + 0.05)
