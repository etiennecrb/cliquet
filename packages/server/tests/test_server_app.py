import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from cliquet_server.app import create_app
from cliquet_server.settings import Settings, SettingsError


def every_variable(data_dir: Path) -> dict[str, str]:
    """Every variable the settings read, each set to a value that differs from its default."""
    return {
        "CLIQUET_DATA_DIR": str(data_dir),
        "CLIQUET_BASE_URL": "https://cliquet.example.com/",
        "CLIQUET_WEBSITE_URL": "https://cliquet.dev/",
        "CLIQUET_DATABASE_URL": "sqlite:///elsewhere.db",
        "CLIQUET_EXECUTOR": "sprites",
        "CLIQUET_AUTH": "oidc",
        "CLIQUET_DEV_LOGIN": "",
        "CLIQUET_ALLOW_REMOTE_DEV_LOGIN": "0",
        "CLIQUET_OIDC_ISSUER": "https://example.eu.auth0.com/",
        "CLIQUET_OIDC_CLIENT_ID": "the-client-id",
        "CLIQUET_OIDC_CLIENT_SECRET": "the-client-secret",
        "CLIQUET_OIDC_AUDIENCE": "https://api.example.com",
        "CLIQUET_SECRET_KEY": "a-secret-key-that-is-at-least-32-characters",
        "CLIQUET_SPRITES_TOKEN": "the-sprites-token",
        "CLIQUET_SPRITES_PREFIX": "test-",
        "CLIQUET_SANDBOX_IMAGE": "example-sandbox:v1",
        "CLIQUET_SANDBOX_PREFIX": "test-",
        "CLIQUET_SANDBOX_SOCKETS": str(data_dir / "socks"),
        "CLIQUET_SANDBOX_SOCKETS_HOST": "/var/lib/socks",
        "CLIQUET_SANDBOX_IDLE_SECONDS": "60",
        "CLIQUET_SANDBOX_MEMORY_MB": "4096",
        "CLIQUET_DOCKER_SOCKET": "/run/docker.sock",
        "CLIQUET_WARM_DAYS": "30",
    }


def test_settings_have_defaults() -> None:
    settings = Settings.from_env({})
    assert settings.data_dir == Path("cliquet-data").resolve()
    assert settings.data_dir.is_absolute()
    assert settings.base_url == "http://127.0.0.1:8800"
    # The site is the server's own address until a deployment sets it apart.
    assert settings.website_url == "http://127.0.0.1:8800"
    assert settings.database_url == f"sqlite:///{settings.data_dir}/server.db"
    assert settings.executor == "local"
    assert settings.auth == "none"
    assert settings.dev_login is None
    assert settings.allow_remote_dev_login is False
    assert settings.oidc_issuer is None
    assert settings.oidc_client_secret is None
    assert settings.oidc_audience is None
    assert settings.secret_key is None
    assert settings.sprites_token is None
    assert settings.sprites_prefix == "cliquet-"
    assert settings.sandbox_image == "cliquet-sandbox:latest"
    assert settings.sandbox_prefix == "cliquet-"
    assert settings.sandbox_sockets == settings.data_dir / "sockets"
    assert settings.sandbox_sockets_host == settings.data_dir / "sockets"
    assert settings.sandbox_sockets_volume is None
    assert settings.sandbox_idle_seconds == 900
    assert settings.sandbox_memory_mb == 2048
    assert settings.docker_socket == "/var/run/docker.sock"
    assert settings.warm_days == 7


def test_settings_read_the_environment(tmp_path: Path) -> None:
    settings = Settings.from_env(every_variable(tmp_path / "data"))
    assert settings.data_dir == (tmp_path / "data").resolve()
    assert settings.base_url == "https://cliquet.example.com"
    assert settings.website_url == "https://cliquet.dev"
    assert settings.database_url == "sqlite:///elsewhere.db"
    assert settings.executor == "sprites"
    assert settings.auth == "oidc"
    assert settings.dev_login is None
    assert settings.allow_remote_dev_login is False
    assert settings.oidc_issuer == "https://example.eu.auth0.com/"
    assert settings.oidc_client_id == "the-client-id"
    assert settings.oidc_client_secret is not None
    assert settings.oidc_client_secret.get_secret_value() == "the-client-secret"
    assert settings.oidc_audience == "https://api.example.com"
    assert settings.secret_key is not None
    assert settings.secret_key.get_secret_value() == "a-secret-key-that-is-at-least-32-characters"
    assert settings.sprites_token is not None
    assert settings.sprites_token.get_secret_value() == "the-sprites-token"
    assert settings.sprites_prefix == "test-"
    assert settings.sandbox_image == "example-sandbox:v1"
    assert settings.sandbox_prefix == "test-"
    assert settings.sandbox_sockets == (tmp_path / "data" / "socks").resolve()
    assert settings.sandbox_sockets_host == Path("/var/lib/socks")
    assert settings.sandbox_idle_seconds == 60
    assert settings.sandbox_memory_mb == 4096
    assert settings.docker_socket == "/run/docker.sock"
    assert settings.warm_days == 30


def test_the_website_url_follows_the_base_url_until_it_is_set() -> None:
    remote = {"CLIQUET_ALLOW_REMOTE_LOCAL_EXECUTOR": "1"}
    follows = Settings.from_env({"CLIQUET_BASE_URL": "https://cliquet.example/", **remote})
    assert follows.website_url == "https://cliquet.example"
    apart = Settings.from_env(
        {
            "CLIQUET_BASE_URL": "https://cliquet.example",
            "CLIQUET_WEBSITE_URL": "https://x.dev/",
            **remote,
        }
    )
    assert apart.website_url == "https://x.dev"
    # A site that is only a slash falls back too, rather than naming nothing.
    empty = Settings.from_env({"CLIQUET_WEBSITE_URL": "/", **remote})
    assert empty.website_url == "http://127.0.0.1:8800"


def test_settings_refuse_an_unknown_executor() -> None:
    with pytest.raises(SettingsError, match="CLIQUET_EXECUTOR"):
        Settings.from_env({"CLIQUET_EXECUTOR": "podman"})


def test_the_sockets_volume_is_a_docker_volume_s_name() -> None:
    settings = Settings.from_env({"CLIQUET_SANDBOX_SOCKETS_VOLUME": "cliquet_sockets"})
    assert settings.sandbox_sockets_volume == "cliquet_sockets"
    for wrong in ("/srv/sockets", "-sockets", "a", "two words"):
        with pytest.raises(SettingsError, match="CLIQUET_SANDBOX_SOCKETS_VOLUME"):
            Settings.from_env({"CLIQUET_SANDBOX_SOCKETS_VOLUME": wrong})
    # The volume is for a server in a container, the host's folder for one outside it.
    both = {"CLIQUET_SANDBOX_SOCKETS_VOLUME": "cliquet_sockets"}
    with pytest.raises(SettingsError, match="cannot be used together"):
        Settings.from_env({**both, "CLIQUET_SANDBOX_SOCKETS_HOST": "/srv/sockets"})


def test_settings_warm_up_between_a_day_and_ten_years() -> None:
    with pytest.raises(SettingsError, match="CLIQUET_WARM_DAYS"):
        Settings.from_env({"CLIQUET_WARM_DAYS": "0"})
    with pytest.raises(SettingsError, match="CLIQUET_WARM_DAYS"):
        Settings.from_env({"CLIQUET_WARM_DAYS": "1000000000"})


def test_the_docker_executor_is_allowed_on_any_address(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {"CLIQUET_EXECUTOR": "docker", "CLIQUET_BASE_URL": "https://cliquet.example.com"}
    )
    assert settings.executor == "docker"
    assert settings.sandbox_image == "cliquet-sandbox:latest"
    assert settings.sandbox_prefix == "cliquet-"
    assert settings.docker_socket == "/var/run/docker.sock"
    assert settings.sandbox_sockets == settings.data_dir / "sockets"
    assert settings.sandbox_sockets_host == settings.sandbox_sockets
    outside = Settings.from_env(
        {
            "CLIQUET_DATA_DIR": str(tmp_path / "data"),
            "CLIQUET_EXECUTOR": "docker",
            "CLIQUET_SANDBOX_IMAGE": "mine:1",
            "CLIQUET_SANDBOX_PREFIX": "box-",
            "CLIQUET_SANDBOX_SOCKETS": str(tmp_path / "socks"),
            "CLIQUET_SANDBOX_SOCKETS_HOST": "/srv/socks",
            "CLIQUET_DOCKER_SOCKET": "/run/docker.sock",
        }
    )
    assert outside.sandbox_image == "mine:1"
    assert outside.sandbox_prefix == "box-"
    assert outside.sandbox_sockets == (tmp_path / "socks").resolve()
    assert outside.sandbox_sockets_host == Path("/srv/socks")
    assert outside.docker_socket == "/run/docker.sock"


def test_a_public_local_executor_is_refused() -> None:
    public = {"CLIQUET_BASE_URL": "https://cliquet.example.com"}
    for executor in ({}, {"CLIQUET_EXECUTOR": "local"}):
        with pytest.raises(SettingsError, match="CLIQUET_EXECUTOR=sprites"):
            Settings.from_env({**public, **executor})
    allowed = Settings.from_env({**public, "CLIQUET_ALLOW_REMOTE_LOCAL_EXECUTOR": "1"})
    assert allowed.executor == "local"
    assert allowed.allow_remote_local_executor is True
    assert Settings.from_env({}).allow_remote_local_executor is False
    with pytest.raises(SettingsError, match="CLIQUET_ALLOW_REMOTE_LOCAL_EXECUTOR"):
        Settings.from_env({"CLIQUET_ALLOW_REMOTE_LOCAL_EXECUTOR": "maybe"})


def test_healthz(settings: Settings) -> None:
    response = TestClient(create_app(settings)).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "version": "0.0.1"}


def test_the_data_folder_is_created(settings: Settings) -> None:
    assert not settings.data_dir.exists()
    create_app(settings)
    assert settings.data_dir.is_dir()


def test_the_command_prints_its_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cliquet_server.cli", "--version"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "cliquet-server 0.0.1"


def test_the_shared_executor_outlives_the_app(settings: Settings, executor) -> None:
    app = create_app(settings, executor=executor)
    assert app.state.apps is not None
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    assert executor.sandbox("etienne").health()["ok"] is True


def test_settings_read_the_os_environment(monkeypatch) -> None:
    monkeypatch.setenv("CLIQUET_DEV_LOGIN", "Etienne")
    settings = Settings.from_env()
    assert settings.auth == "dev"
    assert settings.dev_login == "etienne"


def test_an_empty_variable_is_unset(monkeypatch) -> None:
    for name in ("CLIQUET_SECRET_KEY", "CLIQUET_DEV_LOGIN", "CLIQUET_SANDBOX_IDLE_SECONDS"):
        monkeypatch.setenv(name, "")
    settings = Settings.from_env()
    # An empty key would sign the session cookies with nothing.
    assert settings.secret_key is None
    assert settings.auth == "none"
    assert settings.sandbox_idle_seconds == 900


def test_settings_refuse_old_names(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "CLIQUET_DATA_DIR": str(tmp_path / "data"),
            "GOOGLE_CLIENT_ID": "old-client-id",
            "GOOGLE_CLIENT_SECRET": "old-client-secret",
            "SPRITES_TOKEN": "old-sprites-token",
        }
    )
    assert settings.auth == "none"
    assert settings.oidc_issuer is None
    assert settings.sprites_token is None


def test_auth_modes(tmp_path: Path) -> None:
    base = {"CLIQUET_DATA_DIR": str(tmp_path / "data")}
    assert Settings.from_env({**base, "CLIQUET_DEV_LOGIN": "etienne"}).auth == "dev"
    assert (
        Settings.from_env(
            {
                **base,
                "CLIQUET_OIDC_ISSUER": "https://example.eu.auth0.com/",
                "CLIQUET_OIDC_CLIENT_ID": "id",
                "CLIQUET_OIDC_CLIENT_SECRET": "secret",
                "CLIQUET_SECRET_KEY": "a-secret-key-that-is-at-least-32-characters",
            }
        ).auth
        == "oidc"
    )
    assert Settings.from_env(base).auth == "none"
    with pytest.raises(SettingsError, match="CLIQUET_AUTH"):
        Settings.from_env({**base, "CLIQUET_AUTH": "magic"})


def test_two_auth_providers_are_refused(tmp_path: Path) -> None:
    base = {"CLIQUET_DATA_DIR": str(tmp_path / "data")}
    with pytest.raises(SettingsError, match="CLIQUET_AUTH"):
        Settings.from_env(
            {
                **base,
                "CLIQUET_DEV_LOGIN": "etienne",
                "CLIQUET_OIDC_ISSUER": "https://example.eu.auth0.com/",
            }
        )


def test_the_provider_s_settings_are_all_or_nothing(tmp_path: Path) -> None:
    base = {"CLIQUET_DATA_DIR": str(tmp_path / "data")}
    for given in (
        {"CLIQUET_AUTH": "oidc"},
        {"CLIQUET_OIDC_AUDIENCE": "https://api.example.com"},
        {"CLIQUET_AUTH": "oidc", "CLIQUET_OIDC_ISSUER": "https://example.eu.auth0.com/"},
    ):
        with pytest.raises(SettingsError) as error:
            Settings.from_env({**base, **given})
        assert "CLIQUET_OIDC_CLIENT_ID" in str(error.value)
        assert "CLIQUET_OIDC_CLIENT_SECRET" in str(error.value)
        assert "CLIQUET_SECRET_KEY" in str(error.value)


def test_secrets_are_hidden() -> None:
    settings = Settings.from_env(
        {
            "CLIQUET_AUTH": "oidc",
            "CLIQUET_OIDC_ISSUER": "https://example.eu.auth0.com/",
            "CLIQUET_OIDC_CLIENT_ID": "id",
            "CLIQUET_OIDC_CLIENT_SECRET": "the-provider-secret",
            "CLIQUET_SECRET_KEY": "a-secret-key-that-is-at-least-32-characters",
        }
    )
    assert "the-provider-secret" not in repr(settings)
    assert "a-secret-key-that-is-at-least-32-characters" not in repr(settings)
