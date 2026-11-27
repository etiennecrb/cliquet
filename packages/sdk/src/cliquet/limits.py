"""The size limits that bound one another, in one place.

Data goes to a sandbox and comes back as base64 in JSON. So what one answer may hold decides how
much data it can carry back, and that decides how much a draft may hold. A limit that bounds
nothing else stays with the code it protects.
"""

from starlette.exceptions import HTTPException
from starlette.requests import Request

MB = 1 << 20


def size_text(size: int) -> str:
    """A limit as a message says it: in MB when it is a whole number of them, else in bytes."""
    return f"{size // MB} MB" if size % MB == 0 else f"{size:,} bytes"


def amount_text(size: int) -> str:
    """An amount of data as a message says it: in MB to a tenth, below one MB in bytes."""
    return f"{size / MB:.1f} MB" if size >= MB else f"{size:,} bytes"


MAX_ANSWER = 32 * MB
"""One answer of a sandbox: a worker's line of JSON, and what the sandbox sends on to the
server. The sandbox writes it again more compactly, so it never grows on the way. The sandbox and
the server each hold an answer whole before they parse it: this keeps one call's memory in bounds
on the server's machine, which has 512 MB by default."""

MAX_RESULT = MB
"""An action's result, as compact UTF-8 JSON. It goes back to the caller's agent."""

MAX_QUERY_ANSWER = MAX_RESULT
"""A query's answer to a caller, and what `app_changes_since` answers, as compact UTF-8 JSON.
Like an action's result, it goes into the caller's agent's context, where a few hundred kB
already costs a conversation. A query that a view calls has no such bound: its answer stays in
the sandbox, and only the view's screen leaves it."""

_ROOM = 3 * MAX_RESULT + MB
"""What an answer holds besides its files: an action's result, which the worker's ASCII escapes
can make three times longer, and a refusal's messages."""

MAX_DATA = (MAX_ANSWER - _ROOM) // 4 * 3
"""The data one answer carries back, before base64: the files a call or a migration changed,
21 MB. No changed file may be larger."""

MAX_DRAFT = MAX_DATA
"""All the files of a draft, its code included. No more than one answer carries back, so a
migration can rewrite all the data of an app just published."""

MAX_APP_DATA = MAX_DATA
"""All of an app's data once it is live, every file its declaration matches: no more than one
answer carries back, so one migration can always rewrite all of it. Every call also sends all of
it to the sandbox, and the server holds it several times over meanwhile. A change that would
make the data larger than this, and larger than it was, is refused: so an app already over it
can still get back under, with changes that leave each file within what one answer carries
back."""


def data_problem(what: str, before: int, after: int) -> str | None:
    """The sentence that refuses `what`, which would take an app's data from `before` bytes to
    `after`: more than `MAX_APP_DATA`, and more than before. None when it passes."""
    if after <= MAX_APP_DATA or after <= before:
        return None
    return (
        f"{what} would leave the app with {amount_text(after)} of data, "
        f"{amount_text(after - MAX_APP_DATA)} more than the {size_text(MAX_APP_DATA)} an app "
        "keeps, so that one migration can rewrite all of it."
    )


MAX_DATA_FILE = 20_000_000
"""One data file uploaded to a draft. Under `MAX_DRAFT`, with room for the code."""

MAX_UPLOAD = MAX_DATA_FILE + 1_000_000
"""An upload's request body: the file, and room for the form's other fields and the multipart
framing. The upload's route checks it, and Starlette spools the file to disk."""

MAX_FILE_ARGUMENT = 5 * MB
"""A file an action takes, which the person uploads: it travels to the sandbox with the call, in
base64, beside all of the app's data, so the sandbox's request has room for it. An action that
keeps it writes it to a data file, which `MAX_APP_DATA` bounds."""


def file_too_big(nothing: str) -> str:
    """The sentence that refuses a file for an action over `MAX_FILE_ARGUMENT`, ending with
    what `nothing` was done, such as "changed"."""
    return (
        f"A file for an action is at most {size_text(MAX_FILE_ARGUMENT)} "
        f"({MAX_FILE_ARGUMENT:,} bytes). This one is bigger, and nothing was {nothing}."
    )


MAX_BODY = 4 * MB
"""Any other request's body: a screen's call, a form or an MCP message. The server holds it whole.
It is the limit MCP puts on a message itself, so the MCP endpoints keep theirs."""

TEST_OUTPUT = 20_000
"""The last characters of `cliquet test`'s output that are kept. The sandbox keeps no more, and the
server cuts an answer to it again."""


async def read_body(request: Request, limit: int) -> bytes:
    """A request's body, of at most `limit` bytes. One that states a larger length is refused
    with a 413 before any of it is read, and one that sends more is refused once it has."""
    too_large = HTTPException(413, f"a request's body is at most {size_text(limit)}")
    stated = request.headers.get("content-length")
    if stated is not None and (not stated.isdigit() or int(stated) > limit):
        raise too_large
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise too_large
    return bytes(body)
