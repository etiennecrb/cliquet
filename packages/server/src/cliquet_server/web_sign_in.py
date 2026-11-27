"""Web sign-in: the development login or the sign-in provider, and the session it leaves.

The provider signs a person in by OpenID Connect's authorization code flow, with PKCE, and
Cliquet reads who they are from its userinfo endpoint. It asks for no audience or resource:
the access token it gets serves that one read.

The session cookie holds a person id and an expiry, signed with the server's secret. It
never holds a token of the provider or the person's email.

A new identity has no person yet: its first sign-in ends on a page where it chooses its
login. Until then a short-lived cookie, signed in the same way, holds its issuer, subject and
verified email, if it has one, and where it was going.

A person whose email isn't verified signs in too, with no email. Once their time to verify it
is over, every page that names the signed-in person refuses them, raising `LockedOut`, until
a sign-in reads their verified email: all but their account page, with its form that deletes
the account, and their apps' downloads.
"""

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from functools import partial
from typing import Any
from urllib.parse import urlencode

import anyio
import httpx
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from cliquet_server.app_auth import AuthRefused, Newcomer, person_from_claims
from cliquet_server.db import Database, EmailTaken, LoginRefused, PersonInfo
from cliquet_server.identity import MAX_LOGIN, login_problem
from cliquet_server.oidc import Discovery, Provider, ProviderError
from cliquet_server.pages import problem, render
from cliquet_server.settings import Settings
from cliquet_server.sign_in import (
    EMAIL_ELSEWHERE,
    LockedOut,
    Notices,
    development_person,
    free_login,
    locked_out,
)
from cliquet_server.verification_email import VerificationEmails

SESSION_COOKIE = "session"
OAUTH_COOKIE = "oauth"
SESSION_AGE = 7 * 24 * 60 * 60
OAUTH_AGE = 10 * 60
NEW_PERSON_COOKIE = "new_person"
NEW_PERSON_AGE = 30 * 60
CHOOSE_LOGIN_PATH = "/auth/choose-login"


class WebAuth:
    """Sign-in for the web app, and the signed session cookie that remembers a person."""

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
        self.provider = provider
        # What a sign-in tells its person once, such as that another account has their email.
        self.notices = notices or Notices()
        # What finishes deleting an account a sign-in closed, as `person_from_claims` says.
        self.retire = retire
        self._clock = clock or (lambda: datetime.now(UTC))
        # What sends a person their verification email again, when the settings name a call.
        self.emails = VerificationEmails.of(settings, provider, self._clock)
        if settings.secret_key is not None:
            self._secret = settings.secret_key.get_secret_value().encode()
        else:
            self._secret = secrets.token_bytes(32)
        self._secure = settings.base_url.startswith("https://")

    async def person(self, request: Request, *, even_locked_out: bool = False) -> PersonInfo | None:
        """The signed-in person, or None when there is no valid session. Raises `LockedOut`
        when their time to verify their email is over, unless `even_locked_out`: only the
        account page, its deletion and an owner's download let them through, so they keep
        their apps or delete them."""
        value = request.cookies.get(SESSION_COOKIE)
        data = self._read(value)
        if data is None:
            return None
        person_id = data.get("pid")
        if not isinstance(person_id, int):
            return None
        person = await anyio.to_thread.run_sync(self.database.person_by_id, person_id)
        if person is None or even_locked_out:
            return person
        if refusal := self.locked_out(person):
            raise LockedOut(refusal, self.csrf_for_session(value))
        return person

    def locked_out(self, person: PersonInfo, *, lost_email: bool = True) -> str | None:
        """Why the person may use nothing until they verify their email, as `locked_out`
        says, or None."""
        return locked_out(self.settings, person, self._clock(), lost_email=lost_email)

    def offers_email(self, person: PersonInfo) -> bool:
        """Whether the console offers the person a new verification email: their email
        isn't verified, and the settings name the provider's call that sends one."""
        return self.emails is not None and self.emails.offered(person)

    def session_cookie(self, person: PersonInfo) -> str:
        """A signed cookie value for this person, used by the login and by tests."""
        expires = int(self._clock().timestamp()) + SESSION_AGE
        return self._sign({"pid": person.id, "exp": expires})

    def csrf_for_session(self, value: str | None) -> str | None:
        """A form token bound to a session cookie, or None when the session is not valid."""
        data = self._read(value)
        if value is None or data is None:
            return None
        signature = hmac.new(self._secret, b"csrf:" + value.encode(), hashlib.sha256).digest()
        return _b64(signature)

    def csrf_token(self, request: Request, cookie: str = SESSION_COOKIE) -> str | None:
        """The form token for the request's session, or the cookie named, if it has one."""
        return self.csrf_for_session(request.cookies.get(cookie))

    def csrf_ok(self, request: Request, token: object, cookie: str = SESSION_COOKIE) -> bool:
        """Whether the form token belongs to the request's session, or the cookie named."""
        expected = self.csrf_token(request, cookie)
        return (
            expected is not None and isinstance(token, str) and hmac.compare_digest(expected, token)
        )

    def sign_in(self, response: Response, person: PersonInfo) -> None:
        """Put the person's session on a response."""
        response.set_cookie(
            SESSION_COOKIE,
            self.session_cookie(person),
            max_age=SESSION_AGE,
            path="/",
            httponly=True,
            samesite="lax",
            secure=self._secure,
        )
        response.delete_cookie(NEW_PERSON_COOKIE, path="/")

    def clear(self, response: Response) -> None:
        """Remove the session cookie, and the identity waiting to choose its login."""
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(NEW_PERSON_COOKIE, path="/")

    async def login(self, request: Request) -> Response:
        """Start sign-in: the development login, or the provider with state and PKCE."""
        return await self._start(request, ())

    async def sign_up(self, request: Request) -> Response:
        """Start sign-in on the provider's sign-up form, with the parameters the settings name
        for it: without them, as `login`."""
        return await self._start(request, self.settings.sign_up_params)

    async def _start(self, request: Request, extra: Iterable[tuple[str, str]]) -> Response:
        """Start sign-in, with `extra` added to the provider's authorization request."""
        next_path = safe_next(request.query_params.get("next"))
        if self.settings.auth == "dev":
            try:
                person = await anyio.to_thread.run_sync(self._dev_person)
            except ValueError as error:
                return problem(request, 403, "Sign-in refused", str(error))
            response = RedirectResponse(next_path, status_code=303)
            self.sign_in(response, person)
            return response
        if self.provider is None:
            return problem(
                request,
                401,
                "Sign-in is off",
                "This server has no sign-in provider. Use the development login, or set "
                "CLIQUET_AUTH=oidc.",
            )
        try:
            found = await self.provider.discovery()
        except ProviderError as error:
            return problem(request, 502, "Sign-in failed", error.message)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        params = {**dict(extra), **_authorize_params(self.settings, state, _challenge(verifier))}
        response = RedirectResponse(_with_query(found.authorization_endpoint, params), 302)
        oauth = {"state": state, "verifier": verifier, "next": next_path}
        self._set_cookie(response, OAUTH_COOKIE, oauth, OAUTH_AGE)
        return response

    async def callback(self, request: Request) -> Response:
        """Finish the provider's redirect: check its issuer and state, exchange the code, and
        sign in."""
        if self.provider is None:
            return RedirectResponse("/login", status_code=303)
        try:
            found = await self.provider.discovery()
        except ProviderError as error:
            return problem(request, 502, "Sign-in failed", error.message)
        # RFC 9207: an answer naming another issuer is refused. One without `iss` is taken,
        # since Cliquet knows one provider only, so no other can mix its answers in.
        answered = request.query_params.get("iss")
        if found.names_issuer and answered is not None and answered != self.provider.issuer:
            return problem(
                request,
                400,
                "Sign-in failed",
                "The sign-in answer came from another provider. Start again.",
            )
        if request.query_params.get("error"):
            return problem(
                request,
                401,
                "Sign-in failed",
                "The sign-in provider refused the sign-in. Try again.",
            )
        state = request.query_params.get("state")
        code = request.query_params.get("code")
        if not state or not code:
            return problem(
                request,
                400,
                "Sign-in failed",
                "The sign-in answer was incomplete. Start again.",
            )
        oauth = self._read(request.cookies.get(OAUTH_COOKIE))
        if oauth is None:
            return problem(
                request,
                400,
                "Sign-in failed",
                "The sign-in took too long or came from another browser. Start again.",
            )
        expected = oauth.get("state")
        verifier = oauth.get("verifier")
        if (
            not isinstance(expected, str)
            or not isinstance(verifier, str)
            or not hmac.compare_digest(expected, state)
        ):
            return problem(
                request,
                400,
                "Sign-in failed",
                "The sign-in took too long or came from another browser. Start again.",
            )
        try:
            profile = await self._profile(found, code, verifier)
        except ValueError as error:
            return problem(request, 403, "Sign-in refused", str(error))
        except httpx.HTTPError:
            return problem(
                request,
                502,
                "Sign-in failed",
                "The sign-in provider did not answer. Try again in a moment.",
            )
        # The person's email, when the provider verified it, as at the person's endpoint: one
        # that isn't verified yet, or none, signs them in with no email.
        claims = {**profile, "iss": self.provider.issuer}
        try:
            person = await anyio.to_thread.run_sync(
                person_from_claims,
                self.database,
                self.settings,
                claims,
                None,
                self._clock(),
                self.notices,
                self.retire,
            )
        except AuthRefused as refused:
            return problem(request, 403, "Sign-in refused", refused.message)
        next_path = safe_next(oauth.get("next"))
        if isinstance(person, Newcomer):
            response = RedirectResponse(CHOOSE_LOGIN_PATH, status_code=303)
            # Whoever was signed in on this browser before is signed out.
            response.delete_cookie(SESSION_COOKIE, path="/")
            new_person = {
                "iss": person.issuer,
                "sub": person.subject,
                "email": person.email,
                "next": next_path,
            }
            self._set_cookie(response, NEW_PERSON_COOKIE, new_person, NEW_PERSON_AGE)
        else:
            response = RedirectResponse(next_path, status_code=303)
            self.sign_in(response, person)
        response.delete_cookie(OAUTH_COOKIE, path="/")
        return response

    async def choose_login(self, request: Request) -> Response:
        """The page where a new person chooses their login, filled with a free suggestion
        drawn from their verified email, if they have one."""
        new_person = self._new_person(request)
        if new_person is None:
            return RedirectResponse("/login", status_code=303)
        return self._login_page(request, await self._suggestion(new_person), None, 200)

    async def take_login(self, request: Request) -> Response:
        """Make the new person with the login they chose, sign them in, and go on."""
        new_person = self._new_person(request)
        if new_person is None:
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        if not self.csrf_ok(request, form.get("csrf"), NEW_PERSON_COOKIE):
            return problem(
                request,
                403,
                "The form expired",
                "The form expired. Reload the page and try again.",
            )
        value = form.get("login")
        login = value.strip().lower() if isinstance(value, str) else ""
        try:
            made = partial(
                self.database.person_for,
                new_person["iss"],
                new_person["sub"],
                new_person["email"] or None,
                login,
                self._clock(),
                builds=self.settings.grant_build_on_sign_up,
            )
            person = await anyio.to_thread.run_sync(made)
        except LoginRefused as error:
            # A taken login is offered a free one in its place; a mistyped one is kept to mend.
            offered = login if login_problem(login) else await self._suggestion(new_person)
            return self._login_page(request, offered, str(error), 400)
        except EmailTaken:
            return problem(request, 403, "Sign-in refused", EMAIL_ELSEWHERE)
        except ValueError as error:
            return problem(request, 403, "Sign-in refused", str(error))
        await anyio.to_thread.run_sync(self.database.claim_invitations, person, self._clock())
        response = RedirectResponse(new_person["next"], status_code=303)
        self.sign_in(response, person)
        return response

    def _new_person(self, request: Request) -> dict[str, str] | None:
        """The identity waiting to choose its login, from its cookie, if it is still valid:
        its email is empty when the provider verified none."""
        data = self._read(request.cookies.get(NEW_PERSON_COOKIE))
        if data is None:
            return None
        fields = {key: data.get(key) for key in ("iss", "sub", "email", "next")}
        fields["email"] = fields["email"] or ""
        if not all(isinstance(value, str) for value in fields.values()):
            return None
        return {key: str(value) for key, value in fields.items()}

    async def _suggestion(self, new_person: dict[str, str]) -> str:
        """A free login for the new person, from their verified email, if they have one: the
        same one their assistant is offered, while it stays free."""
        seed = f"{new_person['iss']} {new_person['sub']}"
        made = partial(free_login, self.database, new_person["email"], seed)
        return await anyio.to_thread.run_sync(made)

    def _login_page(
        self, request: Request, login: str, refusal: str | None, status: int
    ) -> Response:
        csrf = self.csrf_for_session(request.cookies.get(NEW_PERSON_COOKIE))
        context = {"login": login, "refusal": refusal, "csrf": csrf, "max_login": MAX_LOGIN}
        response = render(request, "choose_login.html", context)
        response.status_code = status
        return response

    async def logout(self, request: Request) -> Response:
        """Clear the session, and the provider's when it has an end-session endpoint.

        Only a POST with the session's form token signs out, so another site can't. The
        provider is sent no ID token, since Cliquet keeps none, so some providers ask the
        person to confirm. Without that endpoint, or with the development login, a page says
        they are signed out: the home page would sign them in again at once.
        """
        form = await request.form()
        if not self.csrf_ok(request, form.get("csrf")):
            return problem(
                request,
                403,
                "The form expired",
                "The sign-out form expired. Reload the page and try again.",
            )
        end_session = await self._end_session()
        if end_session is not None:
            params = {
                "client_id": self.settings.oidc_client_id or "",
                "post_logout_redirect_uri": f"{self.settings.base_url}/",
            }
            response = RedirectResponse(_with_query(end_session, params), status_code=302)
        else:
            response = problem(
                request, 200, "Signed out", "You are signed out of Cliquet.", ("/login", "Sign in")
            )
        self.clear(response)
        return response

    async def _end_session(self) -> str | None:
        """The provider's end-session endpoint, or None, when signing out of Cliquet is all
        there is to do: without a provider, without the endpoint, or while it doesn't answer."""
        if self.provider is None:
            return None
        try:
            return (await self.provider.discovery()).end_session_endpoint
        except ProviderError:
            return None

    def _dev_person(self) -> PersonInfo:
        return development_person(self.database, self.settings, self._clock())

    async def _profile(self, found: Discovery, code: str, verifier: str) -> dict[str, Any]:
        """Exchange a code at the provider's token endpoint, then read the person from its
        userinfo endpoint."""
        assert self.provider is not None
        if found.userinfo_endpoint is None:
            raise ValueError(
                "The sign-in provider has no userinfo endpoint, which Cliquet reads who you "
                "are from. Tell the server's operator."
            )
        data, headers = self.provider.authenticated(
            found,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{self.settings.base_url}/auth/callback",
                "code_verifier": verifier,
            },
        )
        async with self.provider.client() as client:
            token_response = await client.post(found.token_endpoint, data=data, headers=headers)
            token_response.raise_for_status()
            token = token_response.json()
            access_token = token.get("access_token") if isinstance(token, dict) else None
            if not isinstance(access_token, str):
                raise ValueError("The sign-in provider did not return an access token.")
            info_response = await client.get(
                found.userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            info_response.raise_for_status()
            profile = info_response.json()
        if not isinstance(profile, dict):
            raise ValueError("The sign-in provider did not return who you are.")
        return profile

    def _set_cookie(self, response: Response, name: str, data: dict[str, Any], age: int) -> None:
        payload = {**data, "exp": int(self._clock().timestamp()) + age}
        response.set_cookie(
            name,
            self._sign(payload),
            max_age=age,
            path="/",
            httponly=True,
            samesite="lax",
            secure=self._secure,
        )

    def _sign(self, data: dict[str, Any]) -> str:
        payload = json.dumps(data, separators=(",", ":")).encode()
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return f"{_b64(payload)}.{_b64(signature)}"

    def _read(self, value: str | None) -> dict[str, Any] | None:
        """A signed cookie's data, or None when its signature is wrong or it has expired."""
        if not value:
            return None
        parts = value.split(".")
        if len(parts) != 2:
            return None
        try:
            payload = _unb64(parts[0])
            signature = _unb64(parts[1])
        except ValueError:
            return None
        expected = hmac.new(self._secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            data = json.loads(payload)
        except UnicodeDecodeError, json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        expires = data.get("exp")
        if not isinstance(expires, int) or expires <= int(self._clock().timestamp()):
            return None
        return data


def _authorize_params(settings: Settings, state: str, challenge: str) -> dict[str, str]:
    """The parameters of the provider's authorization endpoint: no audience or resource, since
    the token serves only to read who the person is."""
    return {
        "response_type": "code",
        "client_id": settings.oidc_client_id or "",
        "redirect_uri": f"{settings.base_url}/auth/callback",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }


def _with_query(address: str, params: dict[str, str]) -> str:
    """One of the provider's addresses with `params` added to whatever query it has."""
    return f"{address}{'&' if '?' in address else '?'}{urlencode(params)}"


def _challenge(verifier: str) -> str:
    """The PKCE S256 challenge for a verifier."""
    return _b64(hashlib.sha256(verifier.encode()).digest())


def safe_next(value: Any) -> str:
    """A local path to return to, or the home page."""
    if not isinstance(value, str) or not value.startswith("/"):
        return "/"
    if value.startswith("//") or "\\" in value:
        return "/"
    return value


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
