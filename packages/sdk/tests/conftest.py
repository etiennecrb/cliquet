import base64
import itertools
import logging
import tempfile
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

# Whole apps the tests run, each with its own tests: those run in the app's environment.
collect_ignore = ["apps"]
APPS = Path(__file__).parent / "apps"
# The examples agents read, at the repository's top.
EXAMPLES = Path(__file__).parents[3] / "examples"

HEADER = """\
# /// script
# dependencies = ["cliquet-sdk>=0.0.1,<0.1"]
# ///
"""

_names = itertools.count()

type WriteApp = Callable[..., Path]


@pytest.fixture
def write_app(tmp_path: Path) -> WriteApp:
    """Write an app module to a temporary file and return its path."""

    def write(body: str, header: str = HEADER) -> Path:
        path = tmp_path / f"app_{next(_names)}.py"
        path.write_text(header + textwrap.dedent(body))
        return path

    return write


def module_name(path: Path) -> str:
    """A module name unique to this test app, so test apps never share a module."""
    return f"cliquet_test_{path.stem}"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Keep the heavy, timing-sensitive tests on one worker.

    Browsers and real sandboxes compete for CPU and make scripted screens flaky when
    every worker starts one at once. Light tests still use all workers.
    """
    for item in items:
        if item.get_closest_marker("browser") or item.get_closest_marker("slow"):
            item.add_marker(pytest.mark.xdist_group("heavy"))


# Putting a version as the server does: lock it, list its wheels, send the missing ones.
_WHEELS: list[Any] = []


def wheels_for(client: Any, files: dict[str, bytes]) -> Any:
    """The lock and wheels of `files`, for the platform of the sandbox behind `client`."""
    from cliquet.wheels import Wheels

    if not _WHEELS:
        _WHEELS.append(Wheels(Path(tempfile.gettempdir()) / "cliquet-test-wheels"))
    health = client.get("/health").json()
    return _WHEELS[0], _WHEELS[0].lock(files, health["python"], health["platform"])


def put_version(
    client: Any, login: str, app: str, files: dict[str, bytes], version: str | None = None
) -> Any:
    """Put a version on a sandbox, with the wheels it needs, as the server does."""
    from cliquet.sandbox import version_id

    wheels, locked = wheels_for(client, files)
    sent = {**files, "app.py.lock": locked.lock}
    body = {
        "files": {name: base64.b64encode(content).decode() for name, content in sent.items()},
        "wheels": [{"name": wheel.name, "sha256": wheel.sha256} for wheel in locked.wheels],
    }
    path = f"/apps/{login}/{app}/versions/{version or version_id(files)}"
    answer = client.put(path, json=body)
    if answer.status_code == 409:
        by_name = {wheel.name: wheel for wheel in locked.wheels}
        for name in answer.json()["missing"]:
            wheel = by_name[name]
            sent_wheel = client.put(
                f"/wheels/{name}",
                params={"sha256": wheel.sha256},
                content=wheels.path(wheel).read_bytes(),
            )
            assert sent_wheel.status_code == 200, sent_wheel.text
        answer = client.put(path, json=body)
    return answer


def unexpected(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The records that log a failure of Cliquet's own: at ERROR, `unexpected: ` first."""
    return [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and record.getMessage().startswith("unexpected: ")
    ]


def one_unexpected(
    caplog: pytest.LogCaptureFixture, exc_type: type[BaseException] | None = None
) -> logging.LogRecord:
    """The one record that logs a failure of Cliquet's own, with its traceback: of an
    `exc_type`, when given."""
    (record,) = unexpected(caplog)
    assert record.exc_info and record.exc_info[1] is not None
    assert exc_type is None or isinstance(record.exc_info[1], exc_type)
    return record
