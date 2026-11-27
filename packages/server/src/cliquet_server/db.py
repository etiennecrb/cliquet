"""The server's database: which apps exist, who owns them, and where their sandbox is.

The models stay inside this module. Every method returns plain frozen dataclasses, so
nothing outside holds a database session.
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    or_,
    select,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    aliased,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.types import TypeDecorator

from cliquet.manifest import MAX_TITLE, title_problem
from cliquet.tools import MAX_APP_NAME

from . import tokens as api_tokens
from .identity import is_email, login_problem
from .manifest import ACCENT
from .migrations import migrate

# As long as the SDK lets an app be named: its tools' full names must fit an MCP client.
APP_NAME = re.compile(rf"^[a-z][a-z0-9_-]{{0,{MAX_APP_NAME - 1}}}$")
LOGIN = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}$")
# The errors of an app's code kept for its owner: how many for each app, for how long, and
# how many bytes of each one's text. So an app that fails every minute can't fill the volume.
ERRORS_KEPT = 200
ERRORS_KEPT_FOR = timedelta(days=30)
MAX_ERROR_TEXT = 16 * 1024
# How many characters of each error's text a list reads: enough for what failed.
ERROR_START = 1000
# How many of an app's errors may be failed starts, so an outage whose text changes at each
# try can't push its calls' errors out, and for how long a start that fails the same way is
# the same failure: a failed start is tried again each minute while requests come.
STARTS_KEPT = 20
SAME_START_FOR = timedelta(days=1)


class LoginRefused(ValueError):
    """A login that breaks the rules a chosen one follows."""


class LoginTaken(LoginRefused):
    """Someone already has this login."""


class AccountDeleted(LoginTaken):
    """This login's account was deleted: the login is kept, and nobody may use it again."""


class EmailTaken(ValueError):
    """Another person already has the email the sign-in provider verified, so this identity
    can't take it."""


class AppExists(Exception):
    """This owner already has an app of this name."""


class UtcDateTime(TypeDecorator[datetime]):
    """A time kept in UTC. SQLite drops the offset, so it is put back on the way out."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("a time needs its offset")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class AppInfo:
    login: str
    name: str
    description: str
    created_at: datetime
    published_at: datetime | None
    # The app's accent and icon from its manifest at publish: an accent is `#rrggbb`, or None
    # for Cliquet's own colour, and an icon one of Lucide's names, or None for the letter.
    accent: str | None
    icon: str | None
    # The app's name as people read it, from its manifest at publish, or None for its name.
    title: str | None


@dataclass(frozen=True)
class SandboxInfo:
    login: str
    name: str
    url: str
    token: str
    sdk: str
    ready_at: datetime | None


class Right(StrEnum):
    """What Cliquet lets a person do across the server, as opposed to an app's roles, which
    decide what they may do inside one app."""

    BUILD = "build"
    # A larger monthly sandbox budget: see `sandbox_time`.
    PLUS = "plus"


@dataclass(frozen=True)
class PersonInfo:
    id: int
    issuer: str | None
    subject: str | None
    email: str | None
    login: str
    created_at: datetime
    rights: frozenset[Right]
    # Whether the sign-in provider verified the person's email: until then they have no email,
    # and claim nothing by one. Once verified, they keep no email when another account has it.
    email_verified: bool
    # When the account was deleted: its login is kept, and nothing else of it.
    deleted_at: datetime | None = None


@dataclass(frozen=True)
class Forgotten:
    """What deleting an account forgot of the person's identity, so the operator can delete
    them at the sign-in provider too: its issuer and subject, and their email. Each is None
    when there was none, or when an earlier run of the deletion forgot it already."""

    issuer: str | None
    subject: str | None
    email: str | None


def may(person: PersonInfo, right: Right) -> bool:
    """Whether Cliquet grants this person the right: the one check every gate makes."""
    return right in person.rights


@dataclass(frozen=True)
class MembershipInfo:
    person: PersonInfo
    app: AppInfo
    role: str
    created_at: datetime


# What sharing a role did: shared it with the email, whether that made a share to accept, an
# invitation or nothing new, or nothing, for the owner's own email.
type Shared = Literal["shared", "owner"]


@dataclass(frozen=True)
class PendingShare:
    """A share its person hasn't accepted yet: only what Cliquet knows itself, never what the
    app's author wrote, such as its description."""

    login: str
    name: str
    role: str
    created_at: datetime


@dataclass(frozen=True)
class WaitingShare:
    """A share of an app that its person hasn't accepted yet: the email the owner shared it
    with, the only thing the owner may see of it, and the person it waits for."""

    email: str
    person: PersonInfo
    role: str
    created_at: datetime


@dataclass(frozen=True)
class TokenLimit:
    """One app a limited token works for, and its one role there. `-` is an app without
    roles."""

    login: str
    name: str
    role: str


@dataclass(frozen=True)
class TokenInfo:
    """An API token, as its person sees it in the list: never the token itself. A token
    without limits works everywhere its person may go; one with limits only for their apps,
    one role in each, in the order of their names."""

    id: int
    name: str
    created_at: datetime
    last_used_at: datetime | None
    limits: tuple[TokenLimit, ...] | None
    # The id the apps it reaches see, never the token itself.
    public_id: str
    # The name of the token that made it, if a token did.
    made_by: str | None
    # When it stops working: None for one that never does.
    expires_at: datetime | None

    def expired(self, now: datetime) -> bool:
        """Whether it no longer works, at `now`."""
        return self.expires_at is not None and self.expires_at <= now


class TokenExpired(Exception):
    """A token that expired `at`: it acts as nobody. What it would give, its person, its
    limits and its name and id, tells the owner of an app it is limited to that an agent
    still uses it."""

    def __init__(
        self,
        person: PersonInfo,
        limits: tuple[TokenLimit, ...] | None,
        token: api_tokens.ApiToken,
        at: datetime,
    ) -> None:
        super().__init__(f"the token {token.name!r} expired")
        self.person, self.limits, self.token, self.at = person, limits, token, at


class MakerExpiring(ValueError):
    """The token that would make a token expires `at`, sooner than the shortest lifetime:
    `app_auth.maker_expiring` says it, with where to make another."""

    def __init__(self, at: datetime) -> None:
        super().__init__(f"the token expires on {api_tokens.expiry(at)}")
        self.at = at


@dataclass(frozen=True)
class InvitationInfo:
    app: AppInfo
    email: str
    role: str
    created_at: datetime


@dataclass(frozen=True)
class ErrorInfo:
    """An error of an app's code, as the app's owner reads it: its id, when it happened, who
    called which tool with which role, None in an app without roles, and its text with its
    traceback, or the start of it in a list. Never the call's arguments.

    An app's failed start has no caller, role or tool: nobody's call was made.
    """

    id: str
    at: datetime
    caller: str | None
    role: str | None
    tool: str | None
    text: str

    @property
    def start(self) -> bool:
        """Whether it is the app's failed start, rather than a call its code failed."""
        return self.tool is None


class Base(DeclarativeBase):
    pass


class _Person(Base):
    __tablename__ = "people"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issuer: Mapped[str | None] = mapped_column(String(500), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, nullable=True)
    login: Mapped[str] = mapped_column(String(39), unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    # The names of the rights Cliquet grants the person, a sorted JSON list.
    rights: Mapped[str] = mapped_column(Text, default="[]")
    # Whether the sign-in provider verified their email, as `PersonInfo` says. A person the
    # operator makes is vouched for, and claimed only by a verified email.
    email_verified: Mapped[bool] = mapped_column(Boolean, default=True)
    # When their apps or the builder were last used, to warm their sandbox at boot.
    apps_last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # When their account was deleted. The row stays, so the login is reserved for ever and the
    # id, which a session holds, is never given to anyone else.
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class _App(Base):
    __tablename__ = "apps"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("people.id"))
    name: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    look: Mapped[_AppLook | None] = relationship(lazy="joined", cascade="all, delete-orphan")
    titled: Mapped[_AppTitle | None] = relationship(lazy="joined", cascade="all, delete-orphan")


class _AppLook(Base):
    """How an app's tile looks, from its manifest at publish. An app published before it was
    kept has no row, and gets one when it next starts."""

    __tablename__ = "app_looks"

    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id"), primary_key=True)
    accent: Mapped[str | None] = mapped_column(String(7))
    icon: Mapped[str | None] = mapped_column(String(64))


class _AppTitle(Base):
    """An app's name as people read it, from its manifest at publish. An app without a row
    reads as its name."""

    __tablename__ = "app_titles"

    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id"), primary_key=True)
    title: Mapped[str] = mapped_column(String(MAX_TITLE))


class _Sandbox(Base):
    __tablename__ = "sandboxes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("people.id"), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(500))
    token: Mapped[str] = mapped_column(String(500))
    sdk: Mapped[str] = mapped_column(String(64))
    ready_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class _Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("person_id", "app_id", "role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"))
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id"))
    role: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class _Share(Base):
    """A role shared with a known person, waiting for them to accept it. It gives no access
    until it becomes a membership."""

    __tablename__ = "shares"
    __table_args__ = (UniqueConstraint("person_id", "app_id", "role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"))
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id"))
    role: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    # The email the owner shared it with, which the person's own may no longer be.
    email: Mapped[str] = mapped_column(String(320))


class _Token(Base):
    """An API token: it acts as its person. Only its hash is kept. A limited one works only
    for the apps of its limits: with none left, it works nowhere."""

    __tablename__ = "tokens"
    __table_args__ = (UniqueConstraint("person_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"))
    name: Mapped[str] = mapped_column(String(api_tokens.MAX_NAME))
    hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    limited: Mapped[bool] = mapped_column(Boolean, default=False)
    # What the apps it reaches see of it: random, so it tells nothing of the other tokens.
    public_id: Mapped[str] = mapped_column(String(20), unique=True)
    # The token that made this one, if a token did, not the console or a sign-in: this one
    # goes when that one is revoked.
    made_by: Mapped[int | None] = mapped_column(ForeignKey("tokens.id", name="tokens_made_by_fkey"))
    # When it stops working: None for one its person made to never expire, and for every
    # token made before tokens expired.
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class _TokenLimit(Base):
    """One app a limited token works for, and its one role there."""

    __tablename__ = "token_limits"

    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(40))


class _AppRoles(Base):
    """The roles an app's live version declares, in its order, kept at each publish: so a list
    of the owner's apps, who holds them all, needs no start."""

    __tablename__ = "app_roles"

    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id"), primary_key=True)
    roles: Mapped[str] = mapped_column(Text)


class _Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = (UniqueConstraint("app_id", "email", "role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id"))
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class _AppError(Base):
    """An error of an app's code, in a call to the published app or in its start, kept for
    the app's owner. Its text may hold the app's data, so only the owner reads it. A start
    has no caller and no tool."""

    __tablename__ = "app_errors"
    __table_args__ = (UniqueConstraint("app_id", "error_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id"))
    error_id: Mapped[str] = mapped_column(String(40))
    at: Mapped[datetime] = mapped_column(UtcDateTime)
    caller: Mapped[str | None] = mapped_column(String(39), nullable=True)
    role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tool: Mapped[str | None] = mapped_column(String(100), nullable=True)
    text: Mapped[str] = mapped_column(Text)


class _SandboxTime(Base):
    """How long an owner's sandbox was busy in one calendar month, in UTC, in whole seconds:
    kept for this month and the one before, only while a budget is set. A deleted account's
    stays until it is that old, so the whole server's count never drops."""

    __tablename__ = "sandbox_time"
    __table_args__ = (PrimaryKeyConstraint("person_id", "month", name="sandbox_time_pkey"),)

    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.id", name="sandbox_time_person_id_fkey")
    )
    # Such as 2026-11.
    month: Mapped[str] = mapped_column(String(7))
    seconds: Mapped[int] = mapped_column(Integer)


# The people whose account wasn't deleted: the only ones a lookup finds.
_LIVE = _Person.deleted_at.is_(None)

# An `ErrorInfo`'s fields before its text, in their order.
_ERROR_FIELDS = (
    _AppError.error_id,
    _AppError.at,
    _AppError.caller,
    _AppError.role,
    _AppError.tool,
)


def open_database(data_dir: Path, url: str) -> Database:
    """Make the data folder and open the server's database, migrated to the newest
    revision."""
    data_dir.mkdir(parents=True, exist_ok=True)
    return Database(url)


class Database:
    """Which apps exist, who owns them, and where each owner's sandbox is."""

    def __init__(self, url: str) -> None:
        """Open the database at `url`, migrated to the newest revision. Raises
        `migrations.DatabaseRefused` for one this server can't migrate."""
        migrate(url)
        self._engine = create_engine(url)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)

    def create_app(
        self, login: str, name: str, now: datetime, *, email: str | None = None
    ) -> AppInfo:
        """Make an app for `login`, with no description and no publish yet.

        With `email`, a new owner, or one who has not signed in yet, waits for that email.
        """
        _aware(now)
        if not LOGIN.fullmatch(login):
            raise ValueError(
                f"a login is lower case letters, digits and hyphens: {login!r} is not one"
            )
        if not APP_NAME.fullmatch(name):
            raise ValueError(
                f"an app's name is at most {MAX_APP_NAME} lower case letters, digits, - or _, "
                f"starting with a letter: {name!r} is not one"
            )
        wanted = _email(email) if email is not None else None
        with self._sessions.begin() as session:
            owner = _person_for_login(session, login, now, wanted)
            found = session.scalar(select(_App).where(_App.owner_id == owner.id, _App.name == name))
            if found is not None:
                raise AppExists(f"{login} already has an app called {name}")
            session.add(_App(owner_id=owner.id, name=name, description="", created_at=now))
        return AppInfo(login, name, "", now, None, None, None, None)

    def app(self, login: str, name: str) -> AppInfo | None:
        """The app, or None when this owner has no such app."""
        with self._sessions.begin() as session:
            found = session.execute(
                select(_App, _Person.login)
                .join(_Person)
                .where(_Person.login == login, _App.name == name)
            ).one_or_none()
            return _info(*found) if found is not None else None

    def apps(self, login: str | None = None, *, published: bool = False) -> list[AppInfo]:
        """Every app, sorted by login then name; only one owner's, or only published ones."""
        query = select(_App, _Person.login).join(_Person).order_by(_Person.login, _App.name)
        if login is not None:
            query = query.where(_Person.login == login)
        if published:
            query = query.where(_App.published_at.is_not(None))
        with self._sessions.begin() as session:
            return [_info(row, owner) for row, owner in session.execute(query)]

    def keep_roles(self, login: str, name: str, roles: list[str]) -> None:
        """Keep the roles the app's live version declares, if there is such an app."""
        for attempt in range(2):
            try:
                with self._sessions.begin() as session:
                    app = _app_row(session, login, name)
                    if app is None:
                        return
                    row = session.get(_AppRoles, app.id)
                    if row is None:
                        session.add(_AppRoles(app_id=app.id, roles=json.dumps(roles)))
                    else:
                        row.roles = json.dumps(roles)
                return
            except IntegrityError:
                # Another start or publish added the row meanwhile: replace it.
                if attempt:
                    raise

    def kept_roles(self, login: str, name: str) -> list[str] | None:
        """The roles kept for the app, or None for one published before they were kept."""
        with self._sessions.begin() as session:
            app = _app_row(session, login, name)
            kept = session.get(_AppRoles, app.id) if app is not None else None
            return json.loads(kept.roles) if kept is not None else None

    def mark_published(
        self,
        login: str,
        name: str,
        now: datetime,
        description: str = "",
        *,
        accent: str | None = None,
        icon: str | None = None,
        title: str | None = None,
    ) -> None:
        """Keep the first publish time, this description, cut to 500 characters, and the
        look: the accent, which must be `#rrggbb` or None, the icon's name or None, and the
        app's name as people read it or None."""
        _aware(now)
        with self._sessions.begin() as session:
            row = _app_row(session, login, name)
            if row is None:
                raise ValueError(f"{login} has no app called {name}")
            if row.published_at is None:
                row.published_at = now
            row.description = description[:500]
            _set_look(row, accent, icon, title)

    def set_look(
        self, login: str, name: str, accent: str | None, icon: str | None, title: str | None
    ) -> None:
        """Keep the app's accent, which must be `#rrggbb` or None, its icon's name, and its
        name as people read it."""
        with self._sessions.begin() as session:
            row = _app_row(session, login, name)
            if row is None:
                raise ValueError(f"{login} has no app called {name}")
            _set_look(row, accent, icon, title)

    def delete_app(self, login: str, name: str) -> None:
        """Remove the app, if there is one."""
        with self._sessions.begin() as session:
            row = session.scalar(
                select(_App).join(_Person).where(_Person.login == login, _App.name == name)
            )
            if row is not None:
                session.execute(delete(_Membership).where(_Membership.app_id == row.id))
                session.execute(delete(_Share).where(_Share.app_id == row.id))
                session.execute(delete(_Invitation).where(_Invitation.app_id == row.id))
                # A token limited to the app loses that limit, so it never reaches an app made
                # later with the same name. Limited still, it keeps its other apps, or none.
                session.execute(delete(_TokenLimit).where(_TokenLimit.app_id == row.id))
                session.execute(delete(_AppError).where(_AppError.app_id == row.id))
                session.execute(delete(_AppRoles).where(_AppRoles.app_id == row.id))
                session.delete(row)

    def person_for(
        self,
        issuer: str,
        subject: str,
        email: str | None,
        login: str,
        now: datetime,
        *,
        builds: bool = False,
    ) -> PersonInfo:
        """Find the person for an identity, as `known_person` does, or make them with `login`
        and `email`, the one the sign-in provider verified. With none, the person has no email
        until it verifies one, and claims nothing by it.

        The first person the server ever has may build, and so does every new one when
        `builds`.

        Raises `LoginRefused` when the login breaks the rules a chosen one follows,
        `LoginTaken` when someone else has it, and `AccountDeleted` when it was a deleted
        account's, with the same words, so a stranger never learns of the deletion. Raises
        `EmailTaken` as `known_person` does.
        """
        _aware(now)
        email = _email(email) if email is not None else None
        login = login.strip().lower()
        taken = LoginTaken(f"Someone already has the login {login!r}. Choose another one.")
        try:
            with self._sessions.begin() as session:
                person = _known_person(session, issuer, subject, email, now)
                if person is not None:
                    return _person_info(person)
                refusal = login_problem(login)
                if refusal is not None:
                    raise LoginRefused(refusal)
                holder = session.scalar(select(_Person).where(_Person.login == login))
                if holder is not None and holder.deleted_at is not None:
                    raise AccountDeleted(str(taken))
                if holder is not None:
                    raise taken
                # The first person a server ever has is the one who set it up: they may build,
                # with no command to run. A deleted account's row counts, so it happens once.
                first = session.scalar(select(_Person.id).limit(1)) is None
                person = _Person(
                    issuer=issuer,
                    subject=subject,
                    email=email,
                    login=login,
                    created_at=now,
                    rights=json.dumps([Right.BUILD] if first or builds else []),
                    email_verified=email is not None,
                )
                session.add(person)
                session.flush()
                return _person_info(person)
        except IntegrityError:
            # Another request made this person, or took this login, meanwhile.
            known = self.known_person(issuer, subject, email, now)
            if known is None:
                raise taken from None
            return known

    def known_person(
        self, issuer: str, subject: str, email: str | None, now: datetime
    ) -> PersonInfo | None:
        """The person for an identity, or None when it is new. With `email`, the one the
        sign-in provider verified, it is kept up to date, and the person's email is verified.

        An imported owner with no identity yet is claimed by the email saved for them, and so
        is a person whose identity a provider move cleared. A new identity has no person until
        it chooses a login, with `person_for`. A person with no email whose verified one
        another account has keeps none, unless that account waits for it and theirs holds
        nothing: they are then that account from now on, theirs closed, as `_known_person`
        says.
        """
        _aware(now)
        email = _email(email) if email is not None else None
        with self._sessions.begin() as session:
            person = _known_person(session, issuer, subject, email, now)
            return _person_info(person) if person is not None else None

    def had_people(self) -> bool:
        """Whether the server has ever had a person, a deleted account included: if not, the
        next one made may build."""
        with self._sessions.begin() as session:
            return session.scalar(select(_Person.id).limit(1)) is not None

    def person_by_login(self, login: str) -> PersonInfo | None:
        """The person with this login, if any and their account wasn't deleted."""
        with self._sessions.begin() as session:
            person = session.scalar(select(_Person).where(_Person.login == login, _LIVE))
            return _person_info(person) if person is not None else None

    def account(self, login: str) -> PersonInfo | None:
        """Whoever has or had this login, a deleted account too, or None when nobody did."""
        with self._sessions.begin() as session:
            person = session.scalar(select(_Person).where(_Person.login == login))
            return _person_info(person) if person is not None else None

    def deleted_logins(self) -> list[str]:
        """The logins of every deleted account, sorted."""
        with self._sessions.begin() as session:
            query = select(_Person.login).where(_Person.deleted_at.is_not(None))
            return list(session.scalars(query.order_by(_Person.login)))

    def deleted_logins_with_rows(self) -> list[str]:
        """The logins of the deleted accounts that still have an app or a sandbox row, which a
        stopped deletion left, sorted."""
        with self._sessions.begin() as session:
            app = select(_App.id).where(_App.owner_id == _Person.id).exists()
            sandbox = select(_Sandbox.id).where(_Sandbox.owner_id == _Person.id).exists()
            query = select(_Person.login).where(_Person.deleted_at.is_not(None), or_(app, sandbox))
            return list(session.scalars(query.order_by(_Person.login)))

    def close_account(self, login: str, now: datetime) -> Forgotten | None:
        """Close the account with this login, as the first step of deleting it, and say what
        was forgotten of its identity. None when nobody ever had the login.

        In one transaction: the person's tokens, memberships and shares go, the invitations
        for their email, and everyone's access to the apps they own, so nobody reaches those
        apps while their files are removed. Their identity, email, rights and last use are
        cleared, and the row is kept, deleted, as a tombstone that reserves the login. Safe to
        run again: it then forgets nothing more.
        """
        _aware(now)
        with self._sessions.begin() as session:
            person = session.scalar(select(_Person).where(_Person.login == login))
            return _close(session, person, now) if person is not None else None

    def grant(self, login: str, right: Right) -> PersonInfo:
        """Grant the person with this login a right, kept until it is withdrawn."""
        return self._set_right(login, right, held=True)

    def withdraw(self, login: str, right: Right) -> PersonInfo:
        """Withdraw a right from the person with this login, if they hold it."""
        return self._set_right(login, right, held=False)

    def logins_with(self, right: Right) -> frozenset[str]:
        """The logins of the people who hold a right, deleted accounts left out."""
        with self._sessions.begin() as session:
            people = session.scalars(select(_Person).where(_LIVE))
            return frozenset(person.login for person in people if may(_person_info(person), right))

    def _set_right(self, login: str, right: Right, *, held: bool) -> PersonInfo:
        """Grant or withdraw a right. Raises ValueError when nobody has this login."""
        with self._sessions.begin() as session:
            person = session.scalar(select(_Person).where(_Person.login == login))
            if person is None:
                raise ValueError(
                    f"Nobody has the login {login!r} on this server: check it, or wait until "
                    "they have signed in and chosen it."
                )
            if person.deleted_at is not None:
                raise AccountDeleted(_deleted(login))
            rights = _person_info(person).rights
            person.rights = json.dumps(sorted(rights | {right} if held else rights - {right}))
            return _person_info(person)

    def person_by_email(self, email: str) -> PersonInfo | None:
        """The person with this email, if any."""
        with self._sessions.begin() as session:
            wanted = _email(email)
            person = session.scalar(select(_Person).where(_Person.email == wanted, _LIVE))
            return _person_info(person) if person is not None else None

    def person_by_id(self, person_id: int) -> PersonInfo | None:
        """The person with this id, if any and their account wasn't deleted."""
        with self._sessions.begin() as session:
            person = session.get(_Person, person_id)
            if person is None or person.deleted_at is not None:
                return None
            return _person_info(person)

    def person_by_identity(self, issuer: str, subject: str) -> PersonInfo | None:
        """The person with this provider identity, if any and their account wasn't deleted."""
        with self._sessions.begin() as session:
            person = session.scalar(
                select(_Person).where(_Person.issuer == issuer, _Person.subject == subject, _LIVE)
            )
            return _person_info(person) if person is not None else None

    def memberships(self, person_id: int) -> list[MembershipInfo]:
        """Every membership for one person, sorted by app."""
        with self._sessions.begin() as session:
            member, owner = aliased(_Person), aliased(_Person)
            rows = session.execute(
                select(_Membership, member, _App, owner)
                .join(member, _Membership.person_id == member.id)
                .join(_App, _Membership.app_id == _App.id)
                .join(owner, _App.owner_id == owner.id)
                .where(_Membership.person_id == person_id)
                .order_by(owner.login, _App.name, _Membership.role)
            )
            return [_membership_info(*row) for row in rows]

    def may_use(self, login: str, app_login: str, app_name: str, role: str) -> bool:
        """True when the person owns the app or holds this exact role."""
        with self._sessions.begin() as session:
            app = session.execute(
                select(_App, _Person)
                .join(_Person)
                .where(_Person.login == app_login, _App.name == app_name)
            ).one_or_none()
            if app is None:
                return False
            row, owner = app
            if owner.login == login:
                return True
            person = session.scalar(select(_Person).where(_Person.login == login))
            if person is None:
                return False
            return _membership(session, person.id, row.id, role) is not None

    def may_open(self, login: str, app_login: str, app_name: str) -> bool:
        """True when the person owns the app or is a member of it."""
        with self._sessions.begin() as session:
            app = session.execute(
                select(_App, _Person)
                .join(_Person)
                .where(_Person.login == app_login, _App.name == app_name)
            ).one_or_none()
            if app is None:
                return False
            row, owner = app
            if owner.login == login:
                return True
            person = session.scalar(select(_Person).where(_Person.login == login))
            if person is None:
                return False
            found = session.scalar(
                select(_Membership).where(
                    _Membership.person_id == person.id,
                    _Membership.app_id == row.id,
                )
            )
            return found is not None

    def visible_apps(self, login: str) -> list[AppInfo]:
        """Apps the person owns or is a member of, sorted by owner and name."""
        with self._sessions.begin() as session:
            owned = session.execute(
                select(_App, _Person).join(_Person).order_by(_Person.login, _App.name)
            )
            person = session.scalar(select(_Person).where(_Person.login == login))
            member_ids: set[int] = set()
            if person is not None:
                member_ids = set(
                    session.scalars(
                        select(_Membership.app_id).where(_Membership.person_id == person.id)
                    )
                )
            found = [
                _info(row, owner.login)
                for row, owner in owned
                if owner.login == login or row.id in member_ids
            ]
            return found

    def members(self, app_login: str, app_name: str) -> list[MembershipInfo]:
        """Members and their roles for an app."""
        with self._sessions.begin() as session:
            app = _app_row(session, app_login, app_name)
            if app is None:
                raise ValueError(f"there is no app {app_login}/{app_name}")
            member, owner = aliased(_Person), aliased(_Person)
            rows = session.execute(
                select(_Membership, member, _App, owner)
                .join(member, _Membership.person_id == member.id)
                .join(_App, _Membership.app_id == _App.id)
                .join(owner, _App.owner_id == owner.id)
                .where(_App.id == app.id)
                .order_by(member.login, _Membership.role)
            )
            return [_membership_info(*row) for row in rows]

    def share(
        self,
        owner_login: str,
        app_name: str,
        email: str,
        role: str,
        now: datetime,
    ) -> Shared:
        """Share a role with an email: a known person gets a share to accept, and an unknown
        email an invitation. Nothing changes for the owner, which says so, or for a role the
        person holds or that already waits for them. Which of these happened is never said:
        until a share is accepted, its owner knows only the email."""
        _aware(now)
        email = _email(email)
        role = _role(role)
        with self._sessions.begin() as session:
            app = _owned_app(session, owner_login, app_name)
            if email == _owner_email(session, app):
                return "owner"
            person = session.scalar(select(_Person).where(_Person.email == email))
            if person is None:
                _add_invitation(session, app, email, role, now)
                return "shared"
            if person.login == owner_login:
                return "owner"
            _add_share(session, person.id, app.id, email, role, now)
            session.execute(
                delete(_Invitation).where(
                    _Invitation.app_id == app.id,
                    _Invitation.email == email,
                    _Invitation.role == role,
                )
            )
            return "shared"

    def accept_share(
        self, person_id: int, app_login: str, app_name: str, now: datetime
    ) -> list[str]:
        """Accept every role of a published app shared with this person, and return them: none
        when nothing waits for them there. A share of an app not published yet isn't shown, so
        it can't be accepted either."""
        _aware(now)
        with self._sessions.begin() as session:
            app = _app_row(session, app_login, app_name)
            if app is None or app.published_at is None:
                return []
            shares = session.scalars(
                select(_Share)
                .where(_Share.person_id == person_id, _Share.app_id == app.id)
                .order_by(_Share.role)
            ).all()
            for share in shares:
                if _membership(session, person_id, app.id, share.role) is None:
                    session.add(
                        _Membership(
                            person_id=person_id, app_id=app.id, role=share.role, created_at=now
                        )
                    )
                session.delete(share)
            return [share.role for share in shares]

    def decline_share(self, person_id: int, app_login: str, app_name: str) -> bool:
        """Drop every role of an app shared with this person. True when one waited."""
        with self._sessions.begin() as session:
            app = _app_row(session, app_login, app_name)
            if app is None:
                return False
            dropped = session.execute(
                delete(_Share).where(_Share.person_id == person_id, _Share.app_id == app.id)
            )
            return bool(getattr(dropped, "rowcount", 0))

    def pending_shares(self, person_id: int) -> list[PendingShare]:
        """The shares of published apps waiting for this person to accept them, by app."""
        with self._sessions.begin() as session:
            rows = session.execute(
                select(_Share, _App, _Person)
                .join(_App, _Share.app_id == _App.id)
                .join(_Person, _App.owner_id == _Person.id)
                .where(_Share.person_id == person_id, _App.published_at.is_not(None))
                .order_by(_Person.login, _App.name, _Share.role)
            )
            return [
                PendingShare(owner.login, app.name, share.role, share.created_at)
                for share, app, owner in rows
            ]

    def waiting_shares(self, app_login: str, app_name: str) -> list[WaitingShare]:
        """The shares of an app that their people haven't accepted yet, by email and role."""
        with self._sessions.begin() as session:
            app = _app_row(session, app_login, app_name)
            if app is None:
                raise ValueError(f"there is no app {app_login}/{app_name}")
            rows = session.execute(
                select(_Share, _Person)
                .join(_Person, _Share.person_id == _Person.id)
                .where(_Share.app_id == app.id)
                .order_by(_Share.email, _Share.role)
            )
            return [
                WaitingShare(share.email, _person_info(person), share.role, share.created_at)
                for share, person in rows
            ]

    def invite(
        self,
        owner_login: str,
        app_name: str,
        email: str,
        role: str,
        now: datetime,
    ) -> None:
        """Leave an invitation for an email, whether or not it is known yet."""
        _aware(now)
        email = _email(email)
        role = _role(role)
        with self._sessions.begin() as session:
            app = _owned_app(session, owner_login, app_name)
            _add_invitation(session, app, email, role, now)

    def pending_invitations(self, app_login: str, app_name: str) -> list[InvitationInfo]:
        """Invitations waiting for this app, sorted by email and role."""
        with self._sessions.begin() as session:
            app = _app_row(session, app_login, app_name)
            if app is None:
                raise ValueError(f"there is no app {app_login}/{app_name}")
            rows = session.execute(
                select(_Invitation, _App, _Person)
                .join(_App, _Invitation.app_id == _App.id)
                .join(_Person, _App.owner_id == _Person.id)
                .where(_App.id == app.id)
                .order_by(_Invitation.email, _Invitation.role)
            )
            return [
                InvitationInfo(_info(app_row, owner.login), row.email, row.role, row.created_at)
                for row, app_row, owner in rows
            ]

    def claim_invitations(self, person: PersonInfo, now: datetime) -> list[PendingShare]:
        """Turn this person's invitations into shares for them to accept; safe to call
        twice."""
        _aware(now)
        if person.email is None:
            return []
        with self._sessions.begin() as session:
            rows = session.execute(
                select(_Invitation, _App, _Person)
                .join(_App, _Invitation.app_id == _App.id)
                .join(_Person, _App.owner_id == _Person.id)
                .where(_Invitation.email == person.email)
            ).all()
            claimed: list[PendingShare] = []
            for invitation, app_row, owner in rows:
                if owner.id != person.id:
                    _add_share(
                        session, person.id, app_row.id, invitation.email, invitation.role, now
                    )
                    claimed.append(PendingShare(owner.login, app_row.name, invitation.role, now))
                session.delete(invitation)
            return claimed

    def remove_member(
        self, owner_login: str, app_name: str, person_id: int, role: str | None = None
    ) -> None:
        """Remove one person's memberships and the shares waiting for them, or only their
        membership as `role`; the owner cannot remove themselves."""
        with self._sessions.begin() as session:
            app = _owned_app(session, owner_login, app_name)
            person = session.get(_Person, person_id)
            if person is None:
                return
            if person.login == owner_login:
                raise ValueError("the owner cannot be removed from their own app")
            held = select(_Membership).where(
                _Membership.app_id == app.id, _Membership.person_id == person_id
            )
            if role is not None:
                held = held.where(_Membership.role == role)
            for row in session.scalars(held).all():
                session.delete(row)
            if role is None:
                session.execute(
                    delete(_Share).where(_Share.app_id == app.id, _Share.person_id == person_id)
                )

    def remove_share(self, owner_login: str, app_name: str, person_id: int, role: str) -> None:
        """Remove a share its person hasn't accepted yet."""
        with self._sessions.begin() as session:
            app = _owned_app(session, owner_login, app_name)
            session.execute(
                delete(_Share).where(
                    _Share.app_id == app.id, _Share.person_id == person_id, _Share.role == role
                )
            )

    def remove_invitation(self, owner_login: str, app_name: str, email: str, role: str) -> None:
        """Remove a pending invitation."""
        email = _email(email)
        with self._sessions.begin() as session:
            app = _owned_app(session, owner_login, app_name)
            session.execute(
                delete(_Invitation).where(
                    _Invitation.app_id == app.id,
                    _Invitation.email == email,
                    _Invitation.role == role,
                )
            )

    def create_token(
        self,
        person_id: int,
        name: str,
        now: datetime,
        limits: Sequence[TokenLimit] | None = None,
        made_by: str | None = None,
        days: int | None = api_tokens.DEFAULT_DAYS,
    ) -> tuple[TokenInfo, str]:
        """Make a named token for a person, and return it with the token itself, which is
        never kept. With `limits`, it works only on those apps' endpoints, each as its role:
        one the person holds, or any for the app's owner, whose roles the caller checks
        against the app. It works for `days`, one of `LIFETIMES`, or for good with None.

        With `made_by`, the public id of the person's token that makes it, it names that
        token, goes when that one is revoked, and never outlives it: `days` None is then the
        default lifetime, cut to end with its maker's.

        Raises `ValueError` for a name that isn't one, one already used, no limit, two for
        one app, a limit the person may not have, a lifetime that isn't one, or a token made
        by a token that is gone, is limited, expires too soon, would make one for everywhere
        or one that outlives it: `MakerExpiring` when it expires within the shortest
        lifetime."""
        _aware(now)
        if limits is None and made_by is not None:
            raise ValueError(
                "A token made with a token works only for the apps you choose: limit it to them."
            )
        if days is not None and days not in api_tokens.LIFETIMES:
            raise ValueError(f"A token works for {api_tokens.lifetimes()}: choose one.")
        token = api_tokens.new_token()
        with self._sessions.begin() as session:
            # In the same transaction as the new token: one made while its maker is revoked
            # never outlives it.
            parent = _maker(session, person_id, made_by, now) if made_by is not None else None
            expires_at = _expires_at(now, days, parent)
            name, limits, apps = _new_token(session, person_id, name, limits, now)
            row = _Token(
                person_id=person_id,
                name=name,
                hash=api_tokens.digest(token),
                public_id=api_tokens.new_id(),
                created_at=now,
                limited=limits is not None,
                made_by=parent.id if parent is not None else None,
                expires_at=expires_at,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError:
                # The same name, made by another request at the same moment.
                raise ValueError(
                    f"You already have a token named {name!r}: choose another name."
                ) from None
            for app, limit in apps:
                session.add(_TokenLimit(token_id=row.id, app_id=app.id, role=limit.role))
            maker = parent.name if parent is not None else None
            return _token_info(row, limits, maker), token

    def check_token(
        self, person_id: int, name: str, limits: Sequence[TokenLimit], now: datetime
    ) -> str:
        """Check a limited token as `create_token` would, without making it, and return its
        name, trimmed. Raises `ValueError` as `create_token` does."""
        _aware(now)
        with self._sessions.begin() as session:
            return _new_token(session, person_id, name, limits, now)[0]

    def tokens(self, person_id: int) -> list[TokenInfo]:
        """A person's tokens, newest first, each with the name of the token that made it."""
        with self._sessions.begin() as session:
            rows = session.scalars(
                select(_Token)
                .where(_Token.person_id == person_id)
                .order_by(_Token.created_at.desc(), _Token.id.desc())
            ).all()
            limits = _limits_of(session, [row.id for row in rows if row.limited])
            # A token's maker is the same person's: it is in this list.
            names = {row.id: row.name for row in rows}
            return [
                _token_info(
                    row, limits.get(row.id, ()) if row.limited else None, names.get(row.made_by)
                )
                for row in rows
            ]

    def find_token(self, person_id: int, public_id: str) -> tuple[int, str] | None:
        """The id and name of the person's token with this public id, if they have one."""
        with self._sessions.begin() as session:
            found = session.execute(
                select(_Token.id, _Token.name).where(
                    _Token.person_id == person_id, _Token.public_id == public_id
                )
            ).one_or_none()
            return None if found is None else (found[0], found[1])

    def revoke_token(self, person_id: int, token_id: int) -> list[str] | None:
        """Revoke one of a person's tokens, and the tokens it made, at once, and give the names
        of those it made: None when there was no such token."""
        with self._sessions.begin() as session:
            row = session.get(_Token, token_id)
            if row is None or row.person_id != person_id:
                return None
            # One level deep: a token made by a token is limited, and a limited token makes
            # none, as `create_token` refuses, so the tokens it made made none in turn.
            made = session.execute(
                select(_Token.id, _Token.name).where(
                    _Token.person_id == person_id, _Token.made_by == row.id
                )
            ).all()
            gone = [*(one.id for one in made), row.id]
            session.execute(delete(_TokenLimit).where(_TokenLimit.token_id.in_(gone)))
            session.execute(delete(_Token).where(_Token.id.in_(gone)))
            return [one.name for one in made]

    def token_access(
        self, token: str, now: datetime
    ) -> tuple[PersonInfo, tuple[TokenLimit, ...] | None, api_tokens.ApiToken] | None:
        """The person a token acts as, the apps and roles it is limited to, None for
        everywhere, and its name and id, or None when it is no token of theirs. Its last use is
        kept to the minute. Raises `TokenExpired` for one that expired, whose use isn't kept."""
        _aware(now)
        if not api_tokens.is_token(token):
            return None
        wanted = api_tokens.digest(token)
        with self._sessions.begin() as session:
            found = session.execute(
                select(_Token, _Person)
                .join(_Person, _Token.person_id == _Person.id)
                .where(_Token.hash == wanted, _LIVE)
            ).one_or_none()
            if found is None:
                return None
            row, person = found
            limits = _limits_of(session, [row.id]).get(row.id, ()) if row.limited else None
            if _expired(row, now):
                assert row.expires_at is not None
                named = api_tokens.ApiToken(row.name, row.public_id)
                raise TokenExpired(_person_info(person), limits, named, row.expires_at)
            if limits == ():
                # A limited token whose apps are all gone works nowhere, never everywhere.
                return None
            last = row.last_used_at
            if last is None or (now - last).total_seconds() >= 60:
                row.last_used_at = now
            return _person_info(person), limits, api_tokens.ApiToken(row.name, row.public_id)

    def keep_error(
        self,
        login: str,
        name: str,
        error_id: str,
        now: datetime,
        caller: str,
        role: str | None,
        tool: str,
        text: str,
    ) -> bool:
        """Keep an error of the app's code, in a call, for the app's owner: at most
        `MAX_ERROR_TEXT` bytes of its text. Then drop the app's errors older than
        `ERRORS_KEPT_FOR`, and all but its last `ERRORS_KEPT`. False when there is no such app,
        and nothing is kept."""
        _aware(now)
        with self._sessions.begin() as session:
            app = _app_row(session, login, name)
            if app is None:
                return False
            # The tool's name is the one part a caller writes: any name may be asked for.
            _add_error(session, app.id, error_id, now, caller, role, tool[:100], text)
            return True

    def keep_start_error(
        self, login: str, name: str, now: datetime, text: str, new_id: Callable[[], str]
    ) -> str | None:
        """Keep the app's failed start for its owner, as `keep_error` keeps a call's error,
        under the id `new_id` gives, and drop all but the app's last `STARTS_KEPT` failed
        starts. Unless the app's latest error is a failed start with the same text, kept less
        than `SAME_START_FOR` ago: then it keeps nothing new, and `new_id` isn't called. The
        id it is kept under, or None when there is no such app."""
        _aware(now)
        text = _cut(text, MAX_ERROR_TEXT)
        with self._sessions.begin() as session:
            app = _app_row(session, login, name)
            if app is None:
                return None
            mine = _AppError.app_id == app.id
            latest = session.execute(
                select(_AppError.error_id, _AppError.tool, _AppError.text)
                .where(mine, _AppError.at >= now - SAME_START_FOR)
                .order_by(_AppError.id.desc())
                .limit(1)
            ).one_or_none()
            if latest is not None and latest.tool is None and latest.text == text:
                return latest.error_id
            error_id = new_id()
            _add_error(session, app.id, error_id, now, None, None, None, text)
            _keep_last(session, STARTS_KEPT, mine, _AppError.tool.is_(None))
            return error_id

    def drop_old_errors(self, now: datetime) -> None:
        """Drop every app's errors older than `ERRORS_KEPT_FOR`: those of an app that fails
        no more would otherwise stay, since `keep_error` drops only its own app's."""
        _aware(now)
        with self._sessions.begin() as session:
            session.execute(delete(_AppError).where(_AppError.at < now - ERRORS_KEPT_FOR))

    def errors(
        self, login: str, name: str, now: datetime, limit: int | None = None
    ) -> list[ErrorInfo]:
        """The app's errors kept for its owner, newest first, for a list: none older than
        `ERRORS_KEPT_FOR`, the latest `limit` only, if given, and of each text only its first
        `ERROR_START` characters. `error` gives one whole."""
        start = func.substr(_AppError.text, 1, ERROR_START)
        query = (
            _kept_errors(login, name, now, *_ERROR_FIELDS, start)
            .order_by(_AppError.id.desc())
            .limit(limit)
        )
        with self._sessions.begin() as session:
            return [ErrorInfo(*row) for row in session.execute(query)]

    def error_count(self, login: str, name: str, now: datetime) -> int:
        """How many of the app's errors are kept for its owner."""
        with self._sessions.begin() as session:
            return session.scalar(_kept_errors(login, name, now, func.count())) or 0

    def error(self, login: str, name: str, error_id: str, now: datetime) -> ErrorInfo | None:
        """One of the app's errors kept for its owner, by its id, or None when the app has no
        such error."""
        with self._sessions.begin() as session:
            found = session.execute(
                _kept_errors(login, name, now, *_ERROR_FIELDS, _AppError.text).where(
                    _AppError.error_id == error_id
                )
            ).one_or_none()
            return None if found is None else ErrorInfo(*found)

    def sandbox(self, login: str) -> SandboxInfo | None:
        """The owner's sandbox, or None when they have none yet."""
        with self._sessions.begin() as session:
            found = session.execute(
                select(_Sandbox, _Person.login).join(_Person).where(_Person.login == login)
            ).one_or_none()
            if found is None:
                return None
            row, owner = found
            return SandboxInfo(owner, row.name, row.url, row.token, row.sdk, row.ready_at)

    def note_apps_used(self, login: str, now: datetime) -> None:
        """Keep `now` as when the owner's apps or the builder were last used, unless their
        account was deleted."""
        _aware(now)
        with self._sessions.begin() as session:
            person = session.scalar(select(_Person).where(_Person.login == login, _LIVE))
            if person is not None:
                person.apps_last_used_at = now

    def apps_used_since(self, since: datetime) -> list[str]:
        """The owners whose apps or builder were used since `since`, the most recent first."""
        _aware(since)
        with self._sessions.begin() as session:
            query = (
                select(_Person.login)
                .where(_Person.apps_last_used_at >= since, _LIVE)
                .order_by(_Person.apps_last_used_at.desc(), _Person.login)
            )
            return list(session.scalars(query))

    def add_sandbox_time(self, seconds: Mapping[tuple[str, str], int]) -> None:
        """Add the seconds each owner's sandbox was busy, by their login and the month, to
        what is kept, in one transaction: a deleted account's too."""
        with self._sessions.begin() as session:
            for (login, month), more in seconds.items():
                person = session.scalar(select(_Person.id).where(_Person.login == login))
                if person is None:
                    continue
                row = session.get(_SandboxTime, (person, month))
                if row is None:
                    session.add(_SandboxTime(person_id=person, month=month, seconds=more))
                else:
                    row.seconds += more

    def sandbox_time(self, month: str) -> dict[str, int]:
        """The seconds each owner's sandbox was busy in `month`, by their login."""
        with self._sessions.begin() as session:
            query = (
                select(_Person.login, _SandboxTime.seconds)
                .join(_Person, _Person.id == _SandboxTime.person_id)
                .where(_SandboxTime.month == month)
            )
            return {login: seconds for login, seconds in session.execute(query)}

    def drop_sandbox_time_before(self, month: str) -> None:
        """Drop the sandbox time of every month before `month`."""
        with self._sessions.begin() as session:
            session.execute(delete(_SandboxTime).where(_SandboxTime.month < month))

    def sandbox_logins(self) -> list[str]:
        """The owners who have a sandbox, sorted."""
        with self._sessions.begin() as session:
            query = select(_Person.login).join(_Sandbox).order_by(_Person.login)
            return list(session.scalars(query))

    def save_sandbox(self, info: SandboxInfo) -> None:
        """Replace the owner's sandbox, if they have one."""
        if info.ready_at is not None:
            _aware(info.ready_at)
        with self._sessions.begin() as session:
            owner = _person_for_login(session, info.login, datetime.now(UTC))
            row = session.scalar(select(_Sandbox).where(_Sandbox.owner_id == owner.id))
            if row is None:
                session.add(
                    _Sandbox(
                        owner_id=owner.id,
                        name=info.name,
                        url=info.url,
                        token=info.token,
                        sdk=info.sdk,
                        ready_at=info.ready_at,
                    )
                )
            else:
                row.name = info.name
                row.url = info.url
                row.token = info.token
                row.sdk = info.sdk
                row.ready_at = info.ready_at

    def delete_sandbox(self, login: str) -> None:
        """Remove the owner's sandbox, if there is one."""
        with self._sessions.begin() as session:
            owner = session.scalar(select(_Person).where(_Person.login == login))
            if owner is None:
                return
            row = session.scalar(select(_Sandbox).where(_Sandbox.owner_id == owner.id))
            if row is not None:
                session.delete(row)

    def ping(self) -> None:
        """Read one row of a real table, or raise: SQLite's `SELECT 1` never reads the file."""
        with self._sessions.begin() as session:
            session.execute(select(_Person.id).limit(1)).all()

    def close(self) -> None:
        """Close the engine."""
        self._engine.dispose()


def _aware(value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError("a time needs its offset")


def _cut(text: str, limit: int) -> str:
    """`text` in at most `limit` bytes of UTF-8: whole, or its start and its end around a line
    saying it was cut. The end of a traceback says where the error was raised."""
    raw = text.encode()
    if len(raw) <= limit:
        return text
    mark = f"\n[cut to {limit // 1024} KB]\n".encode()
    start = limit // 4
    end = limit - start - len(mark)
    # A character cut in two, on either side, is left out.
    return (raw[:start] + mark + raw[-end:]).decode(errors="ignore")


def _add_error(
    session: Session,
    app_id: int,
    error_id: str,
    now: datetime,
    caller: str | None,
    role: str | None,
    tool: str | None,
    text: str,
) -> None:
    """Add an error of the app `app_id`, with at most `MAX_ERROR_TEXT` bytes of its text. Then
    drop the app's errors older than `ERRORS_KEPT_FOR`, and all but its last `ERRORS_KEPT`."""
    session.add(
        _AppError(
            app_id=app_id,
            error_id=error_id,
            at=now,
            caller=caller,
            role=role,
            tool=tool,
            text=_cut(text, MAX_ERROR_TEXT),
        )
    )
    session.flush()
    mine = _AppError.app_id == app_id
    session.execute(delete(_AppError).where(mine, _AppError.at < now - ERRORS_KEPT_FOR))
    _keep_last(session, ERRORS_KEPT, mine)


def _keep_last(session: Session, most: int, *where: Any) -> None:
    """Drop all but the last `most` of the errors `where` picks."""
    kept = select(_AppError.id).where(*where).order_by(_AppError.id.desc())
    oldest = session.scalar(kept.offset(most - 1).limit(1))
    if oldest is not None:
        session.execute(delete(_AppError).where(*where, _AppError.id < oldest))


def _kept_errors(login: str, name: str, now: datetime, *columns: Any) -> Any:
    """The query for `columns` of an app's errors that are still kept at `now`."""
    _aware(now)
    return (
        select(*columns)
        .select_from(_AppError)
        .join(_App, _AppError.app_id == _App.id)
        .join(_Person, _App.owner_id == _Person.id)
        .where(_Person.login == login, _App.name == name, _AppError.at >= now - ERRORS_KEPT_FOR)
    )


def _known_person(
    session: Session, issuer: str, subject: str, email: str | None, now: datetime
) -> _Person | None:
    """The person row for an identity, or with `email`, a verified one, the account waiting
    for it: an imported owner with no identity yet, or a person whose identity a provider move
    cleared. Raises `EmailTaken` when another identity has that email, as a new one can't
    claim it, or when another person has it and the person has one already.

    A person with no email whose verified one another person has is never refused: their
    email is verified. When the other is the account waiting for it, and the person's own
    holds nothing, the person becomes the waiting one, as `_join` says. Otherwise both are
    kept, and the person keeps no email."""
    if not issuer.strip() or not subject.strip():
        raise ValueError("a person needs an issuer and a subject")
    person = session.scalar(
        select(_Person).where(_Person.issuer == issuer, _Person.subject == subject, _LIVE)
    )
    if email is None:
        return person
    other = session.scalar(select(_Person).where(_Person.email == email, _LIVE))
    if person is not None:
        if other is not None and other.id != person.id:
            if person.email is not None:
                raise EmailTaken(f"another person already uses the email {email!r}")
            if _waiting(other) and _holds_nothing(session, person.id):
                return _join(session, person, other, now)
        else:
            person.email = email
        person.email_verified = True
        return person
    if other is None:
        return None
    if not _waiting(other):
        raise EmailTaken(f"another identity already uses the email {email!r}")
    other.issuer = issuer
    other.subject = subject
    other.email_verified = True
    return other


def _waiting(person: _Person) -> bool:
    """Whether the person has no identity, and so waits for a sign-in with their email."""
    return person.issuer is None and person.subject is None


def _holds_nothing(session: Session, person_id: int) -> bool:
    """Whether the person owns no app, a draft included, and holds no membership, share or
    token: nothing that deleting their account would lose."""
    held = (
        select(_App.id).where(_App.owner_id == person_id),
        select(_Membership.id).where(_Membership.person_id == person_id),
        select(_Share.id).where(_Share.person_id == person_id),
        select(_Token.id).where(_Token.person_id == person_id),
    )
    return all(session.scalar(query.limit(1)) is None for query in held)


def _join(session: Session, person: _Person, waiting: _Person, now: datetime) -> _Person:
    """Give the account `waiting` for the person's verified email their identity and their
    rights, and close theirs, which holds nothing, as deleting an account does: its login
    stays reserved, and a session that holds its id finds nobody."""
    identity = person.issuer, person.subject
    rights = _person_info(person).rights | _person_info(waiting).rights
    _close(session, person, now)
    # The identity is free before the waiting account takes it.
    session.flush()
    waiting.issuer, waiting.subject = identity
    waiting.email_verified = True
    waiting.rights = json.dumps(sorted(rights))
    return waiting


def _person_for_login(
    session: Session, login: str, now: datetime, email: str | None = None
) -> _Person:
    """The person row for an owner login, made with no provider identity when it is new.

    A new login follows the rules a chosen one does. With `email`, a person with no identity
    yet waits for that email. A deleted account's login is refused with `AccountDeleted`.
    """
    person = session.scalar(select(_Person).where(_Person.login == login))
    if person is not None and person.deleted_at is not None:
        raise AccountDeleted(_deleted(login))
    if email is not None and (person is None or person.email != email):
        if person is not None and person.issuer is not None:
            raise ValueError(f"{login} has already signed in: leave out --email")
        other = session.scalar(select(_Person).where(_Person.email == email))
        if other is not None:
            raise ValueError(f"another person already uses the email {email!r}")
    if person is None:
        refusal = login_problem(login)
        if refusal is not None:
            raise LoginRefused(refusal)
        person = _Person(login=login, email=email, created_at=now)
        session.add(person)
        session.flush()
    elif email is not None and person.issuer is None:
        person.email = email
    return person


def _close(session: Session, person: _Person, now: datetime) -> Forgotten:
    """Close the person's account, as `Database.close_account` says, and say what was
    forgotten of their identity."""
    forgotten = Forgotten(person.issuer, person.subject, person.email)
    if person.deleted_at is None:
        person.deleted_at = now
    tokens = select(_Token.id).where(_Token.person_id == person.id)
    session.execute(delete(_TokenLimit).where(_TokenLimit.token_id.in_(tokens)))
    session.execute(delete(_Token).where(_Token.person_id == person.id))
    session.execute(delete(_Membership).where(_Membership.person_id == person.id))
    session.execute(delete(_Share).where(_Share.person_id == person.id))
    if person.email is not None:
        session.execute(delete(_Invitation).where(_Invitation.email == person.email))
    owned = select(_App.id).where(_App.owner_id == person.id)
    session.execute(delete(_Membership).where(_Membership.app_id.in_(owned)))
    session.execute(delete(_Share).where(_Share.app_id.in_(owned)))
    session.execute(delete(_Invitation).where(_Invitation.app_id.in_(owned)))
    session.execute(delete(_TokenLimit).where(_TokenLimit.app_id.in_(owned)))
    person.issuer = person.subject = person.email = None
    person.rights = "[]"
    person.apps_last_used_at = None
    return forgotten


def _info(row: _App, login: str) -> AppInfo:
    return AppInfo(
        login=login,
        name=row.name,
        description=row.description,
        created_at=row.created_at,
        published_at=row.published_at,
        accent=row.look.accent if row.look is not None else None,
        icon=row.look.icon if row.look is not None else None,
        title=row.titled.title if row.titled is not None else None,
    )


def _set_look(row: _App, accent: str | None, icon: str | None, title: str | None) -> None:
    if accent is not None and not ACCENT.fullmatch(accent):
        raise ValueError(f"an accent is written #rrggbb: {accent!r} is not one")
    if title is not None and (problem := title_problem(title)) is not None:
        raise ValueError(f"{title!r} can't be an app's title: {problem}")
    if title is None:
        row.titled = None
    elif row.titled is None:
        row.titled = _AppTitle(app_id=row.id, title=title)
    else:
        row.titled.title = title
    look = row.look
    if look is None:
        look = row.look = _AppLook(app_id=row.id)
    look.accent = accent
    look.icon = icon


def _email(value: str) -> str:
    email = value.strip().lower()
    if not is_email(email):
        raise ValueError(f"{value!r} is not an email address")
    return email


def _role(value: str) -> str:
    role = value.strip()
    if not role:
        raise ValueError("a membership needs a role")
    return role


def _person_info(row: _Person) -> PersonInfo:
    return PersonInfo(
        id=row.id,
        issuer=row.issuer,
        subject=row.subject,
        email=row.email,
        login=row.login,
        created_at=row.created_at,
        rights=frozenset(Right(name) for name in json.loads(row.rights)),
        email_verified=row.email_verified,
        deleted_at=row.deleted_at,
    )


def _deleted(login: str) -> str:
    """Why a deleted account's login is refused, and what to do."""
    return (
        f"The account {login} was deleted, and its login is kept so that nobody takes it: "
        "use another login."
    )


def _token_info(
    row: _Token, limits: tuple[TokenLimit, ...] | None, made_by: str | None
) -> TokenInfo:
    return TokenInfo(
        row.id,
        row.name,
        row.created_at,
        row.last_used_at,
        limits,
        row.public_id,
        made_by,
        row.expires_at,
    )


def _maker(session: Session, person_id: int, public_id: str, now: datetime) -> _Token:
    """The person's token, by its public id, that makes another. Raises `ValueError` when it
    is gone, revoked meanwhile, expired meanwhile, or limited, as a limited token makes
    none."""
    row = session.scalar(
        select(_Token).where(_Token.person_id == person_id, _Token.public_id == public_id)
    )
    if row is None:
        raise ValueError("The token you are using was revoked: it makes no token any more.")
    if _expired(row, now):
        raise ValueError("The token you are using expired: it makes no token any more.")
    if row.limited:
        raise ValueError(
            "A token limited to apps makes no token: use a token for everywhere, or sign in."
        )
    return row


def _expired(row: _Token, now: datetime) -> bool:
    """Whether a token no longer works, at `now`."""
    return row.expires_at is not None and row.expires_at <= now


def _expires_at(now: datetime, days: int | None, maker: _Token | None) -> datetime | None:
    """When a new token made at `now` for `days` expires: never for None, unless a token
    makes it. Then it never outlives its maker: None is the default lifetime, cut to end with
    the maker's, and a lifetime past the maker's is refused. Raises `ValueError`, and
    `MakerExpiring` for a maker that expires within the shortest lifetime."""
    if maker is None:
        return None if days is None else now + timedelta(days=days)
    latest = maker.expires_at
    if latest is not None and latest - now < timedelta(days=api_tokens.LIFETIMES[0]):
        raise MakerExpiring(latest)
    wanted = now + timedelta(days=api_tokens.DEFAULT_DAYS if days is None else days)
    if latest is None or wanted <= latest:
        return wanted
    if days is None:
        return latest
    raise ValueError(
        f"The token you call with expires on {api_tokens.expiry(latest)}, and a token it "
        "makes never outlives it: choose fewer days, or leave days out to end with it."
    )


def _limits_of(session: Session, token_ids: list[int]) -> dict[int, tuple[TokenLimit, ...]]:
    """The apps and roles each of these tokens is limited to, in the order of the apps' names.
    A token with none is left out."""
    rows = session.execute(
        select(_TokenLimit.token_id, _TokenLimit.role, _App.name, _Person.login)
        .join(_App, _TokenLimit.app_id == _App.id)
        .join(_Person, _App.owner_id == _Person.id)
        .where(_TokenLimit.token_id.in_(token_ids))
    )
    found: dict[int, list[TokenLimit]] = {}
    for token_id, role, name, login in rows:
        found.setdefault(token_id, []).append(TokenLimit(login, name, role))
    return {token_id: tuple(sorted(limits, key=_by_app)) for token_id, limits in found.items()}


def _by_app(limit: TokenLimit) -> tuple[str, str]:
    """A limit's place among a token's: by its app's owner then name, the same in Python
    whatever the database's collation."""
    return limit.login, limit.name


def _new_token(
    session: Session,
    person_id: int,
    name: str,
    limits: Sequence[TokenLimit] | None,
    now: datetime,
) -> tuple[str, tuple[TokenLimit, ...] | None, list[tuple[_App, TokenLimit]]]:
    """A new token of the person's: its name, trimmed, its limits, in the order of their
    apps' names, and the app of each. Raises `ValueError` for a name that isn't one or that
    the person already gave a token, expired or not, when they have the most tokens there
    may be, expired ones included, or for limits they may not have."""
    name = api_tokens.token_name(name)
    ordered = _checked_limits(limits) if limits is not None else None
    person = session.get(_Person, person_id)
    if person is None:
        raise ValueError("there is no such person")
    apps = [(_limit_app(session, person, one), one) for one in ordered or ()]
    mine = session.scalars(select(_Token).where(_Token.person_id == person_id)).all()
    same = next((one for one in mine if one.name == name), None)
    if same is not None and _expired(same, now):
        raise ValueError(
            f"Your token named {name!r} expired: revoke it first to use its name again, or "
            "choose another name."
        )
    if same is not None:
        raise ValueError(f"You already have a token named {name!r}: choose another name.")
    if len(mine) >= api_tokens.MAX_TOKENS:
        raise ValueError(
            f"You have {api_tokens.MAX_TOKENS} tokens, the most there may be. "
            "Revoke one you no longer use first."
        )
    return name, ordered, apps


def _checked_limits(limits: Sequence[TokenLimit]) -> tuple[TokenLimit, ...]:
    """A new token's limits, in the order of their apps' names. Raises `ValueError` when there
    are none, or two for one app."""
    ordered = tuple(sorted(limits, key=_by_app))
    if not ordered:
        raise ValueError("A limited token works for at least one app: choose one.")
    apps = [(one.login, one.name) for one in ordered]
    if len(set(apps)) != len(apps):
        raise ValueError("A token holds one role in each app it works for: choose one per app.")
    return ordered


def _limit_app(session: Session, person: _Person, limit: TokenLimit) -> _App:
    """The app a new token is limited to, once the person may use it as the limit's role.
    Raises `ValueError`."""
    app = _app_row(session, limit.login, limit.name)
    if app is None or app.published_at is None:
        raise ValueError(f"There is no app {limit.login}/{limit.name} to limit a token to.")
    if app.owner_id != person.id and _membership(session, person.id, app.id, limit.role) is None:
        raise ValueError(
            f"You don't hold the role {limit.role} in {limit.login}/{limit.name}: "
            "choose one of your roles."
        )
    return app


def _membership_info(
    membership: _Membership, person: _Person, app: _App, owner: _Person
) -> MembershipInfo:
    return MembershipInfo(
        person=_person_info(person),
        app=_info(app, owner.login),
        role=membership.role,
        created_at=membership.created_at,
    )


def _app_row(session: Session, login: str, name: str) -> _App | None:
    return session.scalar(
        select(_App).join(_Person).where(_Person.login == login, _App.name == name)
    )


def _owned_app(session: Session, owner_login: str, app_name: str) -> _App:
    app = _app_row(session, owner_login, app_name)
    if app is not None:
        return app
    if session.scalar(select(_App).where(_App.name == app_name)) is not None:
        raise ValueError(f"{owner_login} is not the owner of {app_name}")
    raise ValueError(f"there is no app {owner_login}/{app_name}")


def _owner_email(session: Session, app: _App) -> str:
    """The email of the app's owner when a person row records it, else an impossible value."""
    owner = session.get(_Person, app.owner_id)
    return owner.email or "" if owner is not None else ""


def _membership(session: Session, person_id: int, app_id: int, role: str) -> _Membership | None:
    """The person's membership of one role of an app, if they hold it."""
    return session.scalar(
        select(_Membership).where(
            _Membership.person_id == person_id,
            _Membership.app_id == app_id,
            _Membership.role == role,
        )
    )


def _add_share(
    session: Session, person_id: int, app_id: int, email: str, role: str, now: datetime
) -> None:
    """A share to accept, with the email it was shared with, unless the person holds the role
    already. One that waits already keeps the email typed last."""
    waiting = session.scalar(
        select(_Share).where(
            _Share.person_id == person_id, _Share.app_id == app_id, _Share.role == role
        )
    )
    if waiting is not None:
        waiting.email = email
    elif _membership(session, person_id, app_id, role) is None:
        session.add(
            _Share(person_id=person_id, app_id=app_id, role=role, created_at=now, email=email)
        )


def _add_invitation(session: Session, app: _App, email: str, role: str, now: datetime) -> None:
    found = session.scalar(
        select(_Invitation).where(
            _Invitation.app_id == app.id,
            _Invitation.email == email,
            _Invitation.role == role,
        )
    )
    if found is None:
        session.add(_Invitation(app_id=app.id, email=email, role=role, created_at=now))
