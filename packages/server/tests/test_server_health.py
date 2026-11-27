"""`/healthz` answers 503, naming the failing part, when the database or its disk fails."""

import logging
import os
import threading
from collections.abc import Callable, Iterator
from types import SimpleNamespace

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from cliquet_server.app import create_app
from cliquet_server.db import Database
from cliquet_server.health import MIN_FREE, Health
from cliquet_server.settings import Settings


@pytest.fixture
def app(settings: Settings) -> Starlette:
    """The server, with no app imported."""
    return create_app(settings)


@pytest.fixture
def client(app: Starlette) -> Iterator[TestClient]:
    """A client of the server, without its lifespan: the check needs no warm-up."""
    yield TestClient(app)


def health(app: Starlette) -> Health:
    """The server's health checks."""
    return app.state.health


def failing(answer: httpx2.Response) -> list[str]:
    """The parts a 503 names, after checking the rest of its body, which holds no error."""
    assert answer.status_code == 503
    body = answer.json()
    assert body["ok"] is False
    assert body["version"] == "0.0.1"
    assert set(body) == {"ok", "version", "failing"}
    return body["failing"]


def test_a_corrupt_database_fails_the_check(
    app: Starlette, client: TestClient, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    path = settings.data_dir / "server.db"
    size = path.stat().st_size
    with path.open("r+b") as file:
        file.write(b"not a database " * (size // 15 + 1))
    with caplog.at_level(logging.WARNING, logger="cliquet_server"):
        answer = client.get("/healthz")
    assert failing(answer) == ["database"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "database" in warnings[0].getMessage()
    assert not warnings[0].getMessage().startswith("unexpected:")


def test_a_database_that_hangs_fails_the_check_at_its_deadline(
    app: Starlette, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    released = threading.Event()
    calls: list[None] = []

    def hung(self: Database) -> None:
        calls.append(None)
        released.wait(30)

    monkeypatch.setattr(Database, "ping", hung)
    health(app).deadline = 0.05
    clock = [0.0]
    health(app).monotonic = lambda: clock[0]
    try:
        assert failing(client.get("/healthz")) == ["database"]
        # Past the window, the hung ping still holds its thread: no other one starts.
        clock[0] += 10
        assert failing(client.get("/healthz")) == ["database"]
        assert len(calls) == 1
    finally:
        released.set()


def statvfs(flag: int = 0, free_blocks: int = 1 << 20) -> Callable[[object], SimpleNamespace]:
    """A fake `os.statvfs`, with the mount flags and free blocks given."""
    return lambda path: SimpleNamespace(f_flag=flag, f_bavail=free_blocks, f_frsize=4096)


def test_a_read_only_disk_fails_the_check(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "statvfs", statvfs(flag=os.ST_RDONLY))
    assert failing(client.get("/healthz")) == ["disk"]


def test_a_full_disk_fails_the_check(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "statvfs", statvfs(free_blocks=0))
    assert failing(client.get("/healthz")) == ["disk"]
    assert MIN_FREE == 10 * 1024 * 1024


def test_a_result_is_reused_for_five_seconds(
    app: Starlette, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [100.0]
    health(app).monotonic = lambda: clock[0]
    assert client.get("/healthz").status_code == 200
    monkeypatch.setattr(os, "statvfs", statvfs(free_blocks=0))
    clock[0] += 4.9
    assert client.get("/healthz").status_code == 200
    clock[0] += 0.1
    assert failing(client.get("/healthz")) == ["disk"]
