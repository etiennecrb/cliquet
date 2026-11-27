"""Exceptions: refusals and denials at run time, problems in an app's definition."""

import logging
import re
import secrets
import traceback
from collections.abc import Callable
from dataclasses import dataclass

_log = logging.getLogger(__name__)


class Refused(Exception):
    """Raised by an action to refuse a change. The message is shown to the caller."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Denied(Exception):
    """The caller's role may not run this action or query."""


@dataclass(frozen=True)
class Problem:
    """One thing wrong with an app's definition, and where."""

    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


class AppError(Exception):
    """An app's definition is invalid. Lists every problem found, each saying what to fix."""

    def __init__(self, problems: list[Problem]) -> None:
        self.problems = problems
        super().__init__("\n".join(str(p) for p in problems))


class AppFailed(Exception):
    """The app's code failed: an action, a query, a view or an invariant raised, or answered
    something Cliquet can't use. Every engine reports it so, in this process or a sandbox.

    The message is its text, starting with the exception's type, as in `KeyError: 'x'`. In
    this process, the app's exception is the cause (`__cause__`).

    `kind` is the type of the exception, as Cliquet read it from the exception itself, or None.
    It is what a caller who isn't the app's owner is told, so it never comes from the text.
    """

    def __init__(self, message: str, kind: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind

    @classmethod
    def of(cls, error: BaseException, where: str = "") -> AppFailed:
        """The failure of the app's code that raised `error`, which becomes its cause.
        `where` is added to the message, as in ", in the invariant positive"."""
        kind = type(error).__name__
        failed = cls(f"{kind}: {error}{where}", kind)
        failed.__cause__ = error
        return failed


def described(error: BaseException) -> tuple[str | None, str]:
    """An error's type, if known, and its text starting with it, as in `KeyError: 'x'`. An
    `AppFailed`'s text already starts with the type of what the app's code raised."""
    if isinstance(error, AppFailed):
        return error.kind, str(error)
    return type(error).__name__, f"{type(error).__name__}: {error}"


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")


def type_name(value: object) -> str | None:
    """`value` if it can be the name of an exception's type, such as `KeyError`; else None."""
    return value if isinstance(value, str) and _NAME.fullmatch(value) else None


def shortened(text: str, limit: int) -> str:
    """`text`, or its first characters and "...", in at most `limit` characters."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def with_article(kind: str) -> str:
    """An exception's type with its article: "a KeyError", "an AttributeError"."""
    return f"{'an' if kind[0] in 'AEIOUaeiou' else 'a'} {kind}"


class SafeError(Exception):
    """An error whose text is a fixed sentence Cliquet wrote, such as "the sandbox can't be
    reached": it holds nothing from the app's code, so every caller reads it.

    Any other error's text may come from the app's code, and only the app's owner reads it.
    `passes` says the call may work when made again, as when a sandbox can't be reached.
    """

    passes: bool = False


def from_app(error: BaseException) -> bool:
    """Whether an error's text may come from the app's code: any error but a `SafeError`."""
    return not isinstance(error, SafeError)


def new_error_id() -> str:
    """A new id for an error: `e-` and 12 hexadecimal characters."""
    return f"e-{secrets.token_hex(6)}"


def report(error: BaseException, text: str) -> str:
    """Log an error's full text and traceback under a new id, as a warning: the server's log
    has it. It may be the app's own error, so it is never `unexpected:`.

    Returns the id, `e-` and 12 hexadecimal characters, which a caller is given in place of
    the text.
    """
    error_id = new_error_id()
    _log.warning("Error %s: %s", error_id, text, exc_info=error)
    return error_id


def report_unexpected(error: BaseException, text: str) -> str:
    """Log a failure of Cliquet's own, never the app's, as one `unexpected:` error with its
    traceback under a new id, and return the id."""
    error_id = new_error_id()
    _log.error("unexpected: error %s: %s", error_id, text, exc_info=error)
    return error_id


def shown(
    error: BaseException,
    text: str,
    owner: bool,
    stand_in: str,
    *,
    error_id: str | None = None,
    keep: Callable[[str, str], str | None] | None = None,
) -> str:
    """What a caller reads of an error: `text` for the app's owner, and for a `SafeError`;
    for anyone else, `stand_in`, the id it is logged under and who reads its details.

    `text` is written to the log under a new id, unless `error_id` says it already was.

    `keep` is a server's: it keeps an error whose text may come from the app's code for the
    app's owner, whoever the caller is. It takes the error's id and its text with its
    traceback, and returns the sentence that tells anyone else where the owner reads them,
    or None when it kept nothing.
    """
    if error_id is None:
        error_id = report(error, text)
    if not from_app(error):
        return text
    sentence = None
    if keep is not None:
        sentence = keep(error_id, f"{text}\n\n{''.join(traceback.format_exception(error))}")
    if owner:
        return text
    if sentence is None:
        sentence = f"Error {error_id} has the details, for the app's owner."
    return f"{stand_in} {sentence}" if stand_in else sentence
