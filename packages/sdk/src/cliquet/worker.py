"""The worker: runs one app's code on the files it is given.

It is the SDK's engine. `Runtime` keeps the app's history in git and hands the code to
it; the server's engine does the same work in a sandbox. A worker never checks roles.
"""

import annotationlib
import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import chdir, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any, Protocol, TextIO
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo

from cliquet.context import ActionContext, Caller, InvariantContext, QueryContext
from cliquet.errors import AppError, AppFailed, Denied, Problem, Refused, described, shortened
from cliquet.files import INPUTS, MIGRATION, declares, input_name
from cliquet.inputs import Input
from cliquet.limits import MAX_RESULT, size_text
from cliquet.logs import log_to_stderr
from cliquet.manifest import arguments_model, load_app, manifest_of, return_adapter
from cliquet.screens import finish, lay_out
from cliquet.store import write_files
from cliquet.tools import CHECK, PUBLISH, listed, wrong_arguments

# App functions run with their folder as the current directory, which is shared by the
# whole process. One lock keeps two calls from changing it at the same time.
_APP_CODE = threading.RLock()


class InvalidArguments(ValueError):
    """The call can't be made as it was asked, such as with arguments that don't match the
    action's or the query's schema."""


class WrongArguments(InvalidArguments):
    """The arguments don't fit the tool's schema: one it doesn't take, one missing, or a value
    of the wrong type."""


@dataclass(frozen=True)
class Refusal:
    """The change was refused, by the action itself (`message`) or by invariants."""

    message: str | None = None
    invariants: dict[str, list[str]] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:
        lines = [self.message] if self.message else []
        for name, problems in self.invariants.items():
            lines.append(f"Refused by: {self.descriptions.get(name, name)}")
            lines += [f"  - {p}" for p in problems]
        return "\n".join(lines)


@dataclass(frozen=True)
class Committed:
    """The change was committed. `commit` is None when it changed nothing.

    `result` is what the action returned, as JSON data: None for `-> None`. It isn't
    stored, so a call `replayed` by its request id has no result. A committed transaction
    lists in `commits` every commit it kept, oldest first, one per call, and `commit` is
    the last; for anything else `commits` is empty.
    """

    commit: str | None
    files: list[str]
    result: Any = None
    replayed: bool = False
    commits: list[str] = field(default_factory=list)


type Outcome = Committed | Refusal


@dataclass(frozen=True)
class Changes:
    """The files a change touched: each file's new content, or None if it was removed.

    `result` is what the action returned, as JSON data: None for `-> None`, and for any
    change that isn't an action's.
    """

    files: dict[str, bytes | None]
    result: Any = None


@dataclass(frozen=True)
class Checked:
    """What a check of the data found: the invariants' refusal, and the views' problems."""

    refusal: Refusal | None
    views: list[Problem]


class Engine(Protocol):
    """What runs an app's code. `Runtime` works on any engine.

    Every engine reports a failure of the app's code as `AppFailed`; `Refused` and `Denied`
    keep the app's own message. An invariant that raises, or returns something other than a
    list of messages, is such a failure: nothing is committed.
    """

    manifest: dict[str, Any]

    def run_action(
        self,
        caller: Caller,
        now: datetime,
        action: str,
        arguments: dict[str, Any],
        files: dict[str, bytes],
        inputs: Mapping[str, Input] | None = None,
    ) -> Changes | Refusal:
        """Run an action on a working copy of `files`, then every invariant. `inputs` holds
        the file of its file argument, if it takes one and the call gave it."""
        ...

    def apply(
        self,
        caller: Caller,
        label: str,
        files: dict[str, bytes],
        result: dict[str, bytes],
    ) -> Changes | Refusal:
        """Check a change made elsewhere: `files` before, `result` after."""
        ...

    def query_json(
        self,
        caller: Caller,
        now: datetime,
        query: str,
        arguments: dict[str, Any],
        files: dict[str, bytes],
        state: str | None,
    ) -> Any:
        """Run a query read-only, and return its result as JSON data."""
        ...

    def view(
        self,
        caller: Caller,
        now: datetime,
        view: str,
        arguments: dict[str, Any],
        files: dict[str, bytes],
        state: str | None,
    ) -> dict[str, Any]:
        """Lay out a view read-only, and return `{"arguments", "body"}`: the arguments that draw
        it again, and its tree, which the runtime checks."""
        ...

    def check(self, now: datetime, files: dict[str, bytes]) -> Checked:
        """Run every invariant on `files`, and lay out every view for each of its roles."""
        ...


class Worker:
    """The engine that runs an app's code in this process."""

    def __init__(self, app_py: str | Path, module_name: str = "app") -> None:
        loaded = load_app(app_py, module_name)
        self.manifest = manifest_of(loaded)
        self.app_file = loaded.path
        app = loaded.app
        # Every caller's ctx.now is in the app's own zone, whatever the clock's.
        self._zone: tzinfo = UTC if app.timezone == "UTC" else ZoneInfo(app.timezone)
        self._files = tuple(self.manifest["files"])
        self._views = {m["name"]: m for m in self.manifest["views"]}
        actions = self.manifest["actions"]
        self._no_result = {m["name"] for m in actions if m["output_schema"] is None}
        self._file_arguments = {m["name"]: m.get("file_argument") for m in actions}
        self._descriptions = {m["name"]: m["description"] for m in self.manifest["invariants"]}
        # The names of the app's actions, queries and views, by their list in the manifest.
        self.names = {
            kind: frozenset(m["name"] for m in self.manifest[kind])
            for kind in ("actions", "queries", "views")
        }
        self._functions: dict[str, Callable[..., Any]] = {
            getattr(s.fn, "__name__", ""): s.fn for s in [*app.actions, *app.queries, *app.views]
        }
        self._invariants = [(getattr(s.fn, "__name__", ""), s.fn) for s in app.invariants]
        self._models: dict[str, type[BaseModel]] = {}
        self._snapshot_lock = threading.Lock()
        self._kept: tuple[str | None, tempfile.TemporaryDirectory[str]] | None = None

    def _local(self, now: datetime) -> datetime:
        """The clock's instant in the app's own zone, as ctx.now gives it."""
        return now.astimezone(self._zone)

    def close(self) -> None:
        """Remove the kept snapshot of the data."""
        with self._snapshot_lock:
            if self._kept is not None:
                _remove(self._kept[1])
                self._kept = None

    # The actions

    def run_action(
        self,
        caller: Caller,
        now: datetime,
        action: str,
        arguments: dict[str, Any],
        files: dict[str, bytes],
        inputs: Mapping[str, Input] | None = None,
    ) -> Changes | Refusal:
        """Run an action on a working copy of `files`, then every invariant.

        The file of its file argument, in `inputs`, is in the working copy's `INPUTS` folder,
        read-only, while the action runs, and the argument is its path: it is gone before the
        invariants run. Raises `WrongArguments` for a file the call should bring and didn't,
        or one for an argument that isn't a file, and `AppError` when what it returns doesn't
        match its return annotation, or its JSON is longer than `MAX_RESULT`.
        """
        fn = self._functions[action]
        given = self._given(action, arguments, inputs or {})
        paths = {argument: f"{INPUTS}/{one.name}" for argument, one in given.items()}
        kwargs = self._validate(action, fn, ActionContext, {**arguments, **paths})
        ctx = ActionContext(caller, self._local(now))

        def run() -> Any:
            with _inputs(given), _app_code():
                value = fn(ctx, **kwargs)
            if action in self._no_result:
                return None  # `-> None`: whatever it returns is ignored, as before results
            return result_json(fn, value, f"action {action}", bounded=True)

        return self._execute(caller, action, files, run)

    def _given(
        self, action: str, arguments: dict[str, Any], inputs: Mapping[str, Input]
    ) -> dict[str, Input]:
        """The file each of the action's file arguments gets, with its name in `INPUTS`."""
        argument = self._file_arguments.get(action)
        wrong = [name for name in inputs if name != argument]
        if wrong:
            raise WrongArguments(f"{listed(wrong)} isn't a file the action {action} takes.")
        if argument is not None and arguments.get(argument) is not None and not inputs:
            raise WrongArguments(
                f"{argument} is a file the person uploads, and the call brought none: pass "
                "the reference app_ask_for_file gives."
            )
        return {name: Input(input_name(one.name), one.content) for name, one in inputs.items()}

    def edit(
        self, caller: Caller, label: str, files: dict[str, bytes], edit: Callable[[], None]
    ) -> Changes | Refusal:
        """Run an edit made outside any action, then every invariant. It has no result."""

        def run() -> None:
            edit()  # whatever the edit returns is dropped

        return self._execute(caller, label, files, run)

    def apply(
        self, caller: Caller, label: str, files: dict[str, bytes], result: dict[str, bytes]
    ) -> Changes | Refusal:
        """Check a change made elsewhere: `files` before, `result` after."""

        def put() -> None:
            for name in files:
                if name not in result:
                    Path(name).unlink()
            write_files(Path("."), result)

        return self._execute(caller, label, files, put)

    # The reads

    def query_json(
        self,
        caller: Caller,
        now: datetime,
        query: str,
        arguments: dict[str, Any],
        files: dict[str, bytes],
        state: str | None,
    ) -> Any:
        """Run a query read-only, and return its result as JSON data, as its output schema
        describes it.

        Raises `AppError` when what it returns doesn't match its return annotation.
        """
        fn = self._functions[query]
        kwargs = self._validate(query, fn, QueryContext, arguments)
        ctx = QueryContext(caller, self._local(now))
        with self._snapshot(files, state) as snapshot, _APP_CODE, chdir(snapshot), _app_code():
            result = fn(ctx, **kwargs)
        return result_json(fn, result, f"query {query}", bounded=False)

    def view(
        self,
        caller: Caller,
        now: datetime,
        view: str,
        arguments: dict[str, Any],
        files: dict[str, bytes],
        state: str | None,
    ) -> dict[str, Any]:
        """Lay out a view read-only, and return `{"arguments", "body"}`: the arguments that draw
        it again, and its tree, which the runtime checks.

        Raises `WrongArguments` for arguments the view doesn't take, and `AppError` for a
        screen that can't be laid out, such as a template that uses a value it isn't given.
        """
        fn = self._functions[view]
        kwargs = self._validate(view, fn, QueryContext, arguments)
        ctx = QueryContext(caller, self._local(now))
        with self._snapshot(files, state) as snapshot, _APP_CODE, chdir(snapshot):
            with _app_code():
                screen = fn(ctx, **kwargs)
            # The template may read the values' properties, which are the app's code too.
            try:
                return lay_out(screen, view, self._model(view, fn))
            except AppError, Refused, Denied:
                raise
            except Exception as e:
                raise AppFailed.of(e, " while laying out the screen") from e

    def check(self, now: datetime, files: dict[str, bytes]) -> Checked:
        """Run every invariant on `files`, and lay out every view for each of its roles.

        Raises `AppFailed` if an invariant fails."""
        with _workspace(files) as data, _read_only(data):
            refusal = self._run_invariants(Caller("cliquet", None), data, data, CHECK)
        views: list[Problem] = []
        for name, spec in self._views.items():
            for role in spec["roles"] or [None]:
                who = f"view {name}" + (f", as {role}" if role else "")
                try:
                    raw = self.view(Caller("cliquet", role), now, name, {}, files, None)
                    finish(raw, self.manifest, role, name)
                except AppError as e:
                    views += [Problem(who, p.message) for p in e.problems]
                except Exception as e:
                    message = f"it failed while laying out its screen: {described(e)[1]}"
                    views.append(Problem(who, message))
        return Checked(refusal, views)

    # The write path

    def _execute(
        self, caller: Caller, label: str, files: dict[str, bytes], run: Callable[[], Any]
    ) -> Changes | Refusal:
        """Run `run` on a working copy, then every invariant. What it returns is the result."""
        with _workspace(files) as before, _workspace(files) as work:
            with _read_only(before):
                with _APP_CODE, chdir(work):
                    try:
                        answered = run()
                    except Refused as e:
                        return Refusal(message=e.message)
                names = _names(work)
                extra = [name for name in names if not declares(self._files, name)]
                if extra:
                    return Refusal(
                        message=f"the change left files the app doesn't declare: "
                        f"{', '.join(extra)}. App(files=[...]) declares "
                        f"{listed(list(self._files))}: declare them there too, or remove them."
                    )
                with _read_only(work):
                    refusal = self._run_invariants(caller, before, work, label)
            if refusal is not None:
                return refusal
            # Every file left is declared: any other refused the change.
            result = {n: (work / n).read_bytes() for n in names if (work / n).is_file()}
        return Changes(
            {f: result.get(f) for f in sorted({*files, *result}) if files.get(f) != result.get(f)},
            answered,
        )

    def _run_invariants(
        self, caller: Caller, before: Path, after: Path, label: str
    ) -> Refusal | None:
        """Run every invariant: the refusal of those that found problems, if any.

        Raises `AppFailed` if one raises, or returns something other than a list of messages.
        """
        ctx = InvariantContext(caller, before, label)
        problems: dict[str, list[str]] = {}
        for name, fn in self._invariants:
            with _APP_CODE, chdir(after):
                try:
                    found = fn(ctx)
                except Exception as e:  # Refused too: an invariant answers its problems
                    raise AppFailed.of(e, f", in the invariant {name}") from e
            if not isinstance(found, list) or not all(isinstance(p, str) for p in found):
                shown = shortened(repr(found), 200)
                raise AppFailed(
                    f"TypeError: the invariant {name} returned {shown}, not a list of messages",
                    "TypeError",
                )
            if found:
                problems[name] = found
        if not problems:
            return None
        return Refusal(invariants=problems, descriptions=self._descriptions)

    def _model(
        self, name: str, fn: Callable[..., Any], context: type = QueryContext
    ) -> type[BaseModel]:
        """The model of a function's arguments, made once."""
        model = self._models.get(name)
        if model is None:
            model = self._models[name] = arguments_model(fn, context)
        return model

    def _validate(
        self, name: str, fn: Callable[..., Any], context: type, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        model = self._model(name, fn, context)
        try:
            parsed = model.model_validate(arguments)
        except ValidationError as e:
            takes = {_public(key, f): f.description for key, f in model.model_fields.items()}
            problems = wrong_arguments(name, e.errors(include_url=False), takes, arguments)
            raise WrongArguments(problems) from e
        return {field: getattr(parsed, field) for field in type(parsed).model_fields}

    @contextmanager
    def _snapshot(self, files: dict[str, bytes], state: str | None) -> Generator[Path]:
        """A read-only folder holding `files`, kept per `state` when it names one."""
        if state is None:
            with _workspace(files) as root, _read_only(root):
                yield root
            return
        with self._snapshot_lock:
            if self._kept is None or self._kept[0] != state:
                if self._kept is not None:
                    _remove(self._kept[1])
                directory = tempfile.TemporaryDirectory(prefix="cliquet-data-")
                write_files(Path(directory.name), files)
                _freeze(Path(directory.name))
                self._kept = (state, directory)
            root = Path(self._kept[1].name)
        yield root


def _public(name: str, field: FieldInfo) -> str:
    """An argument's public name: the one a call gives it, as its schema lists it."""
    alias = field.validation_alias
    return alias if isinstance(alias, str) else field.alias or name


def _return_text(fn: Callable[..., Any]) -> str:
    """The return annotation as the app's code writes it."""
    return annotationlib.get_annotations(fn, format=annotationlib.Format.STRING)["return"]


def result_json(fn: Callable[..., Any], value: Any, where: str, *, bounded: bool) -> Any:
    """What an action or a query returned, as JSON data by alias, as its `output_schema`
    describes it. `where` names it, such as "query stock".

    Raises `AppError` if it doesn't match its return annotation, or, when `bounded`, if
    its JSON is longer than `MAX_RESULT`.
    """
    name = getattr(fn, "__name__", repr(fn))
    try:
        raw = return_adapter(fn).dump_json(value, by_alias=True, warnings="error")
    except ValueError:  # pydantic's serialization errors are ValueErrors
        shown = shortened(repr(value), 200)
        message = f"{name} returned {shown}, which doesn't match `-> {_return_text(fn)}`"
        raise AppError([Problem(where, message)]) from None
    if bounded and len(raw) > MAX_RESULT:
        message = (
            f"{name} returned a result of more than {size_text(MAX_RESULT)} as JSON. Return less."
        )
        raise AppError([Problem(where, message)])
    return json.loads(raw)


def json_length(data: Any) -> int:
    """How many bytes `data` takes as compact UTF-8 JSON: what `MAX_RESULT` bounds."""
    return len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())


def undo_files(
    current: dict[str, bytes], before: dict[str, bytes], after: dict[str, bytes], commit: str
) -> dict[str, bytes]:
    """The data with what `commit` changed undone, keeping later changes. Raises `Refused`."""
    result = dict(current)
    short = commit[:7]
    for name in sorted({*before, *after}):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        now = result.get(name)
        if now == old:
            continue
        if now == new:
            if old is None:
                del result[name]
            else:
                result[name] = old
            continue
        if now is None or old is None or new is None:
            raise Refused(f"{short} can't be undone: {name} was added or removed, then changed")
        result[name] = _merge(now, new, old, name, short)
    return result


@contextmanager
def _inputs(given: Mapping[str, Input]) -> Generator[None]:
    """The files of a call in its working copy, the current folder, in `INPUTS`, read-only:
    removed when the block ends, before anything looks at what the call left."""
    if not given:
        yield
        return
    folder = Path(INPUTS)
    folder.mkdir()
    for one in given.values():
        (folder / one.name).write_bytes(one.content)
    _freeze(folder)
    try:
        yield
    finally:
        # The app's code may have changed what is there: whatever is left is removed.
        with suppress(OSError):
            _thaw(folder)
        shutil.rmtree(folder, ignore_errors=True)


@contextmanager
def _app_code() -> Generator[None]:
    """Report what the app's code raises as `AppFailed`, with it as the cause. `Refused` and
    `Denied` keep the app's own message."""
    try:
        yield
    except Refused, Denied:
        raise
    except Exception as e:
        raise AppFailed.of(e) from e


def _names(root: Path) -> list[str]:
    """The name of every file under `root`, relative to it, sorted."""
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if not p.is_dir())


def _freeze(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        path.chmod(0o555 if path.is_dir() else 0o444)


def _thaw(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)


def _remove(directory: tempfile.TemporaryDirectory[str]) -> None:
    _thaw(Path(directory.name))
    directory.cleanup()


@contextmanager
def _workspace(files: dict[str, bytes]) -> Generator[Path]:
    directory = tempfile.TemporaryDirectory(prefix="cliquet-work-")
    try:
        root = Path(directory.name)
        write_files(root, files)
        yield root
    finally:
        _remove(directory)


@contextmanager
def _read_only(root: Path) -> Generator[None]:
    _freeze(root)
    try:
        yield
    finally:
        _thaw(root)


def _merge(current: bytes, base: bytes, target: bytes, name: str, short: str) -> bytes:
    """Three-way merge: apply base -> target onto current, as `git merge-file` does."""
    with tempfile.TemporaryDirectory(prefix="cliquet-merge-") as tmp:
        paths = []
        for label, content in (("current", current), ("base", base), ("target", target)):
            p = os.path.join(tmp, label)
            Path(p).write_bytes(content)
            paths.append(p)
        result = subprocess.run(["git", "merge-file", "-p", *paths], capture_output=True)
    if result.returncode != 0:
        raise Refused(f"{short} can't be undone: {name} changed since in the same places")
    return result.stdout


# The worker process: a Worker behind a pipe, one JSON object per line.

# The ops that name a function of the app: what it is, and its list in the manifest. The
# server's engine names what it asked for by it too.
NAMED_OPS = {
    "call": ("action", "actions"),
    "query": ("query", "queries"),
    "view": ("view", "views"),
}
_MIGRATE_ERROR = "migrate.py must define migrate()"


class _UnknownOp(Exception):
    def __init__(self, op: object) -> None:
        self.op = op


class _MissingMigration(Exception):
    pass


# How much of a traceback a failed call's answer carries: its end, where the app's code is.
MAX_TRACEBACK = 8_000


def handle(worker: Worker, folder: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Run one request and return its answer, without the `id`."""
    op = request.get("op") if isinstance(request, dict) else None
    if not isinstance(op, str):
        return _error("unknown", "the request names no op")
    named = NAMED_OPS.get(op)
    if named is not None:
        noun, kind = named
        name = request.get("name")
        if not isinstance(name, str) or name not in worker.names[kind]:
            return _error("unknown", f"there is no {noun} named {name}")
    try:
        return _answer(worker, folder, request)
    except InvalidArguments as e:
        return _error("invalid_arguments", str(e))
    except Refused as e:
        return _error("refused", e.message)
    except Denied as e:
        return _error("denied", str(e))
    except _MissingMigration:
        return _error("failed", _MIGRATE_ERROR)
    except AppFailed as e:
        return _failed(str(e), e.kind, e.__cause__)
    except AppError as e:
        return _app_error(e, folder)
    except _UnknownOp as e:
        return _error("unknown", f"unknown op {e.op}")
    except Exception as e:
        return _failed(f"{type(e).__name__}: {e}", type(e).__name__, e)


def serve(folder: Path, lines: Iterable[str], out: TextIO) -> None:
    """Load the app in `folder`, then answer each request line on `out`."""
    try:
        worker = Worker(folder / "app.py")
    except AppError as e:
        _write(out, {"id": None, **_app_error(e, folder)})
        return
    try:
        for line in lines:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                _write(out, {"id": None, "error": {"kind": "failed", "message": "not JSON"}})
                continue
            answer = handle(worker, folder, request)
            _write(out, {"id": request.get("id") if isinstance(request, dict) else None, **answer})
    finally:
        worker.close()


def main(argv: list[str] | None = None) -> int:
    """`python -m cliquet.worker <folder>`: answer on the pipe, everything else on stderr."""
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: python -m cliquet.worker <folder>", file=sys.stderr)
        return 2
    with os.fdopen(os.dup(1), "w") as out:
        os.dup2(2, 1)
        sys.stdout = sys.stderr
        log_to_stderr(("cliquet",))
        serve(Path(args[0]), sys.stdin, out)
    return 0


def _answer(worker: Worker, folder: Path, request: dict[str, Any]) -> dict[str, Any]:
    op = request["op"]  # a string: `handle` checked it
    if op == "manifest":
        return {"result": worker.manifest}
    if op == "call":
        result = worker.run_action(
            _caller(request),
            _now(request),
            request["name"],
            request["arguments"],
            _decode(request["files"]),
            _inputs_of(request.get("inputs")),
        )
        return _change(result)
    if op == "apply":
        result = worker.apply(
            _caller(request),
            request["label"],
            _decode(request["files"]),
            _decode(request["result"]),
        )
        return _change(result)
    if op == "migrate":
        # The publish keeps the migration's change, so the invariants check it as the publish.
        result = worker.edit(
            _caller(request), PUBLISH, _decode(request["files"]), _migration(folder)
        )
        return _change(result)
    if op == "query":
        result = worker.query_json(
            _caller(request),
            _now(request),
            request["name"],
            request["arguments"],
            _decode(request["files"]),
            request.get("state"),
        )
        return {"result": result}
    if op == "view":
        screen = worker.view(
            _caller(request),
            _now(request),
            request["name"],
            request["arguments"],
            _decode(request["files"]),
            request.get("state"),
        )
        return {"result": screen}
    if op == "check":
        checked = worker.check(_now(request), _decode(request["files"]))
        refusal = None if checked.refusal is None else _refusal(checked.refusal)
        views = [{"where": p.where, "message": p.message} for p in checked.views]
        return {"result": {"refusal": refusal, "views": views}}
    raise _UnknownOp(op)


def _caller(request: dict[str, Any]) -> Caller:
    data = request["caller"]
    return Caller(data["id"], data.get("role"), data.get("token_id"))


def _now(request: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(request["now"])


def _decode(files: dict[str, str]) -> dict[str, bytes]:
    return {name: base64.b64decode(text) for name, text in files.items()}


def _inputs_of(inputs: dict[str, dict[str, str]] | None) -> dict[str, Input]:
    """The files of a call, as the request carries them: each argument's name and base64."""
    return {
        argument: Input(str(one["name"]), base64.b64decode(one["content"]))
        for argument, one in (inputs or {}).items()
    }


def _encode(changes: dict[str, bytes | None]) -> dict[str, str | None]:
    return {
        name: None if content is None else base64.b64encode(content).decode()
        for name, content in changes.items()
    }


def _change(result: Changes | Refusal) -> dict[str, Any]:
    if isinstance(result, Refusal):
        return {"refusal": _refusal(result)}
    return {"changes": _encode(result.files), "result": result.result}


def _refusal(refusal: Refusal) -> dict[str, Any]:
    return {
        "message": refusal.message,
        "invariants": refusal.invariants,
        "descriptions": refusal.descriptions,
    }


def _migration(folder: Path) -> Callable[[], None]:
    path = folder / MIGRATION
    if not path.is_file():
        raise _MissingMigration
    spec = importlib.util.spec_from_file_location("cliquet_migration", path)
    if spec is None or spec.loader is None:
        raise _MissingMigration
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    migrate = getattr(module, "migrate", None)
    if not callable(migrate):
        raise _MissingMigration
    return migrate


def _error(kind: str, message: str) -> dict[str, Any]:
    return {"error": {"kind": kind, "message": message}}


def _failed(
    message: str, exception: str | None, raised: BaseException | None = None
) -> dict[str, Any]:
    """The answer of a call the app's code failed: `type` is the exception's, as Cliquet read
    it from the exception, so the server never takes it from the text. With what the code
    `raised`, `traceback` is the end of its traceback, which says where in app.py: the
    server keeps it for the app's owner."""
    error = {"kind": "failed", "message": message, "type": exception}
    if raised is not None:
        error["traceback"] = "".join(traceback.format_exception(raised))[-MAX_TRACEBACK:]
    return {"error": error}


def _app_error(error: AppError, folder: Path) -> dict[str, Any]:
    """An app's problems, each with where it is in the app's folder, so the builder and the
    engine name the line, and as one text."""
    inside = f"{folder.resolve()}/"
    answer = _error("app_error", "\n".join(f"- {p.message}" for p in error.problems))
    answer["error"]["problems"] = [
        {"where": p.where.removeprefix(inside), "message": p.message} for p in error.problems
    ]
    return answer


def _write(out: TextIO, answer: dict[str, Any]) -> None:
    out.write(json.dumps(answer, separators=(",", ":")) + "\n")
    out.flush()


if __name__ == "__main__":
    sys.exit(main())
