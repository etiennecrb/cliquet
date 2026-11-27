"""The sign-in provider: any OpenID Connect provider, known by its issuer.

Cliquet reads the provider's discovery document, at the issuer's
`/.well-known/openid-configuration`, for the addresses it signs people in at, the keys its
access tokens are signed with, and whether it registers MCP clients. The document is read
when it is first needed and kept for an hour. Its `issuer` must be the configured one
exactly, or nothing it says is used.
"""

import base64
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier

from cliquet_server.app_auth import me_address
from cliquet_server.settings import Settings

logger = logging.getLogger(__name__)

DISCOVERY = "/.well-known/openid-configuration"
# How long the discovery document is kept before it is read again.
DISCOVERY_AGE = 60 * 60
# The algorithms an access token may be signed with: only those with a published public key,
# never a shared secret or none.
ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
        "EdDSA",
        "Ed25519",
        "Ed448",
    }
)


class ProviderError(Exception):
    """The sign-in provider can't be used now. The message says why, and what to do."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Discovery:
    """What Cliquet reads in the provider's discovery document."""

    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None
    end_session_endpoint: str | None
    # How the provider takes the client's secret at its token endpoint: by a form field or
    # by basic authentication.
    secret_in_form: bool
    # Whether its redirect back names its issuer (RFC 9207), which is then checked.
    names_issuer: bool
    # Whether it registers an MCP client it has never seen: by dynamic client registration,
    # or from the client's ID metadata document.
    registers_clients: bool


class Provider:
    """The sign-in provider of the settings, read through `transport`, or the network."""

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if settings.oidc_issuer is None:
            raise ValueError("the sign-in provider needs CLIQUET_OIDC_ISSUER")
        self.settings = settings
        self.issuer = settings.oidc_issuer
        self.audience = settings.oidc_audience or me_address(settings.base_url)
        self._transport = transport
        self._discovery: Discovery | None = None
        self._read_at = 0.0
        self._verifiers: dict[str, JWTVerifier] = {}
        # Set once the check at start is done, whatever it found.
        self.checked = threading.Event()

    def client(self) -> httpx.AsyncClient:
        """An HTTP client for the provider's endpoints."""
        return httpx.AsyncClient(timeout=10, transport=self._transport)

    async def read(self, address: str) -> Any:
        """The JSON document at one of the provider's addresses. Raises `ProviderError` when
        the provider doesn't answer with one."""
        try:
            async with self.client() as client:
                response = await client.get(address)
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ProviderError(
                f"The sign-in provider {self.issuer} did not answer at {address}: {error}."
            ) from error

    async def discovery(self) -> Discovery:
        """What the discovery document says, read again once it is an hour old. Raises
        `ProviderError` when it can't be read or names another issuer."""
        if self._discovery is not None and time.monotonic() - self._read_at < DISCOVERY_AGE:
            return self._discovery
        document = await self.read(self.issuer.rstrip("/") + DISCOVERY)
        self._discovery = self._parse(document)
        self._read_at = time.monotonic()
        return self._discovery

    def _parse(self, document: Any) -> Discovery:
        """The parts of a discovery document Cliquet uses, once its issuer is checked."""
        if not isinstance(document, dict):
            raise ProviderError(
                f"The sign-in provider {self.issuer} has no discovery document at "
                f"{self.issuer.rstrip('/')}{DISCOVERY}. Check CLIQUET_OIDC_ISSUER."
            )
        found = document.get("issuer")
        if found != self.issuer:
            raise ProviderError(
                f"The sign-in provider names its issuer {found!r}, not {self.issuer!r}: set "
                "CLIQUET_OIDC_ISSUER to its issuer exactly, then restart the server."
            )
        required = {}
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = document.get(name)
            if not isinstance(value, str) or not value:
                raise ProviderError(
                    f"The sign-in provider {self.issuer} gives no {name} in its discovery "
                    "document, which Cliquet needs to sign people in."
                )
            required[name] = value
        methods = document.get("token_endpoint_auth_methods_supported")
        return Discovery(
            **required,
            userinfo_endpoint=_text(document.get("userinfo_endpoint")),
            end_session_endpoint=_text(document.get("end_session_endpoint")),
            # Without the list, a provider takes basic authentication only, as the standard
            # says.
            secret_in_form=isinstance(methods, list) and "client_secret_post" in methods,
            names_issuer=document.get("authorization_response_iss_parameter_supported") is True,
            registers_clients=bool(_text(document.get("registration_endpoint")))
            or document.get("client_id_metadata_document_supported") is True,
        )

    async def verify(self, token: str) -> AccessToken | None:
        """The access token's claims, when the provider signed it for Cliquet's audience and it
        hasn't expired, or None."""
        algorithm = _algorithm(token)
        if algorithm not in ALGORITHMS:
            return None
        try:
            found = await self.discovery()
        except ProviderError as error:
            # Once per token: check() and the web sign-in say it where it is seen.
            logger.debug("%s", error.message)
            return None
        verifier = self._verifiers.get(algorithm)
        if verifier is None or verifier.jwks_uri != found.jwks_uri:
            self._verifiers[algorithm] = _Verifier(
                self,
                jwks_uri=found.jwks_uri,
                issuer=self.issuer,
                audience=self.audience,
                algorithm=algorithm,
            )
        return await self._verifiers[algorithm].verify_token(token)

    async def userinfo(self, token: str) -> dict[str, Any] | None:
        """What the provider's userinfo endpoint says of the person an access token names, or
        None when it has none, or doesn't answer, or refuses the token, as Auth0 refuses one
        without the `openid` scope."""
        try:
            endpoint = (await self.discovery()).userinfo_endpoint
            if endpoint is None:
                return None
            async with self.client() as client:
                response = await client.get(endpoint, headers={"Authorization": f"Bearer {token}"})
                response.raise_for_status()
                profile = response.json()
        except ProviderError, httpx.HTTPError, ValueError:
            return None
        return profile if isinstance(profile, dict) else None

    def authenticated(
        self, found: Discovery, data: dict[str, str]
    ) -> tuple[dict[str, str], dict[str, str]]:
        """A request to the token endpoint's form `data` and headers, with the client's ID
        and secret where the provider takes them."""
        client_id = self.settings.oidc_client_id or ""
        secret = self.settings.oidc_client_secret
        value = secret.get_secret_value() if secret is not None else ""
        if found.secret_in_form:
            return data | {"client_id": client_id, "client_secret": value}, {}
        return data, {"Authorization": _basic(client_id, value)}

    async def client_token(self, audience: str | None) -> tuple[str, int]:
        """An access token for the server itself, by OAuth's client credentials grant, with
        the sign-in's client ID and secret, for `audience` when it is set, and the seconds it
        lasts, 5 minutes when the provider doesn't say. Raises `ProviderError` when the
        provider gives none."""
        found = await self.discovery()
        data, headers = self.authenticated(found, {"grant_type": "client_credentials"})
        if audience is not None:
            data["audience"] = audience
        try:
            async with self.client() as client:
                response = await client.post(found.token_endpoint, data=data, headers=headers)
                response.raise_for_status()
                answer = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ProviderError(
                f"The sign-in provider {self.issuer} gave the server no token of its own at "
                f"{found.token_endpoint}: {error}. Check that its client may use the client "
                "credentials grant."
            ) from error
        token = answer.get("access_token") if isinstance(answer, dict) else None
        if not isinstance(token, str) or not token:
            raise ProviderError(
                f"The sign-in provider {self.issuer} answered with no access token at "
                f"{found.token_endpoint}."
            )
        lasts = answer.get("expires_in")
        return token, lasts if isinstance(lasts, int) and lasts > 0 else 300

    async def check(self) -> None:
        """Read the discovery document once at start, and say in the log when assistants
        can't sign in. Never raises: the server serves whatever the provider answers."""
        try:
            found = await self.discovery()
        except ProviderError as error:
            logger.warning("%s Cliquet reads it again when someone signs in.", error.message)
        except Exception:
            logger.exception("unexpected: the sign-in provider's discovery document failed")
        else:
            if not found.registers_clients:
                logger.warning(
                    "The sign-in provider %s registers no MCP client: it offers neither "
                    "dynamic client registration nor client ID metadata documents. "
                    "Assistants can't sign in at %s; API tokens still work.",
                    self.issuer,
                    me_address(self.settings.base_url),
                )
        finally:
            self.checked.set()


class _Verifier(JWTVerifier):
    """FastMCP's check of a signed access token, reading the provider's keys through the
    provider's own HTTP client."""

    def __init__(self, provider: Provider, **options: Any) -> None:
        super().__init__(**options)
        self._provider = provider

    async def _fetch_jwks(self) -> dict[str, Any]:
        """The provider's published keys."""
        assert self.jwks_uri is not None
        try:
            keys = await self._provider.read(self.jwks_uri)
        except ProviderError as error:
            # What the verifier takes for keys it can't read: the token is refused.
            raise ValueError(error.message) from error
        return keys if isinstance(keys, dict) else {}


def _algorithm(token: str) -> str | None:
    """The algorithm a JWT's header names, or None when it isn't a JWT."""
    head = token.split(".", 1)[0]
    try:
        header = json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
    except ValueError:
        return None
    algorithm = header.get("alg") if isinstance(header, dict) else None
    return algorithm if isinstance(algorithm, str) else None


def _basic(client_id: str, secret: str) -> str:
    """The client's basic authentication, each part form-encoded first, as OAuth says."""
    pair = f"{quote(client_id, safe='')}:{quote(secret, safe='')}"
    return "Basic " + base64.b64encode(pair.encode()).decode()


def _text(value: Any) -> str | None:
    """A non-empty string from a discovery document, or None."""
    return value if isinstance(value, str) and value else None
