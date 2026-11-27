"""The server's configuration, read once from the environment."""

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlparse

from pydantic import (
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from cliquet_server.identity import login_problem

AuthMode = Literal["dev", "oidc", "none"]
# Where the server answers when nothing says: on this machine, for a first look at it.
DEFAULT_BASE_URL = "http://127.0.0.1:8800"
# What a sandbox is named by default, and the image a Docker sandbox is made from.
DEFAULT_PREFIX = "cliquet-"
DEFAULT_SANDBOX_IMAGE = "cliquet-sandbox:latest"
# How long a sandbox container may go unused before it is stopped; 0 never stops it.
DEFAULT_SANDBOX_IDLE_SECONDS = 900
# The memory a sandbox container may use, its service, its builds and Chromium included.
DEFAULT_SANDBOX_MEMORY_MB = 2048
# How many days after its owner last used their apps a sandbox is still warmed at boot.
DEFAULT_WARM_DAYS = 7
# How many days a new person whose email isn't verified uses Cliquet before it waits for them
# to verify it.
DEFAULT_VERIFY_EMAIL_DAYS = 7
# What stands for the person's subject in the call that sends their verification email again.
SUBJECT = "{subject}"
# The parameters Cliquet sets in every authorization request, which no setting may change.
AUTHORIZE_PARAMS = frozenset(
    (
        "response_type",
        "client_id",
        "redirect_uri",
        "scope",
        "state",
        "code_challenge",
        "code_challenge_method",
    )
)
# A name Docker takes for a volume.
_VOLUME_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+")


class SettingsError(ValueError):
    """The server's configuration is wrong. The message says which variable, and why."""


def _clean_url(value: str, fallback: str) -> str:
    """One of the server's addresses: `fallback` when it is empty, with no trailing slash."""
    return (value or fallback).rstrip("/") or fallback


class Settings(BaseSettings):
    """The server's settings. Secrets are masked when the object is printed."""

    model_config = SettingsConfigDict(
        env_prefix="CLIQUET_",
        # An empty variable is unset, as `from_env` takes a mapping's, so a file that lists a
        # setting with no value, as Docker Compose's does, keeps its default.
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
        validate_default=True,
    )

    data_dir: Path = Path("cliquet-data")
    base_url: str = DEFAULT_BASE_URL
    # Where Cliquet's website is, which an MCP client shows as the server's: the server's own
    # address unless the site is served apart from it.
    website_url: str = ""
    database_url: str = ""
    executor: Literal["local", "sprites", "docker"] = "local"
    auth: AuthMode | None = None
    dev_login: str | None = None
    allow_remote_dev_login: bool = False
    allow_remote_local_executor: bool = False
    # Whether every new person may build, from their sign-up, not only the server's first.
    grant_build_on_sign_up: bool = False
    secret_key: SecretStr | None = None
    sprites_token: SecretStr | None = None
    sprites_prefix: str = DEFAULT_PREFIX
    # The sandbox image, its containers' names, and where their sockets are: see `docker`.
    sandbox_image: str = DEFAULT_SANDBOX_IMAGE
    sandbox_prefix: str = DEFAULT_PREFIX
    sandbox_sockets: Path
    sandbox_sockets_host: Path
    sandbox_sockets_volume: str | None = None
    sandbox_idle_seconds: int = Field(default=DEFAULT_SANDBOX_IDLE_SECONDS, ge=0)
    sandbox_memory_mb: int = Field(default=DEFAULT_SANDBOX_MEMORY_MB, ge=256)
    docker_socket: str = "/var/run/docker.sock"
    warm_days: int = Field(default=DEFAULT_WARM_DAYS, ge=1, le=3650)
    # The OpenID Connect provider that signs people in, by its issuer, exactly as its tokens
    # name it, and the audience of the access tokens the person's endpoint takes: the
    # endpoint's own address unless it is set.
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_audience: str | None = None
    # How many days, from when they chose their login, a person has to verify their email.
    verify_email_days: int = Field(default=DEFAULT_VERIFY_EMAIL_DAYS, ge=1, le=3650)
    # The provider's own call that sends a person their verification email again, where
    # `{subject}` stands for the person's subject, and the audience of the token it takes:
    # see `verification_email`. Unset, the console offers no new email.
    verification_email_url: str | None = None
    verification_email_method: Literal["POST", "PUT", "PATCH"] = "POST"
    verification_email_body: str | None = None
    verification_email_audience: str | None = None
    # The parameters `/signup` adds to the provider's authorization request, so that it opens
    # on its sign-up form, such as Auth0's `screen_hint=signup`, read once from their query
    # string. Unset, `/signup` is `/login`. `NoDecode` hands the variable to the validator as it
    # is: pydantic-settings would read it as JSON, and refuse to start.
    sign_up_params: Annotated[tuple[tuple[str, str], ...], NoDecode] = ()
    # The operator's privacy notice: the console links to it, and shows no link without it.
    privacy_url: str | None = None
    # What one caller can make the server do: see `limiter`.
    calls_at_once: int = Field(default=8, ge=1)
    calls_per_minute: int = Field(default=300, ge=1)
    sandbox_runs_per_hour: int = Field(default=300, ge=1)
    apps_per_owner: int = Field(default=50, ge=1)
    # The sandbox time each owner's apps, and the whole server, may use in a calendar month,
    # in minutes: see `sandbox_time`. Unset, there is no budget, and nothing is counted. An
    # owner with the right plus has the Plus budget, and the free one while it is unset.
    sandbox_minutes_per_owner_month: int | None = Field(default=None, ge=1)
    sandbox_minutes_per_plus_owner_month: int | None = Field(default=None, ge=1)
    sandbox_minutes_per_month: int | None = Field(default=None, ge=1)

    @field_validator("data_dir", mode="after")
    @classmethod
    def _resolve_data_dir(cls, value: Path) -> Path:
        return value.resolve()

    @field_validator("base_url", mode="after")
    @classmethod
    def _clean_base_url(cls, value: str) -> str:
        return _clean_url(value, DEFAULT_BASE_URL)

    # Website_url is read after `base_url`, which it falls back to when it is not set.
    @field_validator("website_url", mode="after")
    @classmethod
    def _default_and_clean_website_url(cls, value: str, info: ValidationInfo) -> str:
        return _clean_url(value, info.data.get("base_url") or DEFAULT_BASE_URL)

    @field_validator("dev_login", mode="after")
    @classmethod
    def _clean_dev_login(cls, value: str | None) -> str | None:
        if not value:
            return None
        login = value.lower()
        refusal = login_problem(login)
        if refusal is not None:
            raise ValueError(f"CLIQUET_DEV_LOGIN: {refusal}")
        return login

    @field_validator(
        "allow_remote_dev_login",
        "allow_remote_local_executor",
        "grant_build_on_sign_up",
        mode="before",
    )
    @classmethod
    def _parse_flag(cls, value: Any, info: ValidationInfo) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        text = str(value).strip().lower()
        if not text:
            return False
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"CLIQUET_{str(info.field_name).upper()} must be 1 or 0")

    @field_validator("privacy_url", mode="after")
    @classmethod
    def _check_privacy_url(cls, value: str | None) -> str | None:
        if not value:
            return None
        address = urlparse(value)
        if address.scheme not in {"http", "https"} or not address.netloc:
            raise ValueError(
                "must be the privacy notice's http:// or https:// address, "
                "such as https://example.com/privacy"
            )
        return value

    @field_validator("oidc_issuer", mode="after")
    @classmethod
    def _check_oidc_issuer(cls, value: str | None) -> str | None:
        if not value:
            return None
        address = urlparse(value)
        secure = address.scheme == "https" or (address.scheme == "http" and is_local(value))
        if not secure or not address.netloc or address.query or address.fragment:
            raise ValueError(
                "must be the provider's https:// issuer, exactly as its tokens name it, such "
                "as https://example.eu.auth0.com/"
            )
        return value

    @field_validator("verification_email_url", mode="after")
    @classmethod
    def _check_verification_email_url(cls, value: str | None) -> str | None:
        if not value:
            return None
        address = urlparse(value)
        secure = address.scheme == "https" or (address.scheme == "http" and is_local(value))
        if not secure or not address.netloc:
            raise ValueError(
                "must be the https:// address of the provider's call that sends the "
                "verification email again, such as "
                "https://example.eu.auth0.com/api/v2/jobs/verification-email"
            )
        return value

    @field_validator("verification_email_method", mode="before")
    @classmethod
    def _upper_method(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("verification_email_body", mode="after")
    @classmethod
    def _check_verification_email_body(cls, value: str | None) -> str | None:
        if not value:
            return None
        try:
            body = json.loads(value)
        except json.JSONDecodeError:
            body = None
        if not isinstance(body, dict):
            raise ValueError('must be a JSON object, such as {"user_id": "{subject}"}')
        return value

    @field_validator("sign_up_params", mode="before")
    @classmethod
    def _parse_sign_up_params(cls, value: Any) -> Any:
        if not isinstance(value, str | None):
            return value
        if not value:
            return ()
        try:
            pairs = parse_qsl(value, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            pairs = []
        if not pairs or not all(name for name, _ in pairs):
            raise ValueError("must be a query string, such as screen_hint=signup")
        names = [name for name, _ in pairs]
        if len(set(names)) < len(names):
            raise ValueError("names a parameter twice: give each one value")
        if taken := sorted(set(names) & AUTHORIZE_PARAMS):
            raise ValueError(f"can't set {', '.join(taken)}: Cliquet sets it for every sign-in")
        return tuple(pairs)

    @field_validator("sprites_prefix", "sandbox_prefix", mode="after")
    @classmethod
    def _default_prefix(cls, value: str) -> str:
        return value or DEFAULT_PREFIX

    @field_validator("sandbox_sockets_volume", mode="after")
    @classmethod
    def _check_sockets_volume(cls, value: str | None) -> str | None:
        if not value:
            return None
        if not _VOLUME_NAME.fullmatch(value):
            raise ValueError(
                "must be a Docker volume's name: letters, digits, '_', '.' and '-', "
                "starting with a letter or a digit, such as cliquet_sockets"
            )
        return value

    @field_validator("sandbox_image", mode="after")
    @classmethod
    def _default_sandbox_image(cls, value: str) -> str:
        return value or DEFAULT_SANDBOX_IMAGE

    @model_validator(mode="before")
    @classmethod
    def _defaults_and_mode(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        values = dict(data)
        data_dir = Path(str(values.get("data_dir") or "cliquet-data")).resolve()
        values["data_dir"] = data_dir
        if not values.get("database_url"):
            values["database_url"] = f"sqlite:///{data_dir}/server.db"
        sockets = Path(str(values.get("sandbox_sockets") or data_dir / "sockets")).resolve()
        values["sandbox_sockets"] = sockets
        # The path the Docker daemon resolves: the same as the server's, unless the server runs
        # in a container, so it is taken as it is written.
        host = values.get("sandbox_sockets_host")
        if host and values.get("sandbox_sockets_volume"):
            raise ValueError(
                "CLIQUET_SANDBOX_SOCKETS_VOLUME and CLIQUET_SANDBOX_SOCKETS_HOST cannot be used "
                "together: keep the volume when the server runs in a container, else the folder"
            )
        values["sandbox_sockets_host"] = Path(str(host)) if host else sockets
        explicit = values.get("auth")
        if explicit in (None, ""):
            providers = []
            if values.get("dev_login"):
                providers.append("dev")
            if _has_oidc(values):
                providers.append("oidc")
            if len(providers) > 1:
                raise ValueError(
                    "CLIQUET_AUTH is ambiguous: use only one of CLIQUET_DEV_LOGIN or the "
                    "CLIQUET_OIDC_ settings"
                )
            values["auth"] = providers[0] if providers else "none"
        return values

    @model_validator(mode="after")
    def _check(self) -> Settings:
        mode = self.auth
        if mode == "none":
            if self.dev_login:
                raise ValueError("CLIQUET_AUTH=none cannot use CLIQUET_DEV_LOGIN")
            if _has_oidc(self):
                raise ValueError("CLIQUET_AUTH=none cannot have CLIQUET_OIDC_ settings")
        elif mode == "dev":
            if self.dev_login is None:
                raise ValueError("CLIQUET_AUTH=dev needs CLIQUET_DEV_LOGIN")
            if _has_oidc(self):
                raise ValueError("CLIQUET_AUTH=dev cannot be combined with CLIQUET_OIDC_ settings")
            if not is_local(self.base_url) and not self.allow_remote_dev_login:
                raise ValueError(
                    "CLIQUET_DEV_LOGIN needs a local CLIQUET_BASE_URL, or "
                    "CLIQUET_ALLOW_REMOTE_DEV_LOGIN=1 for a short public demo"
                )
        elif mode == "oidc":
            if self.dev_login is not None:
                raise ValueError("CLIQUET_DEV_LOGIN and CLIQUET_AUTH=oidc cannot be used together")
            _check_oidc(self)
        else:
            raise ValueError(f"CLIQUET_AUTH must be dev, oidc or none, not {mode!r}")

        _check_verification_email(self)
        _check_plus_budget(self)
        if self.executor == "sprites" and self.sprites_token is None:
            raise ValueError("CLIQUET_EXECUTOR=sprites needs CLIQUET_SPRITES_TOKEN")
        if (
            self.executor == "local"
            and not is_local(self.base_url)
            and not self.allow_remote_local_executor
        ):
            # App code runs with the server's rights under the local executor.
            raise ValueError(
                "the local executor runs app code on the server itself, so a public "
                "CLIQUET_BASE_URL needs CLIQUET_EXECUTOR=sprites, or "
                "CLIQUET_ALLOW_REMOTE_LOCAL_EXECUTOR=1 for a short demo of your own apps"
            )
        return self

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read the settings from a mapping (`os.environ` by default)."""
        if env is None:
            try:
                return cls()
            except ValidationError as error:
                raise SettingsError(_readable(error)) from error
        values = {
            "data_dir": env.get("CLIQUET_DATA_DIR") or "cliquet-data",
            "base_url": env.get("CLIQUET_BASE_URL") or DEFAULT_BASE_URL,
            "website_url": env.get("CLIQUET_WEBSITE_URL") or "",
            "database_url": _optional(env, "CLIQUET_DATABASE_URL"),
            "executor": env.get("CLIQUET_EXECUTOR") or "local",
            "auth": _optional(env, "CLIQUET_AUTH"),
            "dev_login": _optional(env, "CLIQUET_DEV_LOGIN"),
            "allow_remote_dev_login": env.get("CLIQUET_ALLOW_REMOTE_DEV_LOGIN") or "",
            "allow_remote_local_executor": env.get("CLIQUET_ALLOW_REMOTE_LOCAL_EXECUTOR") or "",
            "grant_build_on_sign_up": env.get("CLIQUET_GRANT_BUILD_ON_SIGN_UP") or "",
            "secret_key": _optional(env, "CLIQUET_SECRET_KEY"),
            "sprites_token": _optional(env, "CLIQUET_SPRITES_TOKEN"),
            "sprites_prefix": env.get("CLIQUET_SPRITES_PREFIX") or DEFAULT_PREFIX,
            "sandbox_image": env.get("CLIQUET_SANDBOX_IMAGE") or DEFAULT_SANDBOX_IMAGE,
            "sandbox_prefix": env.get("CLIQUET_SANDBOX_PREFIX") or DEFAULT_PREFIX,
            "sandbox_sockets": _optional(env, "CLIQUET_SANDBOX_SOCKETS"),
            "sandbox_sockets_host": _optional(env, "CLIQUET_SANDBOX_SOCKETS_HOST"),
            "sandbox_sockets_volume": _optional(env, "CLIQUET_SANDBOX_SOCKETS_VOLUME"),
            "sandbox_idle_seconds": env.get("CLIQUET_SANDBOX_IDLE_SECONDS")
            or DEFAULT_SANDBOX_IDLE_SECONDS,
            "sandbox_memory_mb": env.get("CLIQUET_SANDBOX_MEMORY_MB") or DEFAULT_SANDBOX_MEMORY_MB,
            "docker_socket": env.get("CLIQUET_DOCKER_SOCKET") or "/var/run/docker.sock",
            "warm_days": env.get("CLIQUET_WARM_DAYS") or DEFAULT_WARM_DAYS,
            "oidc_issuer": _optional(env, "CLIQUET_OIDC_ISSUER"),
            "oidc_client_id": _optional(env, "CLIQUET_OIDC_CLIENT_ID"),
            "oidc_client_secret": _optional(env, "CLIQUET_OIDC_CLIENT_SECRET"),
            "oidc_audience": _optional(env, "CLIQUET_OIDC_AUDIENCE"),
            "verify_email_days": env.get("CLIQUET_VERIFY_EMAIL_DAYS") or DEFAULT_VERIFY_EMAIL_DAYS,
            "verification_email_url": _optional(env, "CLIQUET_VERIFICATION_EMAIL_URL"),
            "verification_email_method": env.get("CLIQUET_VERIFICATION_EMAIL_METHOD") or "POST",
            "verification_email_body": _optional(env, "CLIQUET_VERIFICATION_EMAIL_BODY"),
            "verification_email_audience": _optional(env, "CLIQUET_VERIFICATION_EMAIL_AUDIENCE"),
            "sign_up_params": _optional(env, "CLIQUET_SIGN_UP_PARAMS"),
            "privacy_url": _optional(env, "CLIQUET_PRIVACY_URL"),
        }
        for name in _LIMITS:
            value = _optional(env, f"CLIQUET_{name.upper()}")
            if value is not None:
                values[name] = value
        try:
            return cls.model_validate(values)
        except ValidationError as error:
            raise SettingsError(_readable(error)) from error


def _has_oidc(settings: Mapping[str, Any] | Settings) -> bool:
    return any(
        settings.get(name) if isinstance(settings, Mapping) else getattr(settings, name)
        for name in ("oidc_issuer", "oidc_client_id", "oidc_client_secret", "oidc_audience")
    )


def _check_oidc(settings: Settings) -> None:
    given = {
        "CLIQUET_OIDC_ISSUER": settings.oidc_issuer,
        "CLIQUET_OIDC_CLIENT_ID": settings.oidc_client_id,
        "CLIQUET_OIDC_CLIENT_SECRET": settings.oidc_client_secret,
        "CLIQUET_SECRET_KEY": settings.secret_key,
    }
    if not all(given.values()):
        missing = ", ".join(name for name, value in given.items() if value is None)
        raise ValueError(f"sign-in with CLIQUET_AUTH=oidc is incomplete; set {missing}")
    _check_secret(settings.secret_key)


def _check_verification_email(settings: Settings) -> None:
    """Refuse a call that sends the verification email again without what it needs: the
    sign-in provider, and the person's subject in its address or its body."""
    url = settings.verification_email_url
    if url is None:
        others = {
            "CLIQUET_VERIFICATION_EMAIL_METHOD": settings.verification_email_method != "POST",
            "CLIQUET_VERIFICATION_EMAIL_BODY": settings.verification_email_body,
            "CLIQUET_VERIFICATION_EMAIL_AUDIENCE": settings.verification_email_audience,
        }
        if given := [name for name, value in others.items() if value]:
            raise ValueError(
                f"{given[0]} needs CLIQUET_VERIFICATION_EMAIL_URL: set the address of the "
                "provider's call, or unset it"
            )
        return
    if settings.auth != "oidc":
        raise ValueError(
            "CLIQUET_VERIFICATION_EMAIL_URL needs CLIQUET_AUTH=oidc: only a sign-in provider "
            "sends verification emails"
        )
    if SUBJECT not in url and SUBJECT not in (settings.verification_email_body or ""):
        raise ValueError(
            f"CLIQUET_VERIFICATION_EMAIL_URL names nobody: put {SUBJECT} in it, or in "
            "CLIQUET_VERIFICATION_EMAIL_BODY, where the person's subject goes"
        )


def _check_plus_budget(settings: Settings) -> None:
    """Refuse a Plus budget that would cap Plus owners more than free ones."""
    plus, free = (
        settings.sandbox_minutes_per_plus_owner_month,
        settings.sandbox_minutes_per_owner_month,
    )
    if plus is None:
        return
    if free is None:
        raise ValueError(
            "CLIQUET_SANDBOX_MINUTES_PER_PLUS_OWNER_MONTH needs "
            "CLIQUET_SANDBOX_MINUTES_PER_OWNER_MONTH: set the free budget, or unset the Plus one"
        )
    if plus < free:
        raise ValueError(
            f"CLIQUET_SANDBOX_MINUTES_PER_PLUS_OWNER_MONTH ({plus}) is below "
            f"CLIQUET_SANDBOX_MINUTES_PER_OWNER_MONTH ({free}): raise it to at least the free one"
        )


def _check_secret(secret: SecretStr | None) -> None:
    if secret is None:
        raise ValueError("CLIQUET_SECRET_KEY is required for sign-in")
    if len(secret.get_secret_value()) < 32:
        raise ValueError("CLIQUET_SECRET_KEY must be at least 32 characters")


def is_local(base_url: str) -> bool:
    """True when the address can only be reached from this machine."""
    return urlparse(base_url).hostname in {"127.0.0.1", "localhost", "::1"}


# The settings that limit what one caller can make the server do, each with its default.
_LIMITS = (
    "calls_at_once",
    "calls_per_minute",
    "sandbox_runs_per_hour",
    "apps_per_owner",
    "sandbox_minutes_per_owner_month",
    "sandbox_minutes_per_plus_owner_month",
    "sandbox_minutes_per_month",
)


def _optional(env: Mapping[str, str], name: str) -> str | None:
    """The variable's value, or None when it is unset or empty."""
    return env.get(name) or None


def _readable(error: ValidationError) -> str:
    """One line for the first validation error, with the setting's name."""
    first = error.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", "invalid setting"))
    if location:
        name = location.upper().replace(".", "_")
        if not name.startswith("CLIQUET_"):
            name = f"CLIQUET_{name}"
        return f"{name}: {message}"
    return message
