"""Checks on an app's code: its source, its tests and the models its results use."""

import ast
import functools
import inspect
import textwrap
import typing
from collections.abc import Iterable, Iterator
from dataclasses import is_dataclass
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, BaseModel, PlainSerializer, WrapSerializer

from cliquet.errors import Problem
from cliquet.files import is_pattern, matches, versioned


@functools.lru_cache(maxsize=4)
def _parsed(source: str, filename: str) -> ast.Module:
    """The module's tree, parsed once for every check of the same source. No check changes it."""
    return ast.parse(source, filename)


# Calls that read the system clock, as (module, attribute, ...) paths.
_CLOCK_CALLS = {
    ("datetime", "datetime", "now"),
    ("datetime", "datetime", "today"),
    ("datetime", "datetime", "utcnow"),
    ("datetime", "date", "today"),
    ("time", "time"),
    ("time", "time_ns"),
}
# These read the clock only when called with no argument.
_CLOCK_WITHOUT_ARGS = {
    ("time", "localtime"),
    ("time", "gmtime"),
    ("time", "ctime"),
}


def check_clock(source: str, filename: str = "app.py") -> list[Problem]:
    """Find calls that read the system clock anywhere in an app module.

    The whole module is checked, helpers included, since a query may call a helper.
    Only direct imports and their aliases are followed.
    """
    tree = _parsed(source, filename)
    bound = _bound(tree, ("datetime", "time"))
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        path = _resolve(node.func, bound)
        if path is None:
            continue
        reads_clock = path in _CLOCK_CALLS or (
            path in _CLOCK_WITHOUT_ARGS and not node.args and not node.keywords
        )
        if reads_clock:
            call = ast.unparse(node)
            problems.append(
                Problem(
                    f"{filename}:{node.lineno}",
                    f"{call} reads the system clock. Use ctx.now, set by the runtime "
                    "for each call, so tests can fix the time.",
                )
            )
    return problems


def _bound(tree: ast.Module, modules: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    """The names the module binds to `modules` or to what it imports from them, as paths."""
    bound: dict[str, tuple[str, ...]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                path = tuple(alias.name.split("."))
                if path[0] not in modules:
                    continue
                if alias.asname is None:
                    bound[path[0]] = path[:1]  # `import os.path` binds `os`
                else:
                    bound[alias.asname] = path
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            module = tuple(node.module.split("."))
            if module[0] in modules:
                for alias in node.names:
                    bound[alias.asname or alias.name] = (*module, alias.name)
    return bound


def _resolve(func: ast.expr, bound: dict[str, tuple[str, ...]]) -> tuple[str, ...] | None:
    attrs: list[str] = []
    while isinstance(func, ast.Attribute):
        attrs.append(func.attr)
        func = func.value
    if not isinstance(func, ast.Name) or func.id not in bound:
        return None
    return bound[func.id] + tuple(reversed(attrs))


# What would let a value reach a screen as HTML: Jinja's and MarkupSafe's own types, and an
# object that says it is HTML.
_MARKUP_MODULES = ("jinja2", "markupsafe")


def check_markup(source: str, filename: str = "app.py") -> list[Problem]:
    """Find what could make a screen show a value as HTML: an import of Jinja or MarkupSafe,
    or an `__html__` anywhere in the module. Cliquet renders every template itself, and
    escapes every value, so that data never becomes markup."""
    tree = _parsed(source, filename)
    problems = [
        Problem(
            f"{filename}:{node.lineno}",
            f"import {name} isn't allowed: Cliquet renders a view's template itself, and writes "
            "every value as text. Return Screen(...) with plain values.",
        )
        for node, name in _imported(tree, _MARKUP_MODULES)
    ]
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr == "__html__") or (
            isinstance(node, ast.FunctionDef) and node.name == "__html__"
        ):
            problems.append(
                Problem(
                    f"{filename}:{node.lineno}",
                    "__html__ isn't allowed: it would let a value show as HTML on a screen. "
                    "Pass plain text; the template holds the HTML.",
                )
            )
    return problems


def _imported(tree: ast.Module, modules: tuple[str, ...]) -> Iterator[tuple[ast.stmt, str]]:
    """Each import of one of `modules`, or of a module inside one, with the module's name."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        else:
            continue
        for name in names:
            if name.split(".")[0] in modules:
                yield node, name


def _referenced(
    tree: ast.Module, bound: dict[str, tuple[str, ...]]
) -> Iterator[tuple[ast.expr, tuple[str, ...]]]:
    """Each name or attribute that resolves through `bound`, with its path."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Name | ast.Attribute):
            path = _resolve(node, bound)
            if path is not None:
                yield node, path


_SAME_RESULT = (
    "the same data and arguments must give the same result, so a write is safe to run again"
)
_RANDOM_MODULES = ("random", "secrets")
_RANDOM_CALLS = {
    ("uuid", "uuid1"),
    ("uuid", "uuid4"),
    ("uuid", "uuid6"),
    ("uuid", "uuid7"),
    ("os", "urandom"),
    ("os", "getrandom"),
}


def check_randomness(source: str, filename: str = "app.py") -> list[Problem]:
    """Find what draws random values anywhere in an app module, helpers included:
    `random`, `secrets`, `uuid.uuid1`, `uuid4`, `uuid6` and `uuid7`, and `os.urandom` and
    `os.getrandom`."""
    tree = _parsed(source, filename)
    problems = [
        Problem(
            f"{filename}:{node.lineno}",
            f"import {name} isn't allowed: {_SAME_RESULT}. Take the value as an argument, "
            "or derive it from the data, such as the number after the highest one.",
        )
        for node, name in _imported(tree, _RANDOM_MODULES)
    ]
    problems += [
        Problem(
            f"{filename}:{node.lineno}",
            f"{'.'.join(path)} is random, but {_SAME_RESULT}. Derive the value from the data, "
            "such as the number after the highest one, or take it as an argument.",
        )
        for node, path in _referenced(tree, _bound(tree, ("uuid", "os")))
        if path in _RANDOM_CALLS
    ]
    return problems


_PROGRAM = (
    "an app can't declare a program, so it would break on another machine. Use a Python "
    "library listed in the `# /// script` block, or tell the person it isn't possible."
)
# The other ways to run a program, besides the `subprocess` module and `os.exec*` and
# `os.spawn*`.
_PROGRAMS = {
    ("os", "system"),
    ("os", "popen"),
    ("os", "startfile"),
    ("os", "posix_spawn"),
    ("os", "posix_spawnp"),
    ("asyncio", "create_subprocess_exec"),
    ("asyncio", "create_subprocess_shell"),
    ("asyncio", "subprocess", "create_subprocess_exec"),
    ("asyncio", "subprocess", "create_subprocess_shell"),
    ("pty", "spawn"),
}
_OS_PROGRAM_PREFIXES = ("exec", "spawn")
# The methods that change sys.path in place.
_SYS_PATH_CHANGES = {
    ("sys", "path", method)
    for method in ("append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse")
} | {("site", "addsitedir")}


def check_environment(source: str, filename: str = "app.py") -> list[Problem]:
    """Find what depends on the machine rather than on the app's block, anywhere in an app
    module: running another program, and changing `sys.path`."""
    tree = _parsed(source, filename)
    problems = [
        Problem(f"{filename}:{node.lineno}", f"import {name} isn't allowed: {_PROGRAM}")
        for node, name in _imported(tree, ("subprocess",))
    ]
    problems += [
        Problem(f"{filename}:{node.lineno}", f"{'.'.join(path)} runs a program, but {_PROGRAM}")
        for node, path in _referenced(tree, _bound(tree, ("os", "asyncio", "pty")))
        if path in _PROGRAMS
        or (len(path) == 2 and path[0] == "os" and path[1].startswith(_OS_PROGRAM_PREFIXES))
    ]
    problems += [
        Problem(
            f"{filename}:{node.lineno}",
            f"{ast.unparse(node)} changes sys.path, so the app would find other modules on "
            "another machine. List the library in the `# /// script` block, and keep the "
            "app's own code in app.py.",
        )
        for node in _sys_path_changes(tree)
    ]
    return problems


def _sys_path_changes(tree: ast.Module) -> Iterator[ast.stmt | ast.expr]:
    bound = _bound(tree, ("sys", "site"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _resolve(node.func, bound) in _SYS_PATH_CHANGES:
                yield node
        elif isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign | ast.Delete):
            targets = node.targets if isinstance(node, ast.Assign | ast.Delete) else [node.target]
            for target in targets:
                if isinstance(target, ast.Subscript):
                    target = target.value
                if _resolve(target, bound) == ("sys", "path"):
                    yield node


# What builds a path from its parts: a string given to one of these names a path.
_PATH_CALLS = {"Path", "PurePath", "PosixPath", "open", "join", "joinpath"}


def check_tests(folder: Path, data: Iterable[str] = ()) -> list[Problem]:
    """Find the app's tests, its code under `tests/`, that name a path in `data/`. A name
    that `data`, the app's own data files, declares is one a test may give `given`: one of its
    names, or a name its pattern matches when the pattern starts with `data/` itself, since
    `**/*.csv` matches `data/stock.csv` too.

    Each test is parsed again only once it changed: a test run loads the app at each `given`,
    and each load checks the app."""
    problems = []
    for path in sorted((folder / "tests").glob("*.py")):
        if not versioned(f"tests/{path.name}"):
            continue
        try:
            text = path.read_text()
        except OSError, UnicodeDecodeError:
            continue  # pytest reports it
        problems += _test_problems(path.name, text, frozenset(data))
    return problems


@functools.lru_cache(maxsize=64)
def _test_problems(name: str, text: str, data: frozenset[str]) -> tuple[Problem, ...]:
    """The problems of one test file, by its text."""
    try:
        tree = ast.parse(text, name)
    except SyntaxError:
        return ()  # pytest reports it
    return tuple(
        Problem(
            f"tests/{name}:{node.lineno}",
            f"{node.value!r} names the app's data/ folder, which holds its first data "
            "and is gone from the draft after the first publish. State the test's own "
            "data instead: given(app, {file: text}).",
        )
        for node in _data_paths(tree)
        if not any(
            matches(entry, str(node.value)) and (not is_pattern(entry) or entry.startswith("data/"))
            for entry in data
        )
    )


def _data_paths(tree: ast.Module) -> Iterator[ast.Constant]:
    """The strings that name `data/` or a path in it, docstrings left out."""
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        text = node.value.replace("\\", "/")
        parts = [part for part in text.split("/") if part]
        while parts and parts[0] in (".", ".."):
            parts.pop(0)
        if not parts or parts[0] != "data":
            continue
        # A bare "data" is a path only where a path is built.
        if len(parts) > 1 or text.endswith("/") or _builds_path(parents.get(node)):
            yield node


def _builds_path(parent: ast.AST | None) -> bool:
    if isinstance(parent, ast.BinOp):
        return isinstance(parent.op, ast.Div)
    if isinstance(parent, ast.Call):
        func = parent.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        return name in _PATH_CALLS
    return False


_READ_BACK = "callers get the result as JSON, and a test's query rebuilds it from that JSON"
_ORDINARY = "Store each value as the caller should read it, in an ordinary field."
_SET_WHEN_BUILT = "Make it an ordinary field, set when the result is built."


def check_result(annotation: Any, file: str = "app.py") -> list[Problem]:
    """Find what keeps a result from reading back from its JSON, in every model it holds, at
    any depth: a serializer, in a model or in an annotation, a computed field, an excluded
    field, or a serialization alias that isn't also read."""
    problems = []
    for model in _models(annotation):
        where = _where(model, file)
        decorators = model.__pydantic_decorators__
        for kind, found, instead in (
            ("field_serializer", decorators.field_serializers, _ORDINARY),
            ("model_serializer", decorators.model_serializers, _ORDINARY),
            ("computed_field", decorators.computed_fields, _SET_WHEN_BUILT),
        ):
            problems += [
                Problem(
                    _where(decorator.func, where),
                    f"{model.__name__}.{name} is a {kind}, but {_READ_BACK}. {instead}",
                )
                for name, decorator in found.items()
            ]
        for name, field in model.model_fields.items():
            label = f"{model.__name__}.{name}"
            problems += [
                Problem(
                    _field_where(model, name, where),
                    f"{label} has a {type(serializer).__name__}, but {_READ_BACK}. {_ORDINARY}",
                )
                for serializer in _serializers(field.annotation, field.metadata)
            ]
            if field.exclude or field.exclude_if is not None:
                problems.append(
                    Problem(
                        _field_where(model, name, where),
                        f"{label} is excluded from the result's JSON, but {_READ_BACK}. "
                        "Return a model without that field instead.",
                    )
                )
            alias = field.serialization_alias
            if alias is not None and alias not in _read_names(model, name):
                problems.append(
                    Problem(
                        _field_where(model, name, where),
                        f"{label} has serialization_alias={alias!r}, which isn't "
                        f"read back, but {_READ_BACK}. Use Field(alias={alias!r}), which "
                        "names it both ways.",
                    )
                )
    problems += [
        Problem(
            file,
            f"The result's type has a {type(serializer).__name__}, but {_READ_BACK}. {_ORDINARY}",
        )
        for serializer in _serializers(annotation)
    ]
    return problems


def _serializers(annotation: Any, metadata: Iterable[Any] = ()) -> Iterator[Any]:
    """The serializers in a type's `Annotated` parts, but those of the models it holds, which
    are checked field by field: a `PlainSerializer` or a `WrapSerializer` writes JSON that
    the type doesn't read back."""
    for kind in _types(annotation, into_models=False):
        if typing.get_origin(kind) is typing.Annotated:
            metadata = [*metadata, *kind.__metadata__]
    return (one for one in metadata if isinstance(one, PlainSerializer | WrapSerializer))


def _models(annotation: Any) -> Iterator[type[BaseModel]]:
    """The models a type holds, at any depth, each once."""
    return (kind for kind in _types(annotation, into_models=True) if _is_model(kind))


def _types(annotation: Any, *, into_models: bool) -> Iterator[Any]:
    """Every type a type holds, at any depth, each once, itself included: in the models it
    holds too when `into_models`. A type whose annotations can't be read is left out:
    describing the result as JSON reports it."""
    # Each type seen is kept as well as its id: a hint `get_type_hints` made could be freed,
    # and its id given to another.
    seen: dict[int, Any] = {}
    todo = [annotation]
    while todo:
        kind = todo.pop()
        if id(kind) in seen:
            continue
        seen[id(kind)] = kind
        yield kind
        alias = typing.get_origin(kind) or kind
        if isinstance(alias, typing.TypeAliasType):
            # A `type X = ...` alias: what it names, and its arguments when subscripted.
            todo += [alias.__value__, *typing.get_args(kind)]
        elif _is_model(kind):
            if into_models:
                todo += [field.annotation for field in kind.model_fields.values()]
        elif typing.is_typeddict(kind) or (isinstance(kind, type) and is_dataclass(kind)):
            try:
                todo += list(typing.get_type_hints(kind, include_extras=True).values())
            except Exception:
                continue
        else:
            todo += typing.get_args(kind)


def _is_model(kind: Any) -> bool:
    return isinstance(kind, type) and issubclass(kind, BaseModel)


def _read_names(model: type[BaseModel], name: str) -> set[str]:
    """The names a field is read by, when a result is rebuilt from its JSON."""
    field = model.model_fields[name]
    read = field.validation_alias
    names: set[str] = set()
    config = model.model_config
    if config.get("validate_by_alias") is False:
        pass  # read by name only
    elif read is None:
        names.add(field.alias or name)
    elif isinstance(read, str):
        names.add(read)
    elif isinstance(read, AliasChoices):
        names |= {choice for choice in read.choices if isinstance(choice, str)}
    if config.get("validate_by_name") or config.get("populate_by_name"):
        names.add(name)
    return names


def _where(thing: Any, fallback: str) -> str:
    """Where a class or a function is defined, as `file:line`, or `fallback`."""
    try:
        file = inspect.getsourcefile(thing)
        _, line = inspect.getsourcelines(thing)
    except OSError, TypeError:
        return fallback
    return f"{Path(file).name}:{line}" if file else fallback


def _field_where(model: type[BaseModel], name: str, fallback: str) -> str:
    """Where a model's field is declared, as `file:line`, or `fallback`."""
    owner = next((c for c in model.__mro__ if name in inspect.get_annotations(c)), model)
    try:
        file = inspect.getsourcefile(owner)
        lines, start = inspect.getsourcelines(owner)
        tree = ast.parse(textwrap.dedent("".join(lines)))
    except OSError, TypeError, SyntaxError:
        return fallback
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            return f"{Path(file).name}:{start + node.lineno - 1}" if file else fallback
    return _where(owner, fallback)
