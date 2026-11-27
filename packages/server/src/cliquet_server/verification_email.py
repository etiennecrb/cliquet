"""Sending a person their verification email again, from the console, when they lost it.

OpenID Connect defines no way to ask for it, so the operator names the provider's own call
in the settings: an address, a method and a JSON body, in which `{subject}` stands for the
person's subject. Cliquet sends it with an access token it gets by OAuth's client credentials
grant at the provider's token endpoint, with the sign-in's client ID and secret, for the
audience the settings name, if any. Nothing here is one provider's own: Auth0's Management
API and Keycloak's admin API are each one configuration of it, as the self-hosting guide
shows.

A person gets one email every few minutes at most, counted in memory, so a restart forgets
it. The token is kept until shortly before it expires, since some providers count them.
"""

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from cliquet_server.db import PersonInfo
from cliquet_server.oidc import Provider, ProviderError
from cliquet_server.settings import SUBJECT, Settings

logger = logging.getLogger(__name__)

# How long a person waits between two emails.
WAIT = timedelta(minutes=5)
# How long before its expiry the server's token is taken again.
MARGIN = 60


class VerificationEmails:
    """The call that sends a person their verification email again, for the people the
    sign-in provider knows."""

    def __init__(
        self, settings: Settings, provider: Provider, clock: Callable[[], datetime]
    ) -> None:
        assert settings.verification_email_url is not None
        self.settings = settings
        self.provider = provider
        self._clock = clock
        self._url = settings.verification_email_url
        body = settings.verification_email_body
        self._body: dict[str, Any] | None = json.loads(body) if body else None
        # When each person last asked for an email that was sent, or is being sent.
        self._asked: dict[int, datetime] = {}
        self._token: tuple[str, float] | None = None

    @classmethod
    def of(
        cls, settings: Settings, provider: Provider | None, clock: Callable[[], datetime]
    ) -> VerificationEmails | None:
        """The call the settings name, or None when they name none."""
        if settings.verification_email_url is None or provider is None:
            return None
        return cls(settings, provider, clock)

    def offered(self, person: PersonInfo) -> bool:
        """Whether the person may ask for one: their email isn't verified, and the provider
        knows them, by its issuer and their subject."""
        return (
            not person.email_verified
            and person.subject is not None
            and person.issuer == self.provider.issuer
        )

    async def send(self, person: PersonInfo) -> str:
        """Ask the provider to send the person their verification email again, and say what
        came of it, in a sentence for the person."""
        if person.email_verified:
            return "Your email address is verified already."
        subject = person.subject
        if subject is None or not self.offered(person):
            return (
                "Cliquet can't ask the sign-in provider for a new email for this account. "
                "Sign in again, or ask the server's operator."
            )
        now = self._clock()
        self._asked = {one: at for one, at in self._asked.items() if now - at < WAIT}
        if person.id in self._asked:
            return (
                "A new email to verify your address is on its way. Give it a few minutes, and "
                "check your spam folder, before you ask for another."
            )
        self._asked[person.id] = now
        try:
            await self._call(subject)
        except ProviderError, httpx.HTTPError:
            self._asked.pop(person.id, None)
            return (
                "The sign-in provider didn't send a new email. If you verified your address "
                f"already, sign in again at {self.settings.base_url}. Otherwise, try again in a "
                "few minutes, or ask the server's operator."
            )
        return (
            "The sign-in provider is sending you a new email to verify your address. Open its "
            f"link, then sign in again at {self.settings.base_url}."
        )

    async def _call(self, subject: str) -> None:
        """The provider's call, for the person `subject`. Raises `ProviderError` or
        `httpx.HTTPError` when it doesn't succeed, said in the log for the operator."""
        token = await self._server_token()
        address = self._url.replace(SUBJECT, quote(subject, safe=""))
        body = None if self._body is None else _filled(self._body, subject)
        method = self.settings.verification_email_method
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with self.provider.client() as client:
                response = await client.request(method, address, json=body, headers=headers)
        except httpx.HTTPError as error:
            logger.warning("the verification email wasn't sent: %s %s: %s", method, address, error)
            raise
        if response.is_success:
            return
        if response.status_code == 401:
            # A token the provider no longer takes: the next ask gets another.
            self._token = None
        logger.warning(
            "the verification email wasn't sent: %s %s answered %s: %s",
            method,
            address,
            response.status_code,
            response.text[:300],
        )
        response.raise_for_status()

    async def _server_token(self) -> str:
        """The server's own access token for the call, kept until shortly before it expires.
        Raises `ProviderError`, said in the log, when the provider gives none."""
        if self._token is not None and time.monotonic() < self._token[1]:
            return self._token[0]
        try:
            token, lasts = await self.provider.client_token(
                self.settings.verification_email_audience
            )
        except ProviderError as error:
            logger.warning("the verification email wasn't sent: %s", error.message)
            raise
        self._token = (token, time.monotonic() + max(lasts - MARGIN, 0))
        return token


def _filled(value: Any, subject: str) -> Any:
    """The body, with the person's subject in place of each `{subject}` in its strings."""
    if isinstance(value, str):
        return value.replace(SUBJECT, subject)
    if isinstance(value, list):
        return [_filled(one, subject) for one in value]
    if isinstance(value, dict):
        return {key: _filled(one, subject) for key, one in value.items()}
    return value
