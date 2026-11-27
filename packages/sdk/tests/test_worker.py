import itertools
import sys
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import pytest
from pydantic import AfterValidator, ConfigDict, Field, ValidationError, create_model
from pydantic_core import PydanticCustomError
from test_runtime import LOG_APP, NOW, OWNER, TALLY, skip_as_root

from cliquet.context import Caller
from cliquet.errors import AppError, AppFailed, Refused
from cliquet.inputs import Input
from cliquet.runtime import Committed, Runtime
from cliquet.tools import wrong_arguments
from cliquet.worker import (
    Changes,
    Checked,
    InvalidArguments,
    Refusal,
    Worker,
    return_adapter,
    undo_files,
)

_names = itertools.count()
FILES = {"log.txt": b"start\n"}


@pytest.fixture
def worker(tmp_path: Path) -> Iterator[Worker]:
    path = tmp_path / "app.py"
    path.write_text(LOG_APP)
    made = Worker(path, module_name=f"cliquet_worker_{next(_names)}")
    yield made
    made.close()


def test_an_action_returns_the_files_it_changed(worker: Worker) -> None:
    changed = worker.run_action(OWNER, NOW, "append", {"line": "hello"}, FILES)
    assert changed == Changes({"log.txt": b"start\nhello\n"})
    unchanged = worker.run_action(OWNER, NOW, "rewrite", {"text": "start\n"}, FILES)
    assert unchanged == Changes({})


@pytest.fixture
def tally() -> Iterator[Worker]:
    made = Worker(TALLY, module_name=f"cliquet_worker_{next(_names)}")
    yield made
    made.close()


def test_an_action_result_is_json(tally: Worker) -> None:
    added = tally.run_action(OWNER, NOW, "add", {"amounts": ["1.50", "2"]}, {})
    assert added == Changes(
        {"amounts.txt": b"1.50\n2\n"}, {"added": 2, "total": "3.50", "day": "2026-11-10"}
    )
    assert tally.run_action(OWNER, NOW, "count", {}, {"amounts.txt": b"1\n"}) == Changes({}, 1)
    assert tally.run_action(OWNER, NOW, "clear", {}, {}) == Changes({"amounts.txt": b""}, None)


def test_a_result_that_does_not_fit_fails_the_call(tally: Worker) -> None:
    with pytest.raises(AppError) as error:
        tally.run_action(OWNER, NOW, "wrong", {}, {})
    assert [p.message for p in error.value.problems] == [
        "wrong returned 3, which doesn't match `-> Added`"
    ]


def test_a_result_over_1_mb_fails_the_call(tally: Worker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cliquet.worker.MAX_RESULT", 10)
    assert tally.run_action(OWNER, NOW, "count", {}, {}) == Changes({}, 0)
    # The message says the limit: here, the one the test set.
    with pytest.raises(AppError, match="add returned a result of more than 10 bytes as JSON"):
        tally.run_action(OWNER, NOW, "add", {"amounts": ["1"]}, {})


def test_return_adapter_without_a_cache_or_an_annotation() -> None:
    class Unhashable:
        __hash__ = None  # type: ignore[assignment]

        def __call__(self) -> int:
            return 1

    assert return_adapter(Unhashable()).dump_python(1) == 1

    def bare():
        return 1

    with pytest.raises(TypeError, match="bare has no return annotation"):
        return_adapter(bare)


def test_an_action_or_an_invariant_can_refuse(worker: Worker) -> None:
    assert worker.run_action(OWNER, NOW, "append", {"line": ""}, FILES) == Refusal(
        message="Empty line"
    )
    refused = worker.run_action(OWNER, NOW, "append", {"line": "forbidden"}, FILES)
    assert isinstance(refused, Refusal)
    assert "no_forbidden" in refused.invariants


def test_wrong_arguments_raise(worker: Worker) -> None:
    with pytest.raises(InvalidArguments):
        worker.run_action(OWNER, NOW, "append", {"line": 3, "extra": 1}, FILES)


def test_wrong_arguments_say_what_is_wrong_in_plain_words(worker: Worker) -> None:
    # The worker knows the app's own arguments: the server adds the ones Cliquet adds.
    with pytest.raises(InvalidArguments) as error:
        worker.run_action(OWNER, NOW, "append", {"line": 3, "extra": 1, "more": 2}, FILES)
    assert str(error.value).splitlines() == [
        "append doesn't take extra and more. It takes line.",
        "line should be a valid string.",
    ]
    # By its public name, the alias.
    with pytest.raises(InvalidArguments, match=r"^day is required\.$"):
        worker.run_action(OWNER, NOW, "dated", {}, FILES)


def test_wrong_arguments_name_the_argument_whatever_pydantic_says() -> None:
    def odd(value: int) -> int:
        if value % 2 == 0:
            raise ValueError("must be odd")
        return value

    def unique(names: list[str]) -> list[str]:
        raise PydanticCustomError("dup", "Names should be unique")

    model = create_model(
        "m",
        __config__=ConfigDict(extra="forbid"),
        either=(int | list[int], 1),
        odd=(Annotated[int, AfterValidator(odd), Field(description="An odd number")], 1),
        names=(Annotated[list[str], AfterValidator(unique), Field(description="SKU names")], []),
    )
    given = {"either": "x", "odd": 2, "names": ["a"]}
    with pytest.raises(ValidationError) as error:
        model.model_validate(given)
    takes = {"either": None, "odd": "An odd number", "names": "SKU names"}
    # A union's members are each a line about the argument, not about the members' names.
    assert wrong_arguments("m", error.value.errors(), takes, given).splitlines() == [
        "either should be a valid integer, unable to parse string as an integer.",
        "either should be a valid list.",
        "odd is wrong: must be odd: an odd number.",
        # The app's own words stay its own, and a word in capitals keeps them.
        "names is wrong: Names should be unique: SKU names.",
    ]


def test_a_file_the_app_does_not_declare_is_refused(worker: Worker) -> None:
    refused = worker.run_action(OWNER, NOW, "stray", {}, FILES)
    assert isinstance(refused, Refusal)
    assert refused.message is not None and "other.txt" in refused.message


def test_apply_checks_a_change_made_elsewhere(worker: Worker) -> None:
    before = {"log.txt": b"a\nb\n"}
    shrunk = worker.apply(OWNER, "revert", before, {"log.txt": b"a\n"})
    assert isinstance(shrunk, Refusal)
    assert "append_only" in shrunk.invariants
    grown = worker.apply(OWNER, "revert", before, {"log.txt": b"a\nb\nc\n"})
    assert grown == Changes({"log.txt": b"a\nb\nc\n"})
    assert worker.apply(OWNER, "revert", before, dict(before)) == Changes({})


def test_reads_run_on_the_files_they_are_given(worker: Worker) -> None:
    assert worker.query_json(OWNER, NOW, "lines", {}, {"log.txt": b"a\nb\n"}, None) == ["a", "b"]
    assert worker.query_json(OWNER, NOW, "lines", {}, {"log.txt": b"a\nb\n"}, "x") == ["a", "b"]
    assert worker.query_json(OWNER, NOW, "lines", {}, {"log.txt": b"a\nb\n"}, "x") == ["a", "b"]
    assert worker.query_json(OWNER, NOW, "lines", {}, {"log.txt": b"c\n"}, "y") == ["c"]


@skip_as_root
def test_a_query_cannot_write(worker: Worker) -> None:
    with pytest.raises(AppFailed, match="PermissionError") as failed:
        worker.query_json(OWNER, NOW, "scribble", {}, {"log.txt": b"a\n"}, None)
    assert isinstance(failed.value.__cause__, PermissionError)


def test_check_reports_what_is_wrong(worker: Worker) -> None:
    checked = worker.check(NOW, {"log.txt": b"forbidden\n"})
    assert checked.refusal is not None
    assert "no_forbidden" in checked.refusal.invariants
    valid = worker.check(NOW, {"log.txt": b"start\n"})
    assert valid == Checked(None, [])


def test_undo_files() -> None:
    same = {"log.txt": b"a\n"}
    assert undo_files(same, {"log.txt": b"a\n"}, {"log.txt": b"a\n"}, "0123456789") == same
    already = {"log.txt": b"a\n"}
    assert undo_files(already, {"log.txt": b"a\n"}, {"log.txt": b"b\n"}, "0123456789") == already
    assert undo_files({"log.txt": b"b\n"}, {"log.txt": b"a\n"}, {"log.txt": b"b\n"}, "0123") == {
        "log.txt": b"a\n"
    }
    assert undo_files({"added.txt": b"b\n"}, {}, {"added.txt": b"b\n"}, "0123") == {}
    later = {"log.txt": b"1x\n2\n3\n4\n5x\n"}
    merged = undo_files(
        later,
        {"log.txt": b"1\n2\n3\n4\n5\n"},
        {"log.txt": b"1x\n2\n3\n4\n5\n"},
        "0123456789",
    )
    assert merged == {"log.txt": b"1\n2\n3\n4\n5x\n"}
    with pytest.raises(Refused, match="can't be undone"):
        undo_files({"added.txt": b"c\n"}, {}, {"added.txt": b"b\n"}, "0123")
    with pytest.raises(Refused, match="can't be undone"):
        undo_files(
            {"log.txt": b"1\n2\ny\n"},
            {"log.txt": b"1\n2\n3\n"},
            {"log.txt": b"1\n2\nx\n"},
            "0123",
        )


class FakeEngine:
    """An engine that runs nothing: it answers with a fixed change."""

    def __init__(self, manifest: dict[str, Any]) -> None:
        self.manifest = manifest

    def run_action(
        self,
        caller: Caller,
        now: datetime,
        action: str,
        arguments: dict[str, Any],
        files: dict[str, bytes],
        inputs: Mapping[str, Input] | None = None,
    ) -> Changes | Refusal:
        return Changes({"log.txt": b"from the engine\n"})

    def apply(
        self, caller: Caller, label: str, files: dict[str, bytes], result: dict[str, bytes]
    ) -> Changes | Refusal:
        raise NotImplementedError

    def query_json(
        self,
        caller: Caller,
        now: datetime,
        query: str,
        arguments: dict[str, Any],
        files: dict[str, bytes],
        state: str | None,
    ) -> Any:
        raise NotImplementedError

    def view(
        self,
        caller: Caller,
        now: datetime,
        view: str,
        arguments: dict[str, Any],
        files: dict[str, bytes],
        state: str | None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def check(self, now: datetime, files: dict[str, bytes]) -> Checked:
        raise NotImplementedError


def test_a_runtime_works_on_any_engine(tmp_path: Path) -> None:
    from test_runtime import make_app

    app_dir = tmp_path / "log"
    first = make_app(app_dir, LOG_APP, {"log.txt": "start\n"})
    module_name = f"never_imported_{next(_names)}"
    rt = Runtime(app_dir, engine=FakeEngine(first.manifest), module_name=module_name)
    outcome = rt.call(OWNER, "append", {"line": "x"})
    assert isinstance(outcome, Committed)
    assert (rt.store.data_dir / "log.txt").read_text() == "from the engine\n"
    assert module_name not in sys.modules
