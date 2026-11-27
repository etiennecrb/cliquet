from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.applications import Starlette

from cliquet_server.app import create_app
from cliquet_server.apps import Apps, Live
from cliquet_server.builder import Builder
from cliquet_server.db import Database
from cliquet_server.executor import LocalExecutor
from cliquet_server.settings import Settings

from .server_helpers import APPS, NOW, running


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings for a server whose data is in a temporary folder."""
    return Settings.from_env(
        {
            "CLIQUET_DATA_DIR": str(tmp_path / "cliquet-data"),
            "CLIQUET_DEV_LOGIN": "etienne",
        }
    )


@pytest.fixture
def database(settings: Settings) -> Iterator[Database]:
    """An empty database, in the temporary data folder."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    database = Database(settings.database_url)
    yield database
    database.close()


@pytest.fixture(scope="session")
def executor(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LocalExecutor]:
    """One local sandbox for the whole run, so that each version is built once."""
    executor = LocalExecutor(tmp_path_factory.mktemp("sandbox"))
    yield executor
    executor.close()


@pytest.fixture
def apps(settings: Settings, database: Database, executor: LocalExecutor) -> Iterator[Apps]:
    """The server's apps, on the shared local sandbox."""
    apps = Apps(settings, database, executor, clock=lambda: NOW)
    yield apps
    apps.close()


@pytest.fixture
def builder(apps: Apps, settings: Settings) -> Builder:
    """The builder's tools as plain methods."""
    return Builder(apps, settings)


@pytest.fixture
def shop(apps: Apps) -> Live:
    """The stock example, imported as etienne/shop."""
    return apps.import_folder(APPS / "stock", "etienne", "shop")


@pytest.fixture
def server(settings: Settings, executor: LocalExecutor) -> Starlette:
    """The server's app, with the stock example imported as etienne/shop."""
    app = create_app(settings, executor=executor, clock=lambda: NOW)
    app.state.apps.import_folder(APPS / "stock", "etienne", "shop", email="etienne@example.com")
    # A page waits for a start as long as a call, so that a test that starts the app sees it
    # started, however busy the machine: the tests of a page's own wait set theirs.
    app.state.apps.page_wait = app.state.apps.start_wait
    return app


@pytest.fixture
def address(server: Starlette) -> Iterator[str]:
    """The address of the server, running for the length of a test."""
    with running(server) as where:
        yield where


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Keep the heavy, timing-sensitive tests on one worker.

    Browsers and real sandboxes compete for CPU and make scripted screens flaky when
    every worker starts one at once. Light tests still use all workers.
    """
    for item in items:
        if item.get_closest_marker("browser") or item.get_closest_marker("slow"):
            item.add_marker(pytest.mark.xdist_group("heavy"))
