"""Load an app module and build its manifest: the JSON the runner relies on.

Building the manifest is also where an app's definition is checked, with the tests beside
it, so every way to build an app checks the same. Every problem is collected, then raised
together in one `AppError`, each saying what to fix.
"""

import importlib.util
import inspect
import json
import keyword
import os
import re
import sys
import tomllib
import unicodedata
import weakref
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from pydantic import BaseModel, ConfigDict, TypeAdapter, create_model

from cliquet import __version__
from cliquet.app import App, ViewSpec
from cliquet.checks import (
    check_clock,
    check_environment,
    check_markup,
    check_randomness,
    check_result,
    check_tests,
)
from cliquet.context import ActionContext, InvariantContext, QueryContext
from cliquet.errors import AppError, Problem
from cliquet.files import holds_file, is_file_schema
from cliquet.screens import Screen
from cliquet.tools import DATA_VERSION, MAX_FULL_NAME, MAX_KEY, PREFIX, full_name, reserved

_ARGUMENTS = ConfigDict(validate_by_name=True, validate_by_alias=True, extra="forbid")
_SCRIPT_BLOCK = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)
# The SDK's distribution. Its import is `cliquet`.
SDK_DISTRIBUTION = "cliquet-sdk"

_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")


# A name every MCP client accepts for a tool, an argument or a field, in any app's language.
_PLAIN_NAME = re.compile(r"[a-z][a-z0-9_]*")
# How long a view's title may be: it is a tab's wording.
MAX_TITLE = 40


def title_problem(title: object, what: str = "the tab's wording") -> str | None:
    """Why `title` can't be a view's title, or None when it can. `what` says what it words,
    such as the tab's wording or the app's name as people read it."""
    if not isinstance(title, str):
        return f'title={title!r} isn\'t text. Write {what}, such as title="Sales".'
    title = _title(title)
    # A space or a character that draws nothing, such as a zero-width one, leaves an empty tab.
    if all(unicodedata.category(c)[0] in "CZ" for c in title):
        return f"title is empty. Write {what}, or leave title out."
    if any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in title):
        return f"title has a line break or a control character. Write {what} on one line."
    if len(title) > MAX_TITLE:
        return (
            f"title is {len(title)} characters long, and {what} is at most {MAX_TITLE}. Shorten it."
        )
    return None


def _title(title: str) -> str:
    """A title as the manifest keeps it: without the spaces around it, and each accented letter
    as one character, so its length is the one a person counts."""
    return unicodedata.normalize("NFC", title).strip()


def _app_title(app: App, problems: list[Problem]) -> str | None:
    """The app's name as people read it, or None when it chose none: its pages then read its
    name."""
    if app.title is None:
        return None
    problem = title_problem(app.title, "the app's name as people read it")
    if problem is not None:
        problems.append(Problem(f"App({app.name!r})", problem))
        return None
    return _title(app.title)


def _plain_name(name: str, nested: bool = False) -> str:
    """The closest name of lower-case ASCII letters, digits and `_`, or "" when there is none:
    `résultats` gives `resultats`, and `addItem` gives `add_item`."""
    letters = unicodedata.normalize("NFKD", name)
    plain = "".join(c for c in letters if not unicodedata.combining(c))
    if not plain.isascii():
        return ""
    plain = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", plain).lower()
    plain = re.sub(r"[^a-z0-9_]+", "_", plain).lstrip("_0123456789")
    if reserved(plain) and not nested:  # `_app_x` would become `app_x`, which is Cliquet's
        return ""
    return f"{plain}_" if keyword.iskeyword(plain) else plain


def _name_problem(
    name: str, what: str, keep: str = "", *, longest: int | None = None, nested: bool = False
) -> str | None:
    """Why some MCP clients would refuse `name`, the name of `what`, and what to name it
    instead, or None when every client accepts it: the one rule for every name an app sends.

    `keep` says how to keep the wording a name that isn't ASCII gave, with a title (`_keep`).
    A name is at most `longest` characters, when given. Outside a model (`nested`), a name
    that starts with `app_` is Cliquet's, and is refused for that with its own words, so it
    has no problem here.
    """
    if reserved(name) and not nested:
        return None
    long = longest is not None and len(name) > longest
    if _PLAIN_NAME.fullmatch(name):
        if not long:
            return None
        return (
            f"`{name}` is {len(name)} characters long, and some MCP clients accept a name only "
            f"up to {longest}. Shorten {what}."
        )
    plain = _plain_name(name, nested)
    to = f" to `{plain}`" if plain and (longest is None or len(plain) <= longest) else ""
    rule = "lower-case ASCII letters, digits and _, starting with a letter"
    if long:
        rule += f", {longest} characters at most"
    hint = f"; {keep}" if keep and not name.isascii() else ""
    return f"`{name}` isn't a name every MCP client accepts. Rename {what}{to}: use {rule}{hint}."


def _keep(wording: str, view: bool = False) -> str:
    """How to keep `wording`, the words a name gave, with a view's title or a field's."""
    title = json.dumps(wording, ensure_ascii=False)
    if view:
        return f"for the tab's wording, pass title={title} to its view decorator"
    return f"for its wording, write its type as Annotated[..., Field(title={title})]"


# The keywords of a JSON schema whose value is a schema or a list of schemas, and those whose
# value maps keys to schemas. Any other keyword's value is data, such as a `default`, or words.
_SUBSCHEMAS = frozenset(
    {"items", "prefixItems", "additionalItems", "unevaluatedItems", "contains"}
    | {"additionalProperties", "unevaluatedProperties", "propertyNames"}
    | {"allOf", "anyOf", "oneOf", "not", "if", "then", "else", "contentSchema"}
)
_SCHEMA_MAPS = frozenset({"$defs", "definitions", "patternProperties", "dependentSchemas"})


def _properties(schema: Any, twin: Any) -> Iterator[tuple[dict[str, Any], str, str]]:
    """Each property of a JSON schema, at any depth, as the schema of its object, its key, and
    its field's Python name, read from `twin`: the same schema made with Python names
    (`by_alias=False`). So a key that differs from that name is an alias.

    The keys of `$defs` aren't properties: pydantic makes them from the models' names, in
    ASCII, for `$ref` to point at. A `dict`'s keys aren't either: they are data.
    """
    if not isinstance(schema, dict):
        return
    if not isinstance(twin, dict):
        twin = schema  # no Python names to read: each key stands for its own field
    for word, value in schema.items():
        other = twin.get(word)
        if word == "properties" and isinstance(value, dict):
            same = isinstance(other, dict) and len(other) == len(value)
            for (key, sub), name in zip(value.items(), other if same else value, strict=True):
                yield schema, key, name
                yield from _properties(sub, other.get(name) if same else None)
        elif word in _SCHEMA_MAPS and isinstance(value, dict):
            for key, sub in value.items():
                yield from _properties(sub, other.get(key) if isinstance(other, dict) else None)
        elif word in _SUBSCHEMAS:
            subs = value if isinstance(value, list) else [value]
            twins = other if isinstance(other, list) else [other]
            for i, sub in enumerate(subs):
                yield from _properties(sub, twins[i] if i < len(twins) else None)


def _label(name: str) -> str:
    """A name as a page shows it: `stock_levels` reads "Stock levels"."""
    return name.replace("_", " ").strip().capitalize()


def _wording(holder: dict[str, Any], key: str) -> str:
    """The words a client may show for a property of `holder`: its title, or its key's label."""
    prop = holder.get("properties", {}).get(key)
    title = prop.get("title") if isinstance(prop, dict) else None
    return title if isinstance(title, str) else _label(key)


def _field_names(
    schema: dict[str, Any],
    twin: dict[str, Any],
    file: str,
    problems: list[Problem],
    arguments: bool = False,
    result: bool = False,
) -> None:
    """Refuse a field of a model in a tool's schema, at any depth, whose key some MCP clients
    refuse: its alias, when it has one, or its name. With `arguments`, the top's properties
    are a function's arguments, which `_argument_names` checks with their Python names. With
    `result`, the top's properties are the fields of a result, beside which Cliquet adds its
    own, such as `app_data_version`: one that starts with `app_` is refused too.

    The problem names the model and the field, wherever the model is used, so it is said
    once for the file."""
    for holder, key, name in _properties(schema, twin):
        if arguments and holder is schema:
            continue
        title = holder.get("title")
        model = f"the model `{title}`" if isinstance(title, str) else "a model"
        what = f"the field of {model}"
        if key != name:
            what = f"the alias of the field `{name}` of {model}"
        if result and holder is schema and reserved(key):
            problems.append(
                Problem(
                    file,
                    f"`{key}` starts with {PREFIX}, which is kept for what Cliquet adds to an "
                    f"answer, such as {DATA_VERSION}. Rename {what}.",
                )
            )
        keep = _keep(_wording(holder, key))
        problem = _name_problem(key, what, keep, longest=MAX_KEY, nested=True)
        if problem is not None:
            problems.append(Problem(file, problem))


@dataclass(frozen=True)
class LoadedApp:
    app: App
    module: ModuleType
    path: Path
    source: str


def read_script_metadata(source: str) -> dict[str, Any] | None:
    """Parse the PEP 723 `script` block of a source file, or return None if it has none."""
    blocks = [m for m in _SCRIPT_BLOCK.finditer(source) if m.group("type") == "script"]
    if len(blocks) > 1:
        raise ValueError("there is more than one `# /// script` block")
    if not blocks:
        return None
    lines = blocks[0].group("content").splitlines(keepends=True)
    return tomllib.loads("".join(line[2:] if line.startswith("# ") else line[1:] for line in lines))


def sdk_pin() -> str:
    """The dependency an app written for this SDK pins, such as `cliquet-sdk>=0.1.2,<0.2`."""
    major, minor = (int(part) for part in __version__.split(".")[:2])
    return f"{SDK_DISTRIBUTION}>={__version__},<{major}.{minor + 1}"


def sdk_source_problem(source: str, sdk: Path | None) -> str | None:
    """Why uv could take the SDK of this `app.py` from a package index, or None if it can't.

    The SDK isn't on an index, and a package there with its name isn't this SDK. So
    the block must depend on `cliquet-sdk`, give it a plain `path` source, and not depend on
    `cliquet`. Nor may it have another `[tool.uv]` setting, such as `no-sources`, or a direct
    reference: a server refuses them. `sdk` is the local SDK's folder, for the line the message
    asks to add.
    """
    try:
        meta = read_script_metadata(source)
    except (ValueError, tomllib.TOMLDecodeError) as e:
        return f"app.py's `# /// script` block can't be read: {e}"
    if meta is None:
        return (
            "app.py has no `# /// script` block, so uv can't find the SDK. Start the file with "
            f'one that depends on "{sdk_pin()}", with a local source for it.'
        )
    names = {_requirement_name(dep) for dep in meta.get("dependencies", [])}
    # Named after the import, but on PyPI it is somebody else's package.
    if "cliquet" in names:
        return (
            "app.py depends on `cliquet`, a package on PyPI that isn't this SDK. "
            f'Depend on "{sdk_pin()}" instead, with a local source for it.'
        )
    if SDK_DISTRIBUTION not in names:
        return (
            "app.py doesn't depend on the SDK. Add "
            f'"{sdk_pin()}" to the dependencies of its `# /// script` block.'
        )
    settings = _uv_settings(meta)
    sources = settings.get("sources", {})
    entries = [
        value for key, value in sources.items() if _requirement_name(key) == SDK_DISTRIBUTION
    ]
    # A path, and `editable` at most: a `marker` or an `extra` sends uv to an index where it fails.
    entry = entries[0] if len(entries) == 1 else None
    if isinstance(entry, dict) and "path" in entry and set(entry) <= {"path", "editable"}:
        return _elsewhere_problem(meta)
    folder = json.dumps(str(sdk)) if sdk is not None else '"/path/to/cliquet/packages/sdk"'
    # `cliquet` would fill in the folder itself, had it found one.
    unknown = (
        " Or set CLIQUET_SDK_PATH to the SDK's folder." if sdk is None and not settings else ""
    )
    return (
        f"app.py gives `{SDK_DISTRIBUTION}` no local folder, so uv would look for it on a "
        "package index, where the SDK isn't published. Add "
        f"`# {SDK_DISTRIBUTION} = {{ path = {folder}, editable = true }}` under "
        f"`# [tool.uv.sources]` in its `# /// script` block.{unknown}"
    )


def _elsewhere_problem(meta: dict[str, Any]) -> str | None:
    """Why the block would send uv elsewhere than PyPI and the SDK's folder, or None.

    A server's uv runs with its network and its files, so an app may not choose an index, a
    folder of wheels, a direct URL for a library, or to skip its sources. Its `sources` are
    replaced anyway.
    """
    settings = sorted(key for key in _uv_settings(meta) if key != "sources")
    if settings:
        return (
            f"app.py's [tool.uv] sets {', '.join(settings)}. Cliquet takes the SDK from its folder "
            "and every other library from PyPI: remove those settings."
        )
    for dependency in meta.get("dependencies", []):
        if "@" in str(dependency).split(";", 1)[0]:
            return (
                f"app.py depends on {dependency!r}, a direct reference. Cliquet takes every "
                "library from PyPI: depend on it by name and version."
            )
    return None


def lock_source_problem(lock: str) -> str | None:
    """Why uv could install the SDK from elsewhere than a folder by this `app.py.lock`, or None.

    uv installs what a lock says while its requirements match the block's, whatever source it
    gives a package. So a lock written while uv skipped the sources would take the SDK from an
    index.
    """
    try:
        packages = tomllib.loads(lock).get("package", [])
    except tomllib.TOMLDecodeError as e:
        return f"app.py.lock can't be read: {e}. Remove it, and uv writes it again."
    for package in packages if isinstance(packages, list) else [packages]:
        name = package.get("name") if isinstance(package, dict) else None
        if not isinstance(name, str) or _requirement_name(name) != SDK_DISTRIBUTION:
            continue
        source = package.get("source")
        if not isinstance(source, dict) or not {"editable", "directory"} & set(source):
            return (
                f"app.py.lock takes `{SDK_DISTRIBUTION}` from elsewhere than a local folder, "
                "such as a package index. Remove app.py.lock, and uv writes it again."
            )
    return None


# What makes uv take the SDK from an index whatever the block says: skipping its sources, or a
# configuration file that does. Any uv.toml or pyproject.toml uv finds counts, the app's folder's
# included, so uv reads none.
_SKIPPING_SOURCES = ("UV_NO_SOURCES", "UV_NO_SOURCES_PACKAGE", "UV_CONFIG_FILE")


def uv_environment() -> dict[str, str]:
    """This process's environment for uv, without what could make it take the SDK elsewhere."""
    kept = {key: value for key, value in os.environ.items() if key not in _SKIPPING_SOURCES}
    return kept | {"UV_NO_CONFIG": "1"}


def without_uv_settings(source: str) -> bool:
    """Whether app.py's `# /// script` block has no `[tool.uv]` settings, as the server keeps it.

    Its SDK has no source then. `cliquet` gives uv a copy that names the SDK's folder.
    """
    try:
        meta = read_script_metadata(source)
    except ValueError, tomllib.TOMLDecodeError:
        return False
    return meta is not None and not _uv_settings(meta)


def _uv_settings(meta: dict[str, Any]) -> dict[str, Any]:
    return meta.get("tool", {}).get("uv", {})


def _requirement_name(requirement: str) -> str:
    match = _REQUIREMENT_NAME.match(requirement)
    return re.sub(r"[-_.]+", "-", match.group(1)).lower() if match else ""


def load_app(path: str | Path, module_name: str = "app") -> LoadedApp:
    """Import an app module from its file, under `module_name`.

    Tests import the same file as `app`. Loading reuses that module when it is already
    imported, so the harness and the tests share the same functions.
    """
    path = Path(path).resolve()
    source = path.read_text()
    module = sys.modules.get(module_name)
    if module is not None:
        loaded_from = getattr(module, "__file__", None)
        if loaded_from is None or Path(loaded_from).resolve() != path:
            message = (
                f"the module name {module_name!r} is already used by {loaded_from}. "
                "Load this app under another name."
            )
            raise AppError([Problem(str(path), message)])
    else:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise AppError([Problem(str(path), "this file can't be imported as a module")])
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[module_name]
            raise
    apps = list({id(v): v for v in vars(module).values() if isinstance(v, App)}.values())
    if len(apps) != 1:
        found = "none" if not apps else ", ".join(a.name for a in apps)
        message = f'define exactly one App, such as `shop = App("shop", ...)` (found {found})'
        raise AppError([Problem(path.name, message)])
    return LoadedApp(apps[0], module, path, source)


def build_manifest(path: str | Path, module_name: str = "app") -> dict[str, Any]:
    """Load an app and return its manifest, or raise `AppError` listing every problem."""
    return manifest_of(load_app(path, module_name))


def manifest_of(loaded: LoadedApp) -> dict[str, Any]:
    app, file = loaded.app, loaded.path.name
    problems: list[Problem] = []
    script = _script(loaded.source, file, problems)
    description = inspect.getdoc(loaded.module) or ""
    if not description:
        message = (
            "add a docstring at the top of app.py, after the `# /// script` block: one "
            "sentence saying what the app keeps. Agents see it first."
        )
        problems.append(Problem(file, message))
    problems += check_clock(loaded.source, file)
    problems += check_markup(loaded.source, file)
    problems += check_randomness(loaded.source, file)
    problems += check_environment(loaded.source, file)
    problems += check_tests(loaded.path.parent, app.files)
    actions = [
        _action(app, spec.fn, spec.roles, spec.destructive, file, problems) for spec in app.actions
    ]
    queries = [_query(app, spec.fn, spec.roles, file, problems) for spec in app.queries]
    invariants = [_invariant(spec.fn, spec.description, file, problems) for spec in app.invariants]
    views = [_view(app, spec.fn, spec.roles, file, problems, spec.title) for spec in app.views]
    title = _app_title(app, problems)
    _check_names(app, file, problems)
    if problems:
        # A model several tools use has the same problem in each: it is said once.
        raise AppError(list(dict.fromkeys(problems)))
    return {
        "sdk_version": __version__,
        "name": app.name,
        "title": title,
        "description": description,
        "files": list(app.files),
        "roles": list(app.roles),
        "accent": app.accent,
        "icon": app.icon,
        "timezone": app.timezone,
        "language": app.language,
        "script": script,
        "actions": actions,
        "invariants": invariants,
        "queries": queries,
        "views": views,
    }


def arguments_model(fn: Callable[..., Any], context: type) -> type[BaseModel]:
    """The pydantic model of a function's arguments, after `ctx`. Raises `AppError`."""
    problems: list[Problem] = []
    where = _where(fn, "app.py")
    sig = _signature(fn, context, where, problems)
    model = _arguments(fn, sig, where, problems) if sig else None
    if problems or model is None:
        raise AppError(problems)
    return model


def _name(fn: Callable[..., Any]) -> str:
    return getattr(fn, "__name__", repr(fn))


def _where(fn: Callable[..., Any], file: str) -> str:
    code = getattr(fn, "__code__", None)
    line = f":{code.co_firstlineno}" if code else ""
    return f"{file}{line} {_name(fn)}"


def result_is_wrapped(output_schema: dict[str, Any] | None) -> bool:
    """MCP results are objects: a query returning anything else is served as {"result": ...}."""
    return output_schema is not None and output_schema.get("type") != "object"


_RETURN_ADAPTERS: weakref.WeakKeyDictionary[Callable[..., Any], TypeAdapter[Any]] = (
    weakref.WeakKeyDictionary()
)


def return_adapter(fn: Callable[..., Any]) -> TypeAdapter[Any]:
    """The adapter of what an action or a query returns, made once per function.

    The manifest's `output_schema` and the JSON callers get both come from it.
    """
    try:
        adapter = _RETURN_ADAPTERS.get(fn)
    except TypeError:  # not weak-referenceable or not hashable: build it each time
        return _make_return_adapter(fn)
    if adapter is None:
        adapter = _RETURN_ADAPTERS[fn] = _make_return_adapter(fn)
    return adapter


def _make_return_adapter(fn: Callable[..., Any]) -> TypeAdapter[Any]:
    annotation = inspect.signature(fn, eval_str=True).return_annotation
    if annotation is inspect.Signature.empty:
        raise TypeError(f"{_name(fn)} has no return annotation: annotate what it returns")
    return TypeAdapter(annotation)


def _script(source: str, file: str, problems: list[Problem]) -> dict[str, Any] | None:
    try:
        meta = read_script_metadata(source)
    except (ValueError, tomllib.TOMLDecodeError) as e:
        problems.append(Problem(file, f"the `# /// script` block can't be read: {e}"))
        return None
    if meta is None:
        message = (
            "start the file with a PEP 723 block that pins the SDK: "
            f'`# /// script`, `# dependencies = ["{SDK_DISTRIBUTION}>=0.1,<0.2"]`, `# ///`'
        )
        problems.append(Problem(file, message))
        return None
    names = [_requirement_name(dep) for dep in meta.get("dependencies", [])]
    if SDK_DISTRIBUTION not in names:
        message = (
            f'add the SDK to the block\'s dependencies, such as "{SDK_DISTRIBUTION}>=0.1,<0.2"'
        )
        problems.append(Problem(file, message))
    return meta


def _signature(
    fn: Callable[..., Any], context: type, where: str, problems: list[Problem]
) -> inspect.Signature | None:
    try:
        sig = inspect.signature(fn, eval_str=True)
    except Exception as e:
        problems.append(Problem(where, f"its signature can't be read: {e}"))
        return None
    params = list(sig.parameters.values())
    positional = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    if not params or params[0].kind not in positional:
        problems.append(Problem(where, f"take `ctx: {context.__name__}` as the first argument"))
        return None
    if params[0].annotation is not context:
        message = f"annotate the first argument as `{params[0].name}: {context.__name__}`"
        problems.append(Problem(where, message))
    return sig


def _arguments(
    fn: Callable[..., Any], sig: inspect.Signature, where: str, problems: list[Problem]
) -> type[BaseModel] | None:
    fields: dict[str, Any] = {}
    ok = True
    for p in list(sig.parameters.values())[1:]:
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            problems.append(Problem(where, f"`{p}`: name each argument instead"))
            ok = False
        elif p.kind is inspect.Parameter.POSITIONAL_ONLY:
            problems.append(Problem(where, f"`{p.name}` must not be positional-only"))
            ok = False
        elif p.annotation is p.empty:
            problems.append(Problem(where, f"annotate `{p.name}` with its type"))
            ok = False
        else:
            fields[p.name] = (p.annotation, ... if p.default is p.empty else p.default)
    if not ok:
        return None
    try:
        model = create_model(f"{_name(fn)}_arguments", __config__=_ARGUMENTS, **fields)
        model.model_json_schema()
        model.model_json_schema(by_alias=False)  # the check of names reads it
    except Exception as e:
        problems.append(Problem(where, f"its arguments can't be described as JSON: {e}"))
        return None
    return model


def _description(fn: Callable[..., Any], where: str, problems: list[Problem]) -> str:
    doc = inspect.getdoc(fn)
    if not doc:
        problems.append(Problem(where, "add a docstring: it is the tool's description for agents"))
        return ""
    return doc


def _effective_roles(app: App, roles: tuple[str, ...] | None) -> list[str] | None:
    """The roles that may use a function. None means anyone: the app declares no roles."""
    if not app.roles:
        return None
    if roles is None:
        return list(app.roles) if len(app.roles) == 1 else []
    return [r for r in app.roles if r in roles]


def _roles(
    app: App, roles: tuple[str, ...] | None, where: str, problems: list[Problem]
) -> list[str] | None:
    if not app.roles:
        if roles is not None:
            message = "the app declares no roles. Declare them in App(roles=[...]) or drop roles="
            problems.append(Problem(where, message))
        return None
    if roles is None:
        if len(app.roles) > 1:
            message = (
                f"the app has several roles ({', '.join(app.roles)}). "
                "Say which may use it with roles=[...]"
            )
            problems.append(Problem(where, message))
        return _effective_roles(app, roles)
    if not roles:
        problems.append(Problem(where, "roles=[] lets nobody use it. Name at least one role."))
    for role in roles:
        if role not in app.roles:
            message = f"role {role!r} is not declared in App (roles: {', '.join(app.roles)})"
            problems.append(Problem(where, message))
    return _effective_roles(app, roles)


def _action(
    app: App,
    fn: Callable[..., Any],
    roles: tuple[str, ...] | None,
    destructive: bool,
    file: str,
    problems: list[Problem],
) -> dict[str, Any]:
    where = _where(fn, file)
    sig = _signature(fn, ActionContext, where, problems)
    schema = _input_schema(fn, sig, where, file, problems)
    file_argument = _file_argument(schema, where, problems)
    output = None
    if sig:
        result = sig.return_annotation
        if result is sig.empty:
            message = "annotate what it returns: `-> None`, or a model such as `-> Imported`"
            problems.append(Problem(where, message))
        elif result is not None:
            output = _output_schema(fn, where, file, problems)
            problems += check_result(result, file)
    return {
        "name": _name(fn),
        "description": _description(fn, where, problems),
        "roles": _roles(app, roles, where, problems),
        "destructive": destructive,
        "input_schema": schema,
        "output_schema": output,
        # The argument that is a file the person uploads, by its name in the schema, or None.
        "file_argument": file_argument,
    }


# What a file argument's description ends with, for the agent: the reference to pass.
FILE_TOLD = (
    "A file the person uploads: call app_ask_for_file for a reference to it, and pass that "
    "here, so you never copy the file's contents."
)


def _file_argument(
    schema: dict[str, Any] | None, where: str, problems: list[Problem]
) -> str | None:
    """The name of the action's argument that is a file, by its schema, or None. Its
    description says how to pass it. A file anywhere else in the arguments, or a second one,
    is refused."""
    properties = (schema or {}).get("properties", {})
    files = [name for name, one in properties.items() if is_file_schema(one)]
    others = {name: one for name, one in properties.items() if name not in files}
    if holds_file(others) or holds_file((schema or {}).get("$defs", {})):
        problems.append(Problem(where, _NESTED_FILE))
    if len(files) > 1:
        message = (
            f"it takes {len(files)} files ({', '.join(files)}), and an action takes one at most: "
            "split it into several actions, or let the person upload one file that holds them all"
        )
        problems.append(Problem(where, message))
    if len(files) != 1:
        return None
    told = properties[files[0]]
    told["description"] = " ".join(filter(None, [told.get("description", ""), FILE_TOLD]))
    return files[0]


# Why a file may be no part of a value, nor what a query or a view takes, nor any result.
_NESTED_FILE = (
    "a File is an action's own argument, such as `statement: File`, never part of a model, a "
    "list or a dict, nor what a query or a view takes, nor a result: the person uploads one file "
    "for one call"
)


def _no_file(schema: dict[str, Any] | None, where: str, problems: list[Problem]) -> None:
    """Refuse a file in what a query or a view takes, or in what a function returns."""
    if holds_file(schema):
        problems.append(Problem(where, _NESTED_FILE))


def _input_schema(
    fn: Callable[..., Any],
    sig: inspect.Signature | None,
    where: str,
    file: str,
    problems: list[Problem],
) -> dict[str, Any] | None:
    """The JSON schema of what a function takes, by alias, once its arguments' names and its
    models' fields are checked, or None with a problem."""
    model = _arguments(fn, sig, where, problems) if sig else None
    if model is None:
        _argument_names(sig, None, where, problems)
        return None
    schema = model.model_json_schema()
    _argument_names(sig, schema, where, problems)
    twin = model.model_json_schema(by_alias=False)
    _field_names(schema, twin, file, problems, arguments=True)
    return schema


def _argument_names(
    sig: inspect.Signature | None,
    schema: dict[str, Any] | None,
    where: str,
    problems: list[Problem],
) -> None:
    """Refuse an argument named, in Python or by its alias in `schema`, as Cliquet names the
    arguments it adds, such as `app_transaction`, or by a name some MCP clients refuse. Its
    Python name counts too, since an agent may send it."""
    if sig is None:
        return
    properties = (schema or {}).get("properties", {})
    used = set(list(sig.parameters)[1:]) | set(properties)
    for name in sorted(filter(reserved, used)):
        message = (
            f"`{name}` starts with {PREFIX}, which is kept for the arguments every Cliquet app's "
            "tools take, such as app_request_id and app_transaction. Rename the argument."
        )
        problems.append(Problem(where, message))
    for name in sorted(used):
        named = "the argument" if name in sig.parameters else "the argument's alias"
        # An aliased argument's wording comes from its alias, which renaming it keeps.
        keep = _keep(_wording(schema or {}, name)) if name in properties else ""
        problem = _name_problem(name, named, keep, longest=MAX_KEY)
        if problem is not None:
            problems.append(Problem(where, problem))


def _query(
    app: App,
    fn: Callable[..., Any],
    roles: tuple[str, ...] | None,
    file: str,
    problems: list[Problem],
) -> dict[str, Any]:
    where = _where(fn, file)
    sig = _signature(fn, QueryContext, where, problems)
    schema = _input_schema(fn, sig, where, file, problems)
    _no_file(schema, where, problems)
    output = None
    if sig:
        result = sig.return_annotation
        if result is sig.empty or result is None:
            message = "annotate what it returns, such as `-> list[MonthTotal]`"
            problems.append(Problem(where, message))
        else:
            output = _output_schema(fn, where, file, problems)
            problems += check_result(result, file)
    return {
        "name": _name(fn),
        "description": _description(fn, where, problems),
        "roles": _roles(app, roles, where, problems),
        "input_schema": schema,
        "output_schema": output,
    }


def _output_schema(
    fn: Callable[..., Any], where: str, file: str, problems: list[Problem]
) -> dict[str, Any] | None:
    """The JSON schema of what an action or a query returns, by alias, once its models' fields
    are checked, or None with a problem. A field of the result itself may not start with
    `app_`: Cliquet adds its own beside it, such as `app_data_version`."""
    try:
        adapter = return_adapter(fn)
        schema = adapter.json_schema(mode="serialization")
        twin = adapter.json_schema(mode="serialization", by_alias=False)
    except Exception as e:
        problems.append(Problem(where, f"its result can't be described as JSON: {e}"))
        return None
    _field_names(schema, twin, file, problems, result=True)
    _no_file(schema, where, problems)
    return schema


def _invariant(
    fn: Callable[..., Any], description: str, file: str, problems: list[Problem]
) -> dict[str, Any]:
    where = _where(fn, file)
    sig = _signature(fn, InvariantContext, where, problems)
    if sig:
        if len(sig.parameters) != 1:
            problems.append(Problem(where, "an invariant takes only `ctx: InvariantContext`"))
        if sig.return_annotation != list[str]:
            message = "annotate `-> list[str]`: the problems found, empty when the data is valid"
            problems.append(Problem(where, message))
    if not description.strip():
        problems.append(Problem(where, 'describe the rule in one line: @app.invariant("...")'))
    return {"name": _name(fn), "description": description}


def _view(
    app: App,
    fn: Callable[..., Any],
    roles: tuple[str, ...] | None,
    file: str,
    problems: list[Problem],
    title: str | None,
) -> dict[str, Any]:
    """A view's entry. Its layout isn't here: it depends on the data and its arguments."""
    where = _where(fn, file)
    sig = _signature(fn, QueryContext, where, problems)
    schema = _input_schema(fn, sig, where, file, problems)
    _no_file(schema, where, problems)
    if sig:
        if sig.return_annotation is not Screen:
            message = "annotate `-> Screen`: the screen it lays out, from cliquet"
            problems.append(Problem(where, message))
        for p in list(sig.parameters.values())[1:]:
            if p.default is p.empty:
                message = (
                    f"give `{p.name}` a default: a view opens from the app's tabs with no "
                    "arguments, such as `month: Month | None = None`"
                )
                problems.append(Problem(where, message))
    if title is not None:
        problem = title_problem(title)
        if problem is not None:
            problems.append(Problem(where, problem))
    return {
        "name": _name(fn),
        # The wording of its tab, or None: the tab then reads as the name.
        "title": _title(title) if isinstance(title, str) else None,
        "description": _description(fn, where, problems),
        "roles": _roles(app, roles, where, problems),
        "input_schema": schema,
    }


def _check_names(app: App, file: str, problems: list[Problem]) -> None:
    tools = [_name(s.fn) for s in [*app.actions, *app.queries, *app.views]]
    # What is left of a full name for the tool's own, after the app's.
    room = MAX_FULL_NAME - len(full_name(app.name, ""))
    for spec in [*app.actions, *app.queries, *app.views]:
        name, where = _name(spec.fn), _where(spec.fn, file)
        keep = ""
        if isinstance(spec, ViewSpec):
            # The tab's wording until now: the name, as a page shows it.
            keep = _keep(_label(name), view=True)
        problem = _name_problem(name, "the function", keep)
        if problem is not None:
            problems.append(Problem(where, problem))
        if len(name) > room:
            full = full_name(app.name, name)
            # The app's name already leaves room for Cliquet's own tools: shorten the longer one.
            if len(app.name) > len(name):
                shorten = (
                    f"Shorten the app's name, the longer of the two, to at most "
                    f"{len(app.name) - (len(full) - MAX_FULL_NAME)} characters, or the function's"
                )
            else:
                shorten = "Shorten the function's name"
            message = (
                f"the name is too long. Some MCP clients accept a tool's full name only up to "
                f"{MAX_FULL_NAME} characters, and {full} is {len(full)}. {shorten} to at most "
                f"{room} characters."
            )
            problems.append(Problem(where, message))
    for name in sorted({n for n in tools if tools.count(n) > 1}):
        message = f"two actions, queries or views are named {name!r}. Rename one."
        problems.append(Problem(file, message))
    for name in sorted(filter(reserved, set(tools))):
        message = (
            f"{name!r} starts with {PREFIX}, which is kept for the tools every Cliquet app has, "
            "such as app_history. Rename the function."
        )
        problems.append(Problem(file, message))
    descriptions = [i.description for i in app.invariants]
    for d in sorted({d for d in descriptions if descriptions.count(d) > 1}):
        message = f"two invariants are described as {d!r}. Make each description unique."
        problems.append(Problem(file, message))
