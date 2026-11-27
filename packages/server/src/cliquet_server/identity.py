"""Who someone is on Cliquet: their login, which they choose at first sign-in.

The login is in an app's addresses and folders, in its history, and it is the caller id an
app receives. It is never the person's email, and it can't be changed.
"""

import random
import re
import secrets
import unicodedata
from collections.abc import Iterator

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_LOGIN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MIN_LOGIN = 3
MAX_LOGIN = 39

# Names a login can't take: the console's paths, and words that would read as Cliquet's own.
# Anything starting with `app_` is refused too, as underscores are, by the characters.
RESERVED = frozenset(
    {
        "about",
        "account",
        "admin",
        "api",
        "app",
        "apps",
        "auth",
        "build",
        "builder",
        "callback",
        "console",
        "help",
        "healthz",
        "home",
        "login",
        "logout",
        "manage",
        "mcp",
        "me",
        "new",
        "root",
        "settings",
        "shares",
        "sharing",
        "static",
        "cliquet",
        "support",
        "system",
        "tokens",
        "welcome",
        "well-known",
    }
)

# The words of a suggestion when the email gives none: short, plain and harmless.
WORDS = (
    "amber", "apple", "aspen", "badger", "basil", "beacon", "birch", "bright", "brook", "calm",
    "cedar", "clever", "cloud", "clover", "coral", "cosy", "cotton", "crane", "daisy", "dawn",
    "dune", "eager", "ember", "fern", "finch", "fjord", "flint", "frost", "gentle", "ginger",
    "glade", "golden", "harbor", "hazel", "heron", "honey", "indigo", "iris", "ivy", "jolly",
    "juniper", "kind", "lark", "lemon", "lilac", "linen", "lively", "lotus", "maple", "meadow",
    "mellow", "merry", "mint", "misty", "moss", "nimble", "oak", "olive", "orchid", "otter",
    "pebble", "pine", "plum", "polar", "quiet", "quill", "raven", "reed", "river", "robin",
    "rowan", "sage", "sandy", "silver", "sky", "slate", "snowy", "sparrow", "spruce", "steady",
    "sunny", "swift", "thistle", "tidy", "topaz", "tulip", "velvet", "willow", "wren", "zesty",
)  # fmt: skip
_KEY = "abcdefghjkmnpqrstuvwxyz23456789"
# Letters NFKD leaves whole, spelt as ASCII so that søren reads as soren, not sren.
_SPELT = str.maketrans(
    {
        "ø": "o",
        "ß": "ss",
        "ł": "l",
        "æ": "ae",
        "œ": "oe",
        "đ": "d",
        "ð": "d",
        "þ": "th",
        "\u0131": "i",
    }
)
KEY_LENGTH = 3
_SYSTEM = secrets.SystemRandom()


def is_email(value: str) -> bool:
    """True when the value looks like an email address."""
    return _EMAIL.fullmatch(value.strip()) is not None


def login_problem(login: str) -> str | None:
    """Why a login can't be chosen, in a sentence that says what to write, or None.

    It checks the characters, the length and the reserved names, not whether it is free.
    """
    if login in RESERVED or login.startswith("app-"):
        return f"{login!r} is kept for Cliquet's own pages. Choose another login."
    if not MIN_LOGIN <= len(login) <= MAX_LOGIN:
        return (
            f"A login is {MIN_LOGIN} to {MAX_LOGIN} characters long, and {login!r} has "
            f"{len(login)}. Write one of that length."
        )
    if not _LOGIN.fullmatch(login):
        return (
            f"{login!r} has characters a login can't hold. Use lower-case letters, digits "
            "and single hyphens, with no hyphen at either end, such as quiet-otter-4k2."
        )
    return None


def email_login(email: str | None) -> str | None:
    """The login an email's local part reads as, such as sam-smith for Sam.Smith+tea@example.com,
    short enough to take a key, or None when it reads as none or as one of Cliquet's own names,
    which a key wouldn't hide."""
    if not email or "@" not in email:
        return None
    local = email.rsplit("@", 1)[0].split("+", 1)[0]
    spelt = unicodedata.normalize("NFKD", local.lower().translate(_SPELT))
    plain = spelt.encode("ascii", "ignore").decode()
    login = "-".join(re.findall(r"[a-z0-9]+", plain))[: MAX_LOGIN - KEY_LENGTH - 1].strip("-")
    return login if login and login not in RESERVED else None


def login_suggestions(
    email: str | None = None, chance: random.Random | None = None
) -> Iterator[str]:
    """Logins to offer, best first, drawn from `chance`, or from the system's randomness: the
    email's local part, such as sam, if it can be a login, then it with a short key, such as
    sam-4k2, or, when the email gives none, one or two words and a key, such as quiet-otter-4k2."""
    chance = chance or _SYSTEM
    base = email_login(email)
    if base is not None and login_problem(base) is None:
        yield base
    while True:
        key = "".join(chance.choice(_KEY) for _ in range(KEY_LENGTH))
        if base is not None and login_problem(keyed := f"{base}-{key}") is None:
            yield keyed
        else:
            words = [chance.choice(WORDS) for _ in range(chance.choice((1, 2)))]
            yield "-".join([*words, key])
