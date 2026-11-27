"""API tokens: what one looks like, and what the server keeps of it.

A token is `cliquet_` and 43 random URL-safe characters. The prefix tells it from a JWT
without guessing, and from any other secret in a person's settings. The server keeps only
its SHA-256 hash, so a copy of the database holds no token that works.

Each token also has an id, `tok_` and 16 random hexadecimal characters, which the apps it
reaches see: never the token, and not the row's number, which would tell how many tokens the
server made. The `_` keeps it from looking like a login.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

PREFIX = "cliquet_"
ID_PREFIX = "tok_"
# How long a token's name may be.
MAX_NAME = 60
# How many tokens one person may have. A token may never expire, and an expired one stays
# listed until it is revoked, so this bounds the credentials that act as one person, keeps the
# Tokens page and the list short, and stops an agent caught in a loop from making tokens
# without end.
MAX_TOKENS = 50
# How many days a new token may work, the shortest first: a person may also make one that
# never expires, on the Tokens page.
LIFETIMES = (7, 30, 90, 365)
DEFAULT_DAYS = 90


def is_token(value: str) -> bool:
    """Whether a bearer value is one of Cliquet's tokens, by its prefix alone."""
    return value.startswith(PREFIX)


def new_token() -> str:
    """A new token, shown to its person once."""
    return f"{PREFIX}{secrets.token_urlsafe(32)}"


def new_id() -> str:
    """A new token's id, which the apps it reaches see."""
    return f"{ID_PREFIX}{secrets.token_hex(8)}"


@dataclass(frozen=True)
class ApiToken:
    """The API token a caller connected with: its name, which each change it makes keeps, and
    its id, which the app's code sees."""

    name: str
    id: str


def lifetimes() -> str:
    """The lifetimes a token may have, as a sentence says them: 7, 30, 90 or 365 days."""
    *first, last = LIFETIMES
    return f"{', '.join(map(str, first))} or {last} days"


def expiry(at: datetime) -> str:
    """When a token expires, as a sentence says it, in UTC, to the minute."""
    at = at.astimezone(UTC)
    return f"{at.day} {at:%b %Y, %H:%M} UTC"


def digest(token: str) -> str:
    """What the server keeps of a token: its SHA-256, in hexadecimal."""
    return hashlib.sha256(token.encode()).hexdigest()


def token_name(value: str) -> str:
    """A token's name as a person typed it, trimmed. Raises `ValueError` when it is empty,
    longer than `MAX_NAME` or more than one line."""
    name = " ".join(value.split())
    if not name or len(name) > MAX_NAME or not name.isprintable():
        raise ValueError(
            f'A token\'s name is one line of 1 to {MAX_NAME} characters, such as "Laptop at home".'
        )
    return name
