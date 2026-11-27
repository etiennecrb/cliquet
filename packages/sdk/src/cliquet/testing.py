"""Test an app the way the runtime runs it: working copy, action, invariants, commit.

    world = given(shop, {"products.csv": PRODUCTS}, now=NOW)
    staff = world.connect("staff")
    staff.call(record_sale, sku="TEA-SEN", quantity=1, receipt="R-1").expect_ok()
    assert staff.query(stock)[0].quantity == 9

Tests never read the app's `data/` folder. Each `given` starts a new git
repository holding only the files it is given.
"""

import copy
import inspect
import json
import sys
import tempfile
import weakref
from collections.abc import Callable
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Concatenate, Self, cast

from pydantic import ValidationError

from cliquet.app import App, Invariant
from cliquet.context import ActionContext, Caller, QueryContext
from cliquet.errors import Denied, Refused, shortened
from cliquet.files import declares
from cliquet.inputs import Claim, Given
from cliquet.manifest import arguments_model, return_adapter
from cliquet.runtime import EVERYONE, Committed, InvalidArguments, Refusal, Runtime
from cliquet.store import scratch_repository

if TYPE_CHECKING:
    from cliquet.screens import Screen

__all__ = [
    "Connection",
    "Denied",
    "InvalidArguments",
    "Outcome",
    "Refused",
    "Transaction",
    "World",
    "given",
]

TEST_ID = "test"

# Every invariant that refused a change in this process, per app module file.
# The pytest plugin uses it to fail a run where an invariant never refused.
refused: dict[str, set[str]] = {}
seen: dict[str, tuple[str, list[str]]] = {}


class Outcome[R]:
    """What happened to a change: committed or refused, with assertions about it.

    `result` is what the action returned, rebuilt from its JSON as the caller gets it:
    a model instance for `-> Imported`. It is None after a refusal, and for `-> None`.
    """

    def __init__(self, outcome: Committed | Refusal, app: App, rebuild: Callable[[Any], R]) -> None:
        """`rebuild` turns the result's JSON data back into the action's return type."""
        self._outcome = outcome
        self._app = app
        self._rebuild = rebuild

    @cached_property
    def result(self) -> R | None:
        """The action's result as its own type, rebuilt once from its JSON. None if refused."""
        if not isinstance(self._outcome, Committed):
            return None
        return self._rebuild(self._outcome.result)

    @property
    def commit(self) -> str | None:
        return self._outcome.commit if isinstance(self._outcome, Committed) else None

    def expect_ok(self) -> R:
        """Assert the change was committed, and return what the action returned."""
        if isinstance(self._outcome, Refusal):
            raise AssertionError(f"expected the change to be committed, but:\n{self._outcome}")
        return cast(R, self.result)

    def expect_refused(
        self, *, by: Invariant | None = None, message: str | None = None
    ) -> list[str]:
        """Assert the change was refused: by an invariant, or by the action with a message.

        Returns the messages: the invariant's problems with `by`, else every message, so a
        test can check their wording.
        """
        result = self._outcome
        if not isinstance(result, Refusal):
            raise AssertionError("expected the change to be refused, but it was committed")
        if by is not None:
            spec = self._app.find_invariant(by)
            if spec is None:
                raise TypeError(f"{by!r} is not an invariant of {self._app.name}")
            name = getattr(by, "__name__", "")
            if name not in result.invariants:
                raise AssertionError(f"expected a refusal by {spec.description!r}, but:\n{result}")
        if message is not None and result.message != message:
            raise AssertionError(f"expected the refusal {message!r}, but:\n{result}")
        if by is not None:
            return list(result.invariants[getattr(by, "__name__", "")])
        problems = [p for found in result.invariants.values() for p in found]
        return [result.message, *problems] if result.message else problems


class Connection:
    """A caller connected to the app with one role."""

    # The transaction its calls are made in, for a `Transaction`.
    _transaction: str | None = None
    # The version of the data its calls run on only, from `if_unchanged_since`.
    _unchanged: str | None = None

    def __init__(self, world: World, caller: Caller) -> None:
        self.world = world
        self.caller = caller

    def call[**P, R](
        self,
        action: Callable[Concatenate[ActionContext, P], R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Outcome[R]:
        """Run an action through the runtime. Raises `Denied` if the role may not.

        A file argument takes the file's bytes, or a `Path` to a file the test wrote: the
        action gets it as it gets a file the person uploads."""
        if self.world.app.find_action(action) is None:
            raise TypeError(f"{action!r} is not an action of {self.world.app.name}")
        arguments = _arguments(action, args, kwargs)
        name = getattr(action, "__name__", "")
        (spec,) = [a for a in self.world.runtime.manifest["actions"] if a["name"] == name]
        inputs = _given_file(action, spec.get("file_argument"), arguments)
        outcome = self.world.runtime.call(
            self.caller,
            name,
            arguments,
            transaction=self._transaction,
            if_unchanged_since=self._unchanged,
            inputs=inputs,
        )
        return self.world._record(outcome, _rebuilder(action, spec["output_schema"] is not None))

    def query[**P, R](
        self,
        query: Callable[Concatenate[QueryContext, P], R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """Run a query through the runtime and return its result, rebuilt from its JSON as
        the caller gets it: a model instance for `-> StockLevel`.

        Raises `AssertionError` if the result can't be rebuilt from that JSON, and, as an
        agent is refused it, an error for a result past 1 MB as JSON.
        """
        if self.world.app.find_query(query) is None:
            raise TypeError(f"{query!r} is not a query of {self.world.app.name}")
        arguments = _arguments(query, args, kwargs)
        name = getattr(query, "__name__", "")
        data = self.world.runtime.query_json(self.caller, name, arguments, self._transaction)
        return _rebuilder(query, True)(data)

    def view[**P](
        self,
        view: Callable[Concatenate[QueryContext, P], Screen],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> dict[str, Any]:
        """Lay out a view through the runtime, with its arguments, and return the screen as
        the browser gets it.

        `screen["arguments"]` are the arguments that draw it again, and `screen["body"]` its
        tree: `str(screen)` holds every text it shows and every tool it calls. Raises `Denied`
        if the role may not open it, and `AppError` if the screen breaks a rule for this role,
        such as a form calling a tool the role may not use.
        """
        if self.world.app.find_view(view) is None:
            raise TypeError(f"{view!r} is not a view of {self.world.app.name}")
        arguments = _arguments(view, args, kwargs)
        name = getattr(view, "__name__", "")
        return self.world.runtime.view(self.caller, name, arguments, self._transaction)

    @property
    def data_version(self) -> str:
        """The `app_data_version` a query's answer gives this caller now: the version of the
        data it reads, live or in its transaction."""
        return self.world.runtime.version(self.caller, self._transaction)

    def if_unchanged_since(self, version: str) -> Self:
        """This caller, whose calls run only on the data of `version`, as an agent passes it
        as `app_if_unchanged_since`: on any other, a call is refused without running."""
        unchanged = copy.copy(self)
        unchanged._unchanged = version
        return unchanged

    def begin(self) -> Transaction:
        """Begin a transaction: its calls are kept only once it is committed.

        Raises `InvalidArguments` if this caller already has one open.
        """
        return Transaction(self.world, self.caller, self.world.runtime.begin(self.caller))


class Transaction(Connection):
    """A transaction of one caller: `call`, `query` and `view` read and change its data."""

    def __init__(self, world: World, caller: Caller, id: str) -> None:
        super().__init__(world, caller)
        self._transaction = id

    @property
    def id(self) -> str:
        """The transaction's id, as an agent passes it."""
        assert self._transaction is not None
        return self._transaction

    def commit(self) -> Outcome[None]:
        """Keep the transaction's changes, or say why they weren't kept."""
        outcome = self.world.runtime.commit_transaction(self.caller, self.id)
        return self.world._record(outcome, lambda _: None)

    def rollback(self) -> None:
        """Drop the transaction: nothing it changed is kept."""
        self.world.runtime.rollback(self.caller, self.id)


class World:
    """An app running on test data, with a fixed clock."""

    def __init__(
        self, app: App, module: ModuleType, files: dict[str, str | bytes], now: datetime
    ) -> None:
        self.app = app
        self._directory = tempfile.TemporaryDirectory(prefix="cliquet-test-")
        for name in files:
            if not declares(app.files, name):
                raise ValueError(
                    f"{name!r} is not one of {app.name}'s files: {', '.join(app.files)}"
                )
        contents = {n: c.encode() if isinstance(c, str) else c for n, c in files.items()}
        # Dated `now`, so that the history's times agree with the app's clock.
        data = scratch_repository(Path(self._directory.name), contents, when=now)
        module_file = str(Path(module.__file__ or "").resolve())
        # A test reads every error in full and sees every change: each caller is the owner.
        self.runtime: Runtime = Runtime(
            module_file,
            data_dir=data,
            module_name=module.__name__,
            clock=lambda: now,
            owner=EVERYONE,
        )
        self._key = module_file
        weakref.finalize(self, self.runtime.close)
        seen[module_file] = (app.name, [d["name"] for d in self.runtime.manifest["invariants"]])
        refused.setdefault(module_file, set())

    def connect(
        self, role: str | None = None, /, *, id: str = TEST_ID, token_id: str | None = None
    ) -> Connection:
        """Connect as a caller acting with `role`, through the API token `token_id` if given.

        `role` may be left out with one role or none."""
        roles = self.app.roles
        if role is None and len(roles) > 1:
            raise TypeError(
                f"{self.app.name} has several roles: connect with one of {', '.join(roles)}"
            )
        if role is None and roles:
            role = roles[0]
        if role is not None and role not in roles:
            declared = ", ".join(roles) or "none"
            raise ValueError(f"{role!r} is not a role of {self.app.name} (roles: {declared})")
        return Connection(self, Caller(id, role, token_id))

    def change(
        self, file: str, *, replace: tuple[str, str] | None = None, append: str | None = None
    ) -> Outcome[None]:
        """Edit a data file directly, bypassing actions, so an invariant can be tested alone.

        The invariants see it as `ctx.action == "app_change"`, a name no action can have."""
        if not declares(self.app.files, file):
            raise ValueError(f"{file!r} is not one of {self.app.name}'s files")

        def edit() -> None:
            path = Path(file)
            path.parent.mkdir(parents=True, exist_ok=True)
            text = path.read_text() if path.exists() else ""
            if replace is not None:
                if replace[0] not in text:
                    raise AssertionError(f"{replace[0]!r} is not in {file}")
                text = text.replace(*replace)
            path.write_text(text + (append or ""))

        return self._record(self.runtime.change(Caller(TEST_ID, None), edit), lambda _: None)

    def _record[R](self, result: Committed | Refusal, rebuild: Callable[[Any], R]) -> Outcome[R]:
        if isinstance(result, Refusal):
            refused[self._key].update(result.invariants)
        return Outcome(result, self.app, rebuild)


def given(app: App, files: dict[str, str | bytes], *, now: datetime | None = None) -> World:
    """Set up `app` on these files only, with the clock fixed at `now`.

    `ctx.now` is that instant in the app's time zone. Without `now`, it is the time at the
    start of the test.
    """
    module = _module_of(app)
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("now must be timezone-aware, such as datetime(..., tzinfo=ZoneInfo(...))")
    return World(app, module, files, moment)


def _rebuilder[R](fn: Callable[..., R], has_result: bool) -> Callable[[Any], R]:
    """Turns an action's or a query's JSON result back into its return type, as a test
    reads it."""
    if not has_result:  # `-> None`
        return lambda _: cast(R, None)
    adapter = return_adapter(fn)
    name = getattr(fn, "__name__", repr(fn))

    def rebuild(data: Any) -> R:
        text = json.dumps(data)
        try:
            return adapter.validate_json(text)
        except ValidationError as e:
            raise AssertionError(
                f"{name}'s result can't be rebuilt from the JSON an agent gets: "
                f"{shortened(text, 500)}\n{e}"
            ) from None

    return rebuild


def _module_of(app: App) -> ModuleType:
    """The module that created the app, not one that only imported it."""
    module = sys.modules.get(app.module_name)
    if module is None or getattr(module, "__file__", None) is None:
        raise ValueError(f"can't find the module {app.module_name!r} that defines {app!r}")
    return module


# What a test's call passes as the reference to the file it gives, as an agent passes one.
_GIVEN = "given-by-the-test"


def _given_file(
    action: Callable[..., Any], argument: str | None, arguments: dict[str, Any]
) -> dict[str, Claim]:
    """The file a test gives an action's file `argument`, by its name in the schema, taken out
    of `arguments` and put back as a reference, as an agent passes it."""
    if argument is None:
        return {}
    fields = arguments_model(action, ActionContext).model_fields
    python = next(
        key
        for key, field in fields.items()
        if argument in (key, field.alias, field.validation_alias)
    )
    value = arguments.pop(python, None)
    if value is None:
        return {}
    if not isinstance(value, bytes | bytearray | Path):
        raise TypeError(
            f"{python} is a file: pass its bytes, or a Path to a file the test wrote, not "
            f"{type(value).__name__} {shortened(repr(value), 60)}. An agent passes a reference "
            "from app_ask_for_file; a test gives the file itself."
        )
    arguments[argument] = _GIVEN
    return {argument: Given.of(value)}


def _arguments(
    fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    bound = inspect.signature(fn).bind(None, *args, **kwargs)
    ctx = next(iter(bound.arguments))
    return {k: v for k, v in bound.arguments.items() if k != ctx}
