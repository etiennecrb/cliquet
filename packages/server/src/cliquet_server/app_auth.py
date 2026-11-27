"""Who calls Cliquet's MCP endpoints, and where a limited token may go.

An app endpoint takes only Cliquet's tokens limited to its app, among others: it serves no
protected-resource metadata, so no client tries to sign in there. The person's endpoint takes
the sign-in provider's access tokens for its audience, its own address by default, and
Cliquet's tokens for everywhere, and refuses a limited one.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import anyio
from fastmcp.server.auth import AccessToken
from pydantic import ConfigDict

from cliquet.tools import listed
from cliquet_server import tokens
from cliquet_server.db import Database, EmailTaken, PersonInfo, TokenExpired, TokenLimit
from cliquet_server.identity import is_email
from cliquet_server.settings import Settings
from cliquet_server.sign_in import (
    EMAIL_ELSEWHERE,
    Notices,
    development_person,
    email_taken,
    email_verified,
    email_waiting,
    joined,
    locked_out,
)

if TYPE_CHECKING:
    from cliquet_server.oidc import Provider

# The scopes an MCP client asks the sign-in provider for.
SCOPES = ("openid", "email")
# The tool on the person's endpoint with which a newcomer chooses their login, and why any
# other tool refuses them until then.
CHOOSE_LOGIN = "choose_login"
CHOOSE_FIRST = (
    "The person has no login on Cliquet yet: ask them which login they want, then call "
    f"{CHOOSE_LOGIN} with it. This tool works once they have one."
)
# How long the userinfo endpoint's answer about a newcomer is kept, in seconds, a failure too:
# each of their requests checks their token again. About a person whose email isn't verified,
# it is kept an hour, so it is read at most once an hour until it says the email is verified.
USERINFO_AGE = 10 * 60
UNVERIFIED_AGE = 60 * 60


@dataclass(frozen=True)
class Newcomer:
    """An identity the sign-in provider vouches for, that has no person yet: it chooses a
    login with `CHOOSE_LOGIN` first. Its email, if the provider verified one, and whether it
    may build once it has a login: as the server's first person, or as every new one when the
    server grants build at sign-up."""

    issuer: str
    subject: str
    email: str | None
    builds: bool


class AuthRefused(Exception):
    """A verified token that cannot be turned into a person. `status` is the HTTP answer."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


class PersonAccess(AccessToken):
    """A bearer token Cliquet checked, with the person it acts as, or why it names none."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    person: PersonInfo | None = None
    refused: AuthRefused | None = None
    # The apps one of Cliquet's tokens is limited to, one role in each, if it is.
    limits: tuple[TokenLimit, ...] | None = None
    # One of Cliquet's tokens, by its name, kept with each change it makes, and its id, which
    # the app's code sees: None for a sign-in.
    api_token: tokens.ApiToken | None = None
    # One of Cliquet's tokens that expired: it acts as nobody, and is refused for it.
    expired: TokenExpired | None = None
    # A sign-in of an identity with no person yet, which only chooses its login.
    newcomer: Newcomer | None = None
    # Why every call of `person` is refused until they verify their email, as `locked_out`
    # says: they still see their tools.
    locked_out: str | None = None


class AppAuth:
    """Who a bearer token is: one of Cliquet's tokens, an access token of the sign-in
    provider, or the development login."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        clock: Callable[[], datetime] | None = None,
        provider: Provider | None = None,
        notices: Notices | None = None,
        retire: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self._clock = clock or (lambda: datetime.now(UTC))
        self.provider = provider
        # What a sign-in tells its person once, such as that another account has their email.
        self.notices = notices or Notices()
        # What finishes deleting an account a sign-in closed, as `person_from_claims` says.
        self.retire = retire
        # What the userinfo endpoint said of each newcomer, and of each person whose email
        # isn't verified, by issuer and subject, until when, as a timestamp of `_clock`.
        self._userinfo: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

    async def access(self, token: str, resource: str | None = None) -> PersonAccess | None:
        """What a bearer token gives, with its person, or None when it is invalid.

        One of Cliquet's own tokens acts as its person, with no call to the identity provider,
        whatever the sign-in, until it expires: then it acts as nobody, refused with the
        sentence that says when, and where to make another. Under the development login, any
        other token is that login's person; otherwise it is the sign-in provider's, taken only
        by an endpoint that names its `resource`, and invalid without one. A person whose time
        to verify their email is over is refused every call, as `locked_out` says. The database
        is read in a thread, off the event loop.
        """
        now = self._clock()
        if tokens.is_token(token):
            try:
                found = await anyio.to_thread.run_sync(self.database.token_access, token, now)
            except TokenExpired as expired:
                refused = AuthRefused(401, expired_token(self.settings.base_url, expired.at))
                return PersonAccess(
                    token=token,
                    client_id="cliquet-token",
                    scopes=list(SCOPES),
                    refused=refused,
                    expired=expired,
                )
            if found is None:
                return None
            person, limits, api_token = found
            return self._checked(_granted(token, "cliquet-token", person, limits, api_token), now)
        if self.settings.auth == "dev":
            try:
                person = await anyio.to_thread.run_sync(self._dev_person)
            except AuthRefused as refused:
                return PersonAccess(
                    token=token, client_id="cliquet-dev", scopes=list(SCOPES), refused=refused
                )
            return None if person is None else _granted(token, "cliquet-dev", person)
        if self.provider is None or resource is None:
            return None
        verified = await self.provider.verify(token)
        if verified is None:
            return None
        fields = verified.model_dump()
        claims, subject = dict(fields.get("claims") or {}), fields.get("subject")

        def found(claims: dict[str, Any]) -> PersonInfo | Newcomer:
            return person_from_claims(
                self.database, self.settings, claims, subject, now, self.notices, self.retire
            )

        try:
            person = await anyio.to_thread.run_sync(found, claims)
            age = _ask_userinfo(person, claims)
            if age is not None:
                # The token says no verified email: the provider's userinfo may say one.
                said = await self._said(token, person, age, now)
                # Only a verified email can change whom the claims name.
                if _verified_email(claims | said) is not None:
                    person = await anyio.to_thread.run_sync(found, claims | said)
            if isinstance(person, Newcomer):
                return PersonAccess(**fields, newcomer=person)
        except AuthRefused as refused:
            return PersonAccess(**fields, refused=refused)
        return self._checked(PersonAccess(**fields, person=person), now)

    def _checked(self, access: PersonAccess, now: datetime) -> PersonAccess:
        """The access, refused every call when its person's time to verify their email is
        over."""
        assert access.person is not None
        refusal = locked_out(self.settings, access.person, now)
        return access if refusal is None else access.model_copy(update={"locked_out": refusal})

    async def _said(
        self, token: str, who: PersonInfo | Newcomer, age: int, now: datetime
    ) -> dict[str, Any]:
        """The email and whether it is verified, as the provider's userinfo endpoint says of
        the person or newcomer the token names, kept `age` seconds, or nothing, when it
        refuses or names someone else."""
        key = (str(who.issuer), str(who.subject))
        at = now.timestamp()
        kept = self._userinfo.get(key)
        if kept is not None and kept[0] > at:
            return kept[1]
        assert self.provider is not None
        profile = await self.provider.userinfo(token) or {}
        said = {}
        if profile.get("sub") == who.subject:
            said = {name: profile[name] for name in ("email", "email_verified") if name in profile}
        self._userinfo = {one: value for one, value in self._userinfo.items() if value[0] > at}
        self._userinfo[key] = (at + age, said)
        return said

    def authorization_server(self) -> str:
        """The authorization server every MCP endpoint names: the sign-in provider's issuer,
        exactly, or this server under the development login."""
        return self.settings.oidc_issuer or self.settings.base_url

    def _dev_person(self) -> PersonInfo | None:
        if self.settings.dev_login is None:
            return None
        try:
            return development_person(self.database, self.settings, self._clock())
        except ValueError as error:
            raise AuthRefused(403, str(error)) from error


def app_address(base_url: str, login: str, name: str) -> str:
    """The MCP address of an app, for an agent that runs on its own with a limited token."""
    return f"{base_url}/apps/{login}/{name}/mcp"


def me_address(base_url: str) -> str:
    """The person's own MCP address, for their assistant: every app they may use."""
    return f"{base_url}/me/mcp"


def tokens_link(login: str, name: str) -> str:
    """The path of the tokens page that makes a token limited to one app."""
    return f"/tokens?app={login}/{name}"


def filled_tokens_link(name: str, limits: Sequence[TokenLimit], days: int) -> str:
    """The path of the tokens page with its form filled in: the token's name, the role in
    each of its apps, and how many days it works, for the person to make it."""
    chosen = [("limit", limit_value(one)) for one in limits]
    return f"/tokens?{urlencode([('name', name), *chosen, ('days', days)], safe='/')}"


def expired_token(base_url: str, at: datetime) -> str:
    """Why an expired token is refused, wherever it is sent, and where to make another."""
    return f"This token expired on {tokens.expiry(at)}: make another at {base_url}/tokens."


def maker_expiring(base_url: str, at: datetime) -> str:
    """Why a token that expires within the shortest lifetime makes no token."""
    return (
        f"The token you call with expires on {tokens.expiry(at)}, sooner than the shortest "
        f"lifetime of {tokens.LIFETIMES[0]} days: make a new token at {base_url}/tokens, and "
        "call with it."
    )


def limit_value(limit: TokenLimit) -> str:
    """One app a limited token works for, and its role there, as the tokens form sends it:
    login/name/role."""
    return f"{limit.login}/{limit.name}/{limit.role}"


def limit_label(limit: TokenLimit) -> str:
    """One app a limited token works for, and its role there, as a person reads it."""
    app = f"{limit.login}/{limit.name}"
    return app if limit.role == "-" else f"{app} as {limit.role}"


def limits_label(limits: tuple[TokenLimit, ...]) -> str:
    """The apps a limited token works for, each with its role, as a person reads them."""
    return listed([limit_label(limit) for limit in limits])


def limited_elsewhere(base_url: str, limits: tuple[TokenLimit, ...]) -> str:
    """Why a limited token is refused anywhere but its apps' endpoints, and what reaches the
    rest."""
    addresses = ", ".join(app_address(base_url, one.login, one.name) for one in limits)
    at = "at" if len(limits) == 1 else "each at its own address,"
    return (
        f"This token is limited to {limits_label(limits)}, {at} {addresses}: connect an "
        f"assistant at {me_address(base_url)}, or make another token at {base_url}/tokens, "
        "to reach the rest."
    )


def needs_limited_token(base_url: str, login: str, name: str) -> str:
    """Why an app endpoint refuses a request without a token limited to its app."""
    return (
        f"This address takes only a token limited to {login}/{name}: make one at "
        f"{base_url}{tokens_link(login, name)}, or connect an assistant at "
        f"{me_address(base_url)}."
    )


def _granted(
    token: str,
    client: str,
    person: PersonInfo,
    limits: tuple[TokenLimit, ...] | None = None,
    api_token: tokens.ApiToken | None = None,
) -> PersonAccess:
    """The access one of Cliquet's own checks gives: every scope, as its person, limited to
    its apps when the token is, and with the token's name and id when it is one of Cliquet's."""
    return PersonAccess(
        token=token,
        client_id=client,
        scopes=list(SCOPES),
        person=person,
        limits=limits,
        api_token=api_token,
    )


def _ask_userinfo(person: PersonInfo | Newcomer, claims: dict[str, Any]) -> int | None:
    """How long to keep what the userinfo endpoint says, when it may say the verified email
    the claims don't: of a newcomer, or of a person whose email isn't verified. None when
    there is nothing to ask."""
    if _verified_email(claims) is not None:
        return None
    if isinstance(person, Newcomer):
        return USERINFO_AGE
    return None if person.email_verified else UNVERIFIED_AGE


def _verified_email(claims: dict[str, Any]) -> str | None:
    """The email the claims say the provider verified, in lower case, or None."""
    email = claims.get("email")
    if not isinstance(email, str) or not is_email(email):
        return None
    return email.strip().lower() if email_verified(claims.get("email_verified")) else None


def person_from_claims(
    database: Database,
    settings: Settings,
    claims: dict[str, Any],
    subject: object,
    now: datetime,
    notices: Notices,
    retire: Callable[[str], None] | None = None,
) -> PersonInfo | Newcomer:
    """The person the claims of the sign-in provider name, from an access token or its
    userinfo endpoint: by their issuer and subject, or by their verified email when they have
    one, as for the imported owner waiting for it. A verified email is saved, and claims the
    invitations waiting for it. A new identity is a `Newcomer`, with no person until it
    chooses a login.

    A person whose email wasn't verified, and another account has it once it is, keeps none:
    `notices` tells them once. When that account was waiting for it and theirs held nothing,
    they are that account from now on, and `retire` is given their closed account's login, to
    finish deleting it. Raises `AuthRefused` when the claims name nobody, or a new identity's
    verified email is another identity's, with `EMAIL_ELSEWHERE`."""
    issuer = str(claims.get("iss") or settings.oidc_issuer)
    subject = str(claims.get("sub") or subject or "")
    if not subject:
        raise AuthRefused(401, "The token has no subject.")
    person = database.person_by_identity(issuer, subject)
    email = _verified_email(claims)
    if email is not None:
        if person is None or person.email != email or not person.email_verified:
            try:
                known = database.known_person(issuer, subject, email, now)
            except EmailTaken:
                raise AuthRefused(403, EMAIL_ELSEWHERE) from None
            except ValueError as error:
                raise AuthRefused(403, str(error)) from error
            if person is not None and known is not None and known.id != person.id:
                # Their account held nothing, and the one waiting for this email is theirs.
                notices.tell(known.id, joined(known.login, person.login))
                if retire is not None:
                    retire(person.login)
            elif person is not None and not person.email_verified and known and not known.email:
                # Verified just now, with an email another account has.
                holder = database.person_by_email(email)
                waiting = holder is not None and holder.subject is None
                notices.tell(known.id, email_waiting(email) if waiting else email_taken(email))
            person = known
        if person is not None:
            database.claim_invitations(person, now)
    if person is not None:
        return person
    builds = settings.grant_build_on_sign_up or not database.had_people()
    return Newcomer(issuer, subject, email, builds)
