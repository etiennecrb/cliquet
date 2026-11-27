import inspect
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    WrapSerializer,
    computed_field,
)

from cliquet.checks import (
    check_clock,
    check_environment,
    check_randomness,
    check_result,
    check_tests,
)


@pytest.mark.parametrize(
    "source",
    [
        "import datetime\ndatetime.datetime.now()",
        "import datetime as dt\ndt.date.today()",
        "from datetime import datetime\ndatetime.now()",
        "from datetime import datetime as moment\nmoment.utcnow()",
        "from datetime import date\ndate.today()",
        "import time\ntime.time()",
        "from time import time_ns\ntime_ns()",
        "import time\ntime.localtime()",
        "def helper():\n    from datetime import datetime\n    return datetime.now()",
    ],
)
def test_rejects_reading_the_clock(source: str) -> None:
    problems = check_clock(source)
    assert len(problems) == 1
    assert "ctx.now" in problems[0].message


@pytest.mark.parametrize(
    "source",
    [
        "from datetime import date\ndate(2026, 11, 10)",
        "from datetime import datetime\ndatetime.fromisoformat('2026-11-10')",
        "import time\ntime.localtime(0)",
        "def f(ctx):\n    return ctx.now.date()",
        "class Clock:\n    def now(self): ...\nClock().now()",
    ],
)
def test_allows_other_uses_of_dates_and_times(source: str) -> None:
    assert check_clock(source) == []


def test_reports_the_line() -> None:
    (problem,) = check_clock("import time\n\n\nx = time.time()\n", "shop.py")
    assert problem.where == "shop.py:4"


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("import random\nrandom.random()", "import random isn't allowed"),
        ("from random import choice", "import random isn't allowed"),
        ("import secrets", "import secrets isn't allowed"),
        ("import uuid\nuuid.uuid4()", "uuid.uuid4 is random"),
        ("from uuid import uuid1 as new\nnew()", "uuid.uuid1 is random"),
        ("import os\nos.urandom(8)", "os.urandom is random"),
        ("import os.path\nos.urandom(8)", "os.urandom is random"),
        ("from uuid import uuid7\nuuid7()", "uuid.uuid7 is random"),
        ("import os\nos.getrandom(8)", "os.getrandom is random"),
        ("from pydantic import Field\nimport uuid\nField(default_factory=uuid.uuid4)", "uuid4"),
        ("def helper():\n    import random\n    return random.random()", "import random"),
    ],
)
def test_rejects_randomness(source: str, found: str) -> None:
    (problem,) = check_randomness(source)
    assert found in problem.message
    assert "the same data and arguments must give the same result" in problem.message


@pytest.mark.parametrize(
    "source",
    [
        "import uuid\nuuid.uuid5(uuid.NAMESPACE_URL, 'x')",
        "import uuid\nuuid.UUID('12345678123456781234567812345678')",
        "import os\nos.path.join('a', 'b')",
        "from hashlib import sha256\nsha256(b'x').hexdigest()",
    ],
)
def test_allows_what_is_not_random(source: str) -> None:
    assert check_randomness(source) == []


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("import subprocess", "import subprocess isn't allowed"),
        ("from subprocess import run", "import subprocess isn't allowed"),
        ("import os\nos.system('ls')", "os.system runs a program"),
        ("from os import execvp\nexecvp('ls', ['ls'])", "os.execvp runs a program"),
        ("import os\nos.popen('ls')", "os.popen runs a program"),
        ("import os.path\nos.system('ls')", "os.system runs a program"),
        ("import asyncio\nasyncio.create_subprocess_exec('ls')", "runs a program"),
        (
            "from asyncio.subprocess import create_subprocess_exec\ncreate_subprocess_exec('ls')",
            "runs a program",
        ),
        ("import asyncio.subprocess\nasyncio.subprocess.create_subprocess_shell('ls')", "runs"),
        ("from asyncio import subprocess\nsubprocess.create_subprocess_exec('ls')", "runs"),
        ("import asyncio.subprocess as sub\nsub.create_subprocess_exec('ls')", "runs"),
        ("import pty\npty.spawn('ls')", "pty.spawn runs a program"),
        ("import site\nsite.addsitedir('lib')", "changes sys.path"),
        ("import sys\nsys.path.insert(0, 'lib')", "changes sys.path"),
        ("import sys\nsys.path.append('lib')", "changes sys.path"),
        ("import sys\nsys.path = ['lib']", "changes sys.path"),
        ("import sys\nsys.path += ['lib']", "changes sys.path"),
        ("import sys\nsys.path[0] = 'lib'", "changes sys.path"),
        ("from sys import path\npath.append('lib')", "changes sys.path"),
    ],
)
def test_rejects_what_depends_on_the_machine(source: str, found: str) -> None:
    problems = check_environment(source)
    assert [found in problem.message for problem in problems] == [True]


@pytest.mark.parametrize(
    "source",
    [
        "import sys\nprint(sys.path[0])",
        "import os\nos.environ.get('X')",
        "import sys\nsys.exit(0)",
        "items = []\nitems.append(1)",
    ],
)
def test_allows_what_does_not_depend_on_the_machine(source: str) -> None:
    assert check_environment(source) == []


def test_reports_the_line_of_randomness_and_programs() -> None:
    (problem,) = check_randomness("x = 1\n\nimport random\n", "shop.py")
    assert problem.where == "shop.py:3"
    (problem,) = check_environment("import sys\n\nsys.path.append('x')\n", "shop.py")
    assert problem.where == "shop.py:3"


def write_test(tmp_path: Path, text: str) -> Path:
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_shop.py").write_text(text)
    return tmp_path


@pytest.mark.parametrize(
    "text",
    [
        "open('data/stock.csv').read()",
        "from pathlib import Path\nPath('data/stock.csv').read_text()",
        "from pathlib import Path\n(Path(__file__).parent.parent / 'data' / 'stock.csv')",
        "from pathlib import Path\nPath(__file__).parents[1].joinpath('data', 'stock.csv')",
        "import os\nos.path.join('..', 'data', 'stock.csv')",
        "open('./data/stock.csv')",
        "open('../data/stock.csv')",
        "name = 'stock.csv'\nopen(f'data/{name}')",
        "FIRST = 'data/'",
    ],
)
def test_rejects_a_test_that_reads_data(tmp_path: Path, text: str) -> None:
    (problem,) = check_tests(write_test(tmp_path, text))
    assert problem.where.startswith("tests/test_shop.py:")
    assert "data/" in problem.message
    assert "given(app, {file: text})" in problem.message


@pytest.mark.parametrize(
    "text",
    [
        "from cliquet.testing import given\nworld = given(app, {'stock.csv': 'sku\\n'})",
        "payload = {'data': [1, 2]}",
        "assert result.data == 'x'",
        '"""Tests never read data/stock.csv."""',
        'def test_x() -> None:\n    """Not from data/."""',
        "open('metadata/x.csv')",
    ],
)
def test_allows_a_test_that_states_its_data(tmp_path: Path, text: str) -> None:
    assert check_tests(write_test(tmp_path, text)) == []


def test_reports_the_test_s_line(tmp_path: Path) -> None:
    (problem,) = check_tests(write_test(tmp_path, "x = 1\n\nopen('data/stock.csv')\n"))
    assert problem.where == "tests/test_shop.py:3"


def test_an_app_without_tests_has_no_test_problems(tmp_path: Path) -> None:
    assert check_tests(tmp_path) == []


@dataclass
class Node:
    name: str
    children: list[Node]
    total: Total


class Total(BaseModel):
    amount: int = Field(serialization_alias="sum")


class ByName(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    amount: int = Field(alias="sum")


def test_a_result_is_checked_at_any_depth_once() -> None:
    (problem,) = check_result(list[Node])
    assert problem.message.startswith("Total.amount has serialization_alias='sum'")
    assert problem.where == f"test_checks.py:{inspect.getsourcelines(Total)[1] + 1}"


def test_an_alias_a_model_doesn_t_read_by_is_refused() -> None:
    (problem,) = check_result(ByName)
    assert problem.message.startswith("ByName.amount has serialization_alias='sum'")


class Shown(BaseModel):
    day: Annotated[date, PlainSerializer(lambda day: day.strftime("%d/%m/%Y"), return_type=str)]
    tags: list[Annotated[str, WrapSerializer(lambda value, handler: handler(value).upper())]]
    secret: str = Field("", exclude=True)
    hidden: str = Field("", exclude_if=lambda value: not value)
    note: str = ""


def test_a_serializer_in_an_annotation_and_an_excluded_field_are_refused() -> None:
    problems = check_result(Shown)
    assert [problem.message.split(",")[0] for problem in problems] == [
        "Shown.day has a PlainSerializer",
        "Shown.tags has a WrapSerializer",
        "Shown.secret is excluded from the result's JSON",
        "Shown.hidden is excluded from the result's JSON",
    ]
    start = inspect.getsourcelines(Shown)[1]
    assert [problem.where for problem in problems] == [
        f"test_checks.py:{start + line}" for line in (1, 2, 3, 4)
    ]


@dataclass
class Row:
    when: Annotated[date, PlainSerializer(str)]


def test_a_serializer_around_the_result_or_in_a_dataclass_is_refused() -> None:
    (around,) = check_result(Annotated[list[str], WrapSerializer(lambda v, h: h(v))])
    assert around.message.startswith("The result's type has a WrapSerializer")
    (inside,) = check_result(list[Row])
    assert inside.message.startswith("The result's type has a PlainSerializer")


def test_a_test_may_name_a_data_file_the_app_declares(tmp_path: Path) -> None:
    folder = write_test(tmp_path, "world = given(app, {'data/log.csv': 'a\\n'})")
    assert check_tests(folder, ["data/log.csv"]) == []
    assert len(check_tests(folder)) == 1


def test_a_test_may_name_a_data_file_a_pattern_in_data_declares(tmp_path: Path) -> None:
    folder = write_test(tmp_path, "world = given(app, {'data/2026/01.csv': 'a\\n'})")
    assert check_tests(folder, ["data/**/*.csv"]) == []
    assert check_tests(folder, ["data/2026/*.csv"]) == []
    # A pattern that matches data/ only by its wildcard still keeps tests off the live data.
    assert len(check_tests(folder, ["**/*.csv"])) == 1
    assert len(check_tests(folder, ["data/*.csv"])) == 1


def test_an_edited_test_is_checked_again(tmp_path: Path) -> None:
    folder = write_test(tmp_path, "open('data/a.csv')")
    assert len(check_tests(folder)) == 1
    # The same size, at once: the test's text says it changed.
    write_test(tmp_path, "open('dbta/a.csv')")
    assert check_tests(folder) == []


class Totalled(BaseModel):
    @computed_field
    @property
    def total(self) -> int:
        return 1


type Totals = list[Totalled]
type Each[T] = list[T]


def test_a_result_is_checked_behind_a_type_alias() -> None:
    (problem,) = check_result(Totals)
    assert problem.message.startswith("Totalled.total is a computed_field")
    (generic,) = check_result(Each[Totalled])
    assert generic.message.startswith("Totalled.total is a computed_field")
