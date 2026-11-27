from pathlib import Path

import pytest
from starlette.testclient import TestClient

from cliquet_server.app import create_app
from cliquet_server.executor import LocalExecutor
from cliquet_server.settings import Settings, SettingsError

from .server_helpers import NOW, signed_in

NOTICE = "https://cliquet.example/privacy"
LINK = f'href="{NOTICE}" rel="privacy-policy">Privacy notice</a>'


def _pages(tmp_path: Path, executor: LocalExecutor, **env: str) -> tuple[str, str]:
    """A missing page, signed out, and the signed-in home page."""
    settings = Settings.from_env(
        {"CLIQUET_DATA_DIR": str(tmp_path / "data"), "CLIQUET_DEV_LOGIN": "etienne", **env}
    )
    server = create_app(settings, executor=executor, clock=lambda: NOW)
    with TestClient(server) as client:
        missing = client.get("/nowhere").text
    with signed_in(server) as client:
        home = client.get("/").text
    return missing, home


def test_every_page_links_to_the_operator_s_privacy_notice(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    missing, home = _pages(tmp_path, executor, CLIQUET_PRIVACY_URL=NOTICE)
    assert LINK in missing
    assert "Sign out" in home and LINK in home


def test_a_server_without_a_privacy_notice_links_to_none(
    tmp_path: Path, executor: LocalExecutor
) -> None:
    for page in _pages(tmp_path, executor):
        assert "privacy-link" not in page


def test_the_privacy_notice_is_an_address() -> None:
    with pytest.raises(SettingsError, match="CLIQUET_PRIVACY_URL"):
        Settings.from_env({"CLIQUET_PRIVACY_URL": "privacy.html"})
    with pytest.raises(SettingsError, match="CLIQUET_PRIVACY_URL"):
        Settings.from_env({"CLIQUET_PRIVACY_URL": "javascript:alert(1)"})
