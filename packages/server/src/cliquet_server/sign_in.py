"""What sign-in on the web and at an MCP endpoint share: how a claim says an email is
verified, the login a newcomer is offered, the development login's person, and what a person
whose email isn't verified yet may do.

Such a person has no email until the provider verifies it: they claim nothing by one, share
no app and make no token. They have `Settings.verify_email_days` from when they chose their
login to verify it. After that, `locked_out` is the one decision every page, tool and app
address asks, and each gives its one sentence in its own form.
"""

import math
import random
import threading
from datetime import date, datetime, timedelta
from itertools import islice
from typing import Any

from cliquet_server.db import AccountDeleted, Database, PersonInfo, Right, may
from cliquet_server.identity import login_suggestions
from cliquet_server.settings import Settings


def email_verified(value: Any) -> bool:
    """Whether an `email_verified` claim says the address is verified."""
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1"}
    return value == 1


def free_login(database: Database, email: str | None = None, seed: str | None = None) -> str:
    """A suggested login nobody has or had, as a deleted account's is kept, or a taken one
    after many tries, for the choice to refuse: the email's local part, then with a key.
    With `seed`, such as an identity, the same one each time while it stays free."""
    chance = None if seed is None else random.Random(seed)
    for login in islice(login_suggestions(email, chance), 20):
        if database.account(login) is None:
            break
    return login


def development_person(database: Database, settings: Settings, now: datetime) -> PersonInfo:
    """The development login's person, made on its first sign-in, and granted build at each
    one: whoever runs the server as it builds.

    Raises ValueError when the server has no development login, or its login is refused, as
    a deleted account's is.
    """
    login = settings.dev_login
    if login is None:
        raise ValueError("the development login has no login name")
    try:
        person = database.person_by_login(login) or database.person_for(
            "cliquet://dev", f"dev:{login}", f"{login}@dev.invalid", login, now
        )
    except AccountDeleted:
        raise ValueError(
            f"The account {login} was deleted, and its login is kept so that nobody takes it. "
            "Set CLIQUET_DEV_LOGIN to another login, then restart the server."
        ) from None
    return person if may(person, Right.BUILD) else database.grant(login, Right.BUILD)


def days_left(settings: Settings, person: PersonInfo, now: datetime) -> int | None:
    """How many days, rounded up, the person has left to verify their email: None once it is
    verified, and 0 once their time is over."""
    if person.email_verified:
        return None
    left = person.created_at + timedelta(days=settings.verify_email_days) - now
    return max(0, math.ceil(left / timedelta(days=1)))


def locked_out(
    settings: Settings, person: PersonInfo, now: datetime, *, lost_email: bool = True
) -> str | None:
    """Why the person may use nothing until they verify their email, as every surface says
    it: their time to verify it is over. None for anyone else. With `lost_email`, it says
    where to ask for a new email, as the account page's button does itself."""
    if days_left(settings, person, now) != 0:
        return None
    return (
        f"Your email address isn't verified, and the {_days(settings.verify_email_days)} to "
        f"verify it are over: verify it with the sign-in provider, then sign in at "
        f"{settings.base_url}. Cliquet keeps your apps meanwhile."
        f"{_lost_email(settings) if lost_email else ''}"
    )


class LockedOut(Exception):
    """The signed-in person's time to verify their email is over: every page refuses them
    with `sentence`, as `locked_out` words it, and offers to sign out with `csrf`, their
    session's form token."""

    def __init__(self, sentence: str, csrf: str | None = None) -> None:
        super().__init__(sentence)
        self.sentence = sentence
        self.csrf = csrf


def verify_first(settings: Settings, doing: str) -> str:
    """Why a person whose email isn't verified may not do `doing`, such as share an app, and
    what to do."""
    return (
        f"Verify your email address with the sign-in provider, then sign in at "
        f"{settings.base_url}: until then, you can't {doing}."
    )


def reminder(settings: Settings, left: int, *, lost_email: bool = True) -> str:
    """What a person whose email isn't verified is told, with the days they have `left`, and
    with `lost_email`, where to ask for a new email, as the console's button does itself."""
    return (
        f"Your email address isn't verified yet: you have {_days(left)} left to verify it with "
        f"the sign-in provider, then sign in at {settings.base_url}. After that, Cliquet "
        "refuses everything until you do. Verifying it also lets you receive what people share "
        "with you, share your own apps and make API tokens."
        f"{_lost_email(settings) if lost_email else ''}"
    )


def _lost_email(settings: Settings) -> str:
    """Where a person asks for a new verification email, when the server can send one."""
    if settings.verification_email_url is None:
        return ""
    return (
        " If you lost the email, ask for a new one on your account page, at "
        f"{settings.base_url}/account."
    )


# Why an identity whose verified email another account has gets no account, and what to do,
# without the email, on a page and in a tool's error alike.
EMAIL_ELSEWHERE = (
    "Another account on Cliquet already has the email address the sign-in provider verified "
    "for this sign-in: sign in with that account instead, or ask the server's operator for help."
)


def email_taken(email: str) -> str:
    """What a person is told once when the email the provider verified is another account's,
    so theirs keeps none."""
    return (
        f"The sign-in provider verified your email address, {email}, but another account on "
        f"Cliquet has it, so this one keeps no email: what people share with {email} goes to "
        "that account."
    )


def email_waiting(email: str) -> str:
    """What a person is told once when the email the provider verified is another account's,
    which was waiting for it, and theirs holds something already, so both are kept."""
    return (
        f"The sign-in provider verified your email address, {email}, but it belongs to another "
        "account on Cliquet, which was waiting for it, and this one already holds apps or "
        "access: both are kept, and this one keeps no email. To join them, ask the server's "
        "operator."
    )


def joined(login: str, retired: str) -> str:
    """What a person is told once when the account `login` was waiting for the email the
    provider verified, and theirs, `retired`, held nothing: they are `login` from now on."""
    return (
        f"Your email address is verified, and the account {login} was waiting for it: you are "
        f"{login} from now on, with its apps. Your account {retired}, which held nothing, is "
        "closed, and its login stays reserved."
    )


def _days(count: int) -> str:
    return f"{count} day" if count == 1 else f"{count} days"


class Notices:
    """What Cliquet tells a person once, kept in memory, so a restart forgets it: what came up
    as they signed in, such as an email another account has, and once a day how many days
    they have left to verify their email. The person's endpoint says them in its next tool's
    answer, and the console on its next page."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiting: dict[int, list[str]] = {}
        # The day each person was last reminded to verify their email.
        self._reminded: dict[int, date] = {}

    def tell(self, person_id: int, text: str) -> None:
        """Keep `text` for the person's next answer."""
        with self._lock:
            self._waiting.setdefault(person_id, []).append(text)

    def waiting(self, person_id: int) -> bool:
        """Whether something waits to be told to the person, apart from the reminder."""
        with self._lock:
            return person_id in self._waiting

    def pop(self, person_id: int) -> list[str]:
        """What waits to be told to the person, once, apart from the reminder, which the
        console shows on every page itself."""
        with self._lock:
            return self._waiting.pop(person_id, [])

    def take(self, settings: Settings, person: PersonInfo, now: datetime) -> list[str]:
        """What to tell the person now, once: what waits for them, then, while their email
        isn't verified, once a day, how many days they have left."""
        with self._lock:
            told = self._waiting.pop(person.id, [])
            left = days_left(settings, person, now)
            today = now.date()
            if left and self._reminded.get(person.id) != today:
                told.append(reminder(settings, left))
                # Only today's are needed: the others go, so the dictionary stays small.
                self._reminded = {one: day for one, day in self._reminded.items() if day == today}
                self._reminded[person.id] = today
            return told
