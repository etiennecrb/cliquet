# Writing app.py

A Cliquet app is one Python module, `app.py`, with three kinds of functions:

- **actions** change the data;
- **invariants** say what must always hold;
- **queries** answer questions.

It may also have **views**, the screens people read and tap: see `read_guide("views")`.

Every change runs on a copy of the data. Then every invariant runs on the result, and the change is kept or refused with the reasons. Agents call actions and queries as MCP tools. Each change that is kept is recorded, with who made it and when. Cliquet adds three tools to every app for that: `app_history`, `app_changes_since` and `app_revert`. `app_changes_since` runs one of the caller's queries on the data as it was at a time and as it is now, with the current code, and answers what was added and removed: a reader learns what changed only through the queries it may use.

Three complete apps show everything below. Read the one closest to yours first:

- `read_example("meeting-rooms", "app.py")`: bookings in CSV. Each rule over time is one function that the invariants, `book`, `move` and the `availability` query share, so a refusal and the query say the same thing. It numbers its records, moves one keeping its number, and has a week, a day, a search and an admin's screens.
- `read_example("environments", "app.py")`: a register in CSV, with references between three files. A role only reads, another records, an admin removes. Its queries answer with verdicts and their reasons, and an invariant keeps secrets out of free text.
- `read_example("ledger", "app.py")`: a household's accounts in Beancount, a library declared as a dependency, with two settings files in CSV. It imports a bank statement in one call, refused unless it reconciles, skipping the lines already there and saying so in its result; fixes an entry by replacing its exact text; refuses any change to what a statement confirmed; and lists what to do next. Its screens have charts, data grids and tabs.

`read_example("meeting-rooms")` lists the example's other files: its tests, its instructions and its data.

## The draft's files

| Path | Holds |
| --- | --- |
| `app.py` | The app |
| `instructions.md` | How an agent uses the app, sent to every agent that connects: see `read_guide("instructions")` |
| `tests/test_<name>.py` | Tests through the harness: see `read_guide("tests")` |
| `data/<file>` | The app's first data, until the first publish. Once the app is live, only its actions write the data. A file's name is up to eight parts joined by `/`, each of letters, digits, `.`, `_` and `-`, not starting with a dot, such as `stock.csv` or `2026/january.csv` |
| `migrate.py` | A one-off change of live data: see `read_guide("publishing")` |
| `app.py.lock` | The exact versions of the libraries. Cliquet writes it |

**Replace the template.** `create_app(app)` starts the draft from a template: a small list of items, with its own tests. Replace `app.py`, and remove `tests/test_app.py` with `delete_file` once you write your own tests.

A draft holds nothing else. A file written with `write_file` or `edit_file` is at most 200 KB: code, the migration, or data you write. A data file reaches the draft in one of two ways: you write it, for made-up data, or the person uploads it with `ask_for_file`, in the conversation or through its link, for data of their own, up to 20 MB. A draft holds at most 21 MB, and so does a live app's data: a change that would grow it past that is refused, so that one migration can rewrite all of it.

`edit_file` replaces an exact text `old` by `new`. For a long part, such as a whole view, give a range of lines instead: `first_line` and `last_line`, with `first_text` and `last_text`, the whole text of those two lines as `read_file` gave them, and `size`, the file's size it gave. An empty `new` removes the lines. If the size or either line reads otherwise, because an edit changed the file since you read it, nothing changes: read the lines again.

Write only what the request asks for. The agent using the app does the reading, sorting and summarising. The app keeps the data and the rules.

## The top of app.py

```python
# /// script
# requires-python = ">=3.14"
# dependencies = ["cliquet-sdk>=0.0.1,<0.1"]
# ///
"""Meeting rooms of a small office: rooms with their seats and opening hours, closing days,
and bookings that never clash."""

from cliquet import ActionContext, App, BaseModel, Field, InvariantContext, QueryContext, Refused

ROOMS = "rooms.csv"
CLOSURES = "closing_days.csv"
BOOKINGS = "bookings.csv"

office = App(
    "meeting-rooms",
    title="Meeting rooms",
    files=[ROOMS, CLOSURES, BOOKINGS],
    roles=["admin", "member"],
    accent="#4338ca",
    icon="calendar-clock",
    timezone="Europe/Paris",
)
```

- **The block at the top** lists the SDK, `cliquet-sdk`, and every library the app imports. Keep its `cliquet-sdk` line as `create_app` wrote it. Never list `cliquet`: on PyPI that is somebody else's package, and the app is refused. Cliquet builds the environment when you call `check_app`. Import only from `cliquet`, the standard library and the listed libraries. To use pydantic types such as `AwareDatetime`, list `pydantic` too. Libraries come from PyPI, by name and version, and each must publish a wheel: a library without one is refused by name. The block holds `requires-python` and `dependencies` only: no other setting, such as another package index, and no library pointing at a URL (`name @ https://...`).
- **The docstring is required.** It is the one sentence agents see first: say what the app keeps.
- **Exactly one `App`**, in `app.py`. Tests import it and never redefine it. Its name is lower case letters, digits, `-` or `_`, starting with a letter, and at most 33 characters: see "Names" below.
- **`timezone` is the zone of `ctx.now`,** as an IANA name such as `"Europe/Paris"`. It decides the dates the app writes: "today", "this month", the day a sale is dated. Every caller gets the same dates, wherever they are. Ask the person where they are, or where the app's work happens, and set it. Without it, `ctx.now` is in UTC, so a sale at half past midnight in Paris is dated the day before. An unknown name is refused, by `check_app` and when the app loads.
- **`language` is optional:** the language the app's screens are written in, `"en"` or `"fr"`. The renderer writes its own words in it, such as a data grid's Search, Filters and pages, and its numbers and dates: `1 234,50 €` and `9 nov. 2026` in French. Set it to the language you write the app's screens in, the person's. Without it, they are in English.
- **`accent` is optional:** the app's colour, as `#rrggbb`, for its screens, its web page and its tile in Cliquet's lists. Give one even when the app has no screen. Choose a dark shade that fits the subject, so white text reads on it: `check_app` refuses an accent on which white text has a contrast below 4.5:1. See "The accent" in `read_guide("views")`.
- **`title` is optional:** the app's name as people read it, on its tile, its pages and their titles, such as `title="FoodBank HQ"` for an app named `foodbank-hq`. Without it, the name reads with a capital first letter, spaces for `-` and `_`, and every other letter in lower case: "Foodbank hq". Give one when the name has capitals, an acronym or an accent. It is one line of at most 40 characters, in the person's language.
- **`icon` is optional:** the name of one of Lucide's icons, such as `"clapperboard"` for films or `"piggy-bank"` for savings. The app's tile shows it on the accent, in Cliquet's lists and on the app's page, and the app's MCP endpoint gives it to each client as the app's icon, so people and their assistants tell apps apart at a glance. With the accent, it is also the app's icon on a phone's home screen, when a person adds one of its pages there. Search the set with `find_icons`, by a word for the subject, and choose one that says what the app is for. An unknown name is refused, with the closest ones. Without it, the tile shows the app's first letter, and Cliquet's mark reaches each client and the home screen.
- **`files`** are the only files the code may read, write or create, opened by name relative to the current directory. Any other file left behind refuses the change. Each is a name, such as `"rooms.csv"`, or a pattern of names: `*` matches any characters but `/` inside a part, and `**`, a whole part, any folders, none included. So `files=["**/*.beancount"]` declares `ledger.beancount` and `2026/01.beancount`, whether they exist yet or not, and an action may add `2026/02.beancount`. A pattern has at most one `**` and one `*`, and ends in a name's end, such as `*.csv`: `**/*` and `2026/*` are refused, since they would take in a database's journal or a library's cache. Before writing in a new folder, make it: `Path("2026").mkdir(parents=True, exist_ok=True)`.
- **`roles` is optional.**
  - With no roles, anyone may use everything: write `@office.action` and `@office.query`, without arguments.
  - With one role, the same, and that role is the default.
  - With several roles, every action, query and view says `roles=[...]`. Roles don't inherit: list every role that may use it.
  - A role's name is lower case letters, digits, `-` or `_`, and not `mcp`, which Cliquet takes for the app's MCP address.
  - Give agents that only look things up a role with no action, such as the environments' `reader`: a coding agent that reads the register can't change it by mistake.
- **Who is calling.** `ctx.caller.role` is the caller's role, in actions, queries and views. `ctx.caller.id` names the signed-in person, by their Cliquet login, whether they use the app's web page or an assistant. In a preview, it is `"preview"`.
- **Which agent is calling: record `ctx.caller.id` with `ctx.caller.token_id`, compare both, and let Cliquet's own changes through by `ctx.action`.** One person may give several agents their own API token. `ctx.caller.token_id` names the token behind a call, by an opaque id such as `"tok_5e0c9a41b27d83f6"`: the same in every app, and new when the person makes the token again. So an action can record which agent claimed a task, and an invariant can refuse another agent's change to it: a change by another person is refused, and so is one by another token of the same person, when the claim was made with a token. `token_id` is `None` on the web, with a sign-in, in a preview, and for what Cliquet does itself. So let Cliquet's own changes through by `ctx.action` (`app_revert`, `app_publish`, `app_check`), never because `token_id` is `None`: anyone on the web has `None`. An agent isn't told its own id: if it needs it, add a query that returns `ctx.caller.token_id`.

### Reading and writing CSV files

Copy these, with the file's columns. A data file may not exist yet, in a new app and in tests: reading it gives no rows, and the first write adds the header.

```python
import csv
from pathlib import Path

COLUMNS = {ROOMS: ["id", "name", "capacity", "opens", "closes", "days"]}


def _read(file: str) -> list[dict[str, str]]:
    """The rows of a data file, none if it doesn't exist yet."""
    try:
        with open(file, newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def _append(file: str, row: list[object]) -> None:
    """Append a row, writing the header first if the file is new."""
    new = not Path(file).exists()
    with open(file, "a", newline="") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(COLUMNS[file])
        writer.writerow(row)
```

- **Adding a record** appends a row. **Changing one** rewrites the file: read the rows, change the one, write them all with `csv.DictWriter` and the header. See `update_room` in the meeting-rooms example.
- **Values are strings** in the file. Write dates with `.isoformat()` and decimals with `f"{price:.2f}"`. Read them back with `date.fromisoformat(row["date"])`, `Decimal(row["price"])` and `int(row["capacity"])`.

### Actions

```python
@office.action(roles=["member", "admin"])
def cancel(ctx: ActionContext, booking: BookingId) -> None:
    """Cancel a booking that hasn't started. Members cancel their own; an admin cancels any."""
    rows = _read(BOOKINGS)
    found = next((b for b in rows if b["id"] == booking and b["status"] == "booked"), None)
    if found is None:
        raise Refused(f"There is no booking {booking} to cancel. See `bookings`.")
    _write(BOOKINGS, [{**b, "status": "cancelled"} if b["id"] == booking else b for b in rows])
```

- `ctx` comes first, typed `ActionContext`. It gives `ctx.caller` and `ctx.now`, a timezone-aware datetime in the app's `timezone`.
- Every argument is typed. The docstring is the tool's description. Annotate the return type: `-> None` when there is nothing to say.
- An action may return a model that says what it did, such as `-> Imported` with how many lines it added and which it skipped as already recorded. Do it when the caller can't easily know it afterwards. The agent gets it with the outcome, as "The action answered: {...}" and in the structured content's `result`. A screen doesn't show it: after a call, the view is laid out again from the data. It isn't stored, so a call retried with the same `app_request_id` doesn't get it again. Like a query's result, it holds only what the roles that may call the action may read, must read back from its JSON, and is at most 1 MB as JSON: a larger one fails the call. `try_call` shows it.
- `raise Refused("...")` refuses the change. `Refused` is imported from `cliquet`.
- Write `@office.action(roles=["admin"], destructive=True)` for an action that removes or overwrites something the person can't add back with one call, such as removing a record with its links, or replacing text they would have to type again. A client may then ask the person before each call. Leave it off for an action that only adds, or one that a single call undoes, such as `update_room`.
- Give each record a key that agents and people can quote. Take it from the data when there is one, such as a receipt number. If you number records yourself, never reuse a number, even after a record is removed. A record people change, such as a booking moved to another hour, gets an action that changes it in place, keeping its key: removing it and adding it again loses the key, and lets someone else take its place in between.

### A file the person uploads

An action may take a file the person uploads, such as a bank's export or a spreadsheet, so the agent never reads it into its context nor copies it into the call: a long file costs it tokens twice, and may change on the way. Give the action one argument typed `File`, imported from `cliquet`:

```python
from cliquet import File


@ledger.action
def import_export(ctx: ActionContext, export: File) -> Imported:
    """Import the bank's CSV export the person uploads: the agent never reads it."""
    with open(export, newline="") as f:
        rows = list(csv.DictReader(f))
    ...
```

- **The action gets a path** to the file in its working folder, named after the person's file, and reads it as an ordinary file. It is read-only, and it isn't data: it is gone once the action returns, and before the invariants run. To keep it, write what the app needs into a declared data file.
- **An action takes one file at most,** and `File | None = None` makes it optional. A file can't be part of a model, a list or a dict, nor what a query or a view takes, nor a result: `check_app` refuses it.
- **The agent asks for it with `app_ask_for_file`,** a tool Cliquet adds for a role that has such an action, which the person's assistant calls with `ask_for_app_file`. It answers a reference at once, and the person uploads the file in a panel in the conversation, or on a page its link opens. The agent then passes the reference as the argument. The tool's schema says so: the argument is a string of the format `app_file`.
- **A reference works once,** for the person who asked for it: the call that keeps a change uses it up, inside a transaction too. A refused call, or one that changes nothing, leaves it for the next call. It ends an hour after the file came, or an hour after it was asked for if no file came.
- **A file is at most 5 MB.** For a larger file, or one the person keeps adding to, upload it to the draft as data with `ask_for_file` before the first publish.
- **Use one when the person has the file and the agent has nothing to decide in it,** such as an export to import as it is. When the agent must read the records first, to choose or fix them, let it pass them as arguments, as `import_statement` takes its lines in the ledger example.
- **`try_call` can't give an action a file:** test it in the app's tests, which give it the file's bytes or a path. See `read_guide("tests")`.

### Invariants

```python
@office.invariant("Bookings of the same room never overlap")
def no_overlap(ctx: InvariantContext) -> list[str]:
    rooms = _rooms()
    # Only bookings of one room on one day can overlap: compare those, not every pair.
    same_day: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for b in _active():
        same_day[b["room"], b["date"]].append(b)
    return [
        f"{_label(b, rooms)} overlaps {_label(a, rooms)}"
        for day in same_day.values()
        for i, a in enumerate(day)
        for b in day[i + 1 :]
        if _overlap(a, b)
    ]
```

- The description is the rule, in one line people read. Each description is unique.
- Return every problem found, an empty list when the data is valid.
- `ctx.before` is a `Path` to the data before the change, for rules about change itself. See `reconciled` in the ledger example.
- `ctx.action` names the action that made the change. What Cliquet does itself has a name no action can have: `app_revert` for an undo, `app_publish` for a new version's migration, `app_check` when it checks the data without changing it, as before a version goes live, and `app_change` for `world.change` in a test.
- Invariants get no clock, so a rule can't break overnight. The data is read-only here. An invariant that raises fails the call, as an action that raises does: nothing is kept, and only the owner reads the error.

### Queries

```python
class Availability(BaseModel):
    room: str
    free: bool
    why_not: Annotated[list[str], Field(description="Why the room can't take it, empty when free")]


@office.query(roles=["member", "admin"])
def availability(
    ctx: QueryContext, date_: Day, start: Time, end: Time, attendees: int = 1
) -> list[Availability]:
    """Which rooms could take a meeting, the free ones first, and why each other one can't."""
```

- Return a `BaseModel`, a list of them, or a plain value. The return type is the tool's output schema.
- Callers get the result as JSON, each field by its alias, and a test's `query` rebuilds it from that JSON. So it must read back: `check_app` refuses a model in a result, at any depth, with a `field_serializer`, a `model_serializer`, a `computed_field`, a field with `exclude`, or a `serialization_alias` that isn't also read, and a `PlainSerializer` or a `WrapSerializer` anywhere in its type. For a public name, use `Field(alias=...)`. A result that doesn't match the return type fails the call: return that type exactly, such as `Decimal(0)` rather than `0` for `-> Decimal`, and a list rather than a tuple for `-> list[...]`.
- Queries are read-only. They see the data as the last change left it. `ctx.now` gives "this month" or "the last 30 days".
- Cliquet adds `app_data_version` to every query's answer, beside the result: the version of the data the query read, the id of its commit. An agent passes it to an action as `app_if_unchanged_since`, and the action is refused if the data changed meanwhile. The app's code sees neither. A result whose own fields start with `app_` is refused when the app is checked, as an action's argument is.
- A query may call another query: `sizes = {r.id: r.capacity for r in rooms(ctx)}`.
- **Every name an agent must pass has a query that lists it,** such as `accounts` in the ledger or `rooms` in the meeting rooms, and the argument's description names that query: "as listed by `accounts`". Otherwise an agent guesses, and the call is refused.
- A query may `raise Refused(...)`, for example when asked about something that doesn't exist. A filter given a value no record has, such as a misspelt customer, refuses and names the values there are: an empty list would read as "none".
- **A write that may be refused has a query that says what would pass,** and why each other choice wouldn't, such as `availability` before `book`. Write each rule once, as a function that the invariant, the action and the query all call: their words then agree.
- **A query over data that grows takes an argument that narrows it,** such as a period, a search or a limit, with a default that keeps the answer short. An answer goes into the caller's context: one larger than 1 MB as JSON isn't sent, and the caller is told to ask again with narrower arguments. So is what `app_changes_since` answers. A query that a view calls has no such limit: only the view's screen leaves the app.

## Where each rule goes

| The rule is about | Put it in | Example |
| --- | --- | --- |
| One argument's form | `Field`, so agents see it before calling | more than zero, a month as `YYYY-MM`, not blank: `Field(pattern=r"\S")` |
| The data or the time | The action, with `raise Refused(...)` | the room exists; a booking can't start in the past |
| What the data must always look like | An invariant | bookings of a room never overlap; what a statement confirmed never changes |

A rule can be in both places. The action refuses early, with a message about the call ("There is no room cedar"), and the invariant guards every other way the data changes, such as another action, an import or a migration. Then the invariant only refuses in tests through `world.change`: see `read_guide("tests")`.

Refusal messages name things as the person knows them: a booking's number, a room, a dated entry. Never put a line number in place of what the person knows: agents can't see the file.

**Behaviour follows a setting, never a name.** When the code treats some records differently, such as the asset classes the ledger asks a yearly value for, give them a column that says so, such as the ledger's `upkeep`. Code that matches a name people can change, such as a class called "Property", stops working the day they rename it.

Arguments:

- Name repeated argument types once, with a description that tells the agent what to send:

  ```python
  RoomId = Annotated[str, Field(description="A room's id, such as oak, as listed by `rooms`")]
  People = Annotated[int, Field(gt=0, le=500, description="How many people will attend")]
  ```

- Arguments may be `str`, `int`, `Decimal`, `bool`, `date`, `datetime`, `Literal`, a `BaseModel` such as `lines: list[ImportLine]`, and `| None` of these. Agents send them as JSON, dates as ISO 8601. An action may also take one `File`: see "A file the person uploads" above. No `*args` or `**kwargs`.
- If a description says "as listed by `rooms`", the app needs that query: agents look values up before calling.
- Times from another system, such as when an email was sent, arrive as `datetime` arguments with a time zone. Store them as ISO 8601 in UTC.
- If an argument's natural name hides something, such as `date`, which hides the `date` type, give it a public name: `date_: Annotated[date, Field(alias="date")]`. Agents send `date`. Tests and Python code use the Python name: `call(add, date_=...)`.
- Names that start with `app_`, in any case, are the tools every Cliquet app has, such as `app_history`, and the arguments Cliquet adds, `app_request_id`, `app_remark`, `app_if_unchanged_since` and `app_transaction`. No function or argument may use them: the app's check refuses it. Any other name is the app's, `history` and `transaction` included.

### Names

**Name every function, argument and field in lower-case ASCII, and keep names short.** Write `resultats`, not `résultats`, and `unit_price`, not `unitPrice`, whatever language the app is in. The person's language goes in the docstrings, the descriptions, the refusals, the screens, each view's `title`, which words its tab (see `read_guide("views")`), and an argument's or a field's `title`: `quantite: Annotated[int, Field(title="Quantité")]`.

The reason: an agent sees each action, query and view as a tool, named after its function, with a schema that names its arguments and the fields of the models it takes and returns. The MCP specification allows more, such as capitals, but a client hands these names to its model's API, which may accept less: Claude's refuses a name in a schema with an accent or over 64 characters, and the whole request with it. So `check_app` refuses these, and says what to rename:

- **A name outside lower-case ASCII letters, digits and `_`, starting with a letter, or over 64 characters.** This holds for the name of an action, a query, a view and an argument, an argument's alias too, and of every field of a model they take or return, in a list, an optional or a nested model too, by its alias when it has one: `record_sale`, `resultats`, `date_`, `unit_price`. No accent, no capital, no other script. The keys of a `dict` and the values of a `Literal` are data, and may be anything.
- **A full tool name over 64 characters.** Some clients name a tool `mcp__<app>__<tool>`, and accept at most 64 characters. Every app has Cliquet's `app_rollback_transaction`, so an app's name is at most 33 characters. A function's name is at most what the app's name leaves: 53 characters in an app named `shop`. Keep both short.

## Who reads what

Cliquet shows the data only through the app's code: the queries and views each role may use, and what the app writes. It adds nothing from the data on its own. Each thing the app writes has its own readers:

- an action's result goes to the caller;
- a refusal's message, from an action or an invariant, goes to any role that may call the action;
- `instructions.md` goes to every role.

So each says only what those readers' roles may read through their queries. An invariant may name a place as `<file>:<line>: <message>`, such as `ledger.beancount:412: the entry doesn't balance`, and quote the entry it is about when every role that may write may also read it.

When the app's code fails, only the app's owner reads the error's text. Anyone else gets its type and an error id. Cliquet keeps the error for the owner, who reads it by that id: see "When the live app fails" in `read_guide("publishing")`.

## Rules for app code

- **Only the declared files.** No network, no other paths.
- **The time is `ctx.now`.** `datetime.now()`, `date.today()` and `time.time()` are rejected anywhere in `app.py`.
- **No state between calls, and no randomness.** The same data and arguments give the same result, so a write is safe to run again. `check_app` rejects `random`, `secrets`, `uuid.uuid1`, `uuid4`, `uuid6` and `uuid7`, `os.urandom` and `os.getrandom` anywhere in `app.py`. Number records from the data instead.
- **A cache keyed only on what the code reads is fine,** such as `functools.lru_cache` on a ledger's text. Module state lasts between calls, since the app's process stays up, and the same text still gives the same result.

## Data formats

Plain text by default: CSV with the `csv` module, Markdown, or a domain format read by a declared library. History then shows readable changes, and `app_revert` can undo any of them.

SQLite works, at a cost:

- history holds binary copies;
- `app_revert` only undoes the latest change;
- every write stores the whole file again;
- connections must be closed inside the action, or its `-wal` and `-shm` files are refused;
- queries and invariants open it read-only: `sqlite3.connect("file:x.sqlite?mode=ro", uri=True)`.

## Recipes

**Importing from another system.** The agent fetches records with its own connector and passes them to one action. The action skips the records already stored, by their key in that system, so running an import twice is harmless. A query gives the point to resume from. An invariant still guards against duplicates.

```python
    recorded = {row["ref"] for row in _read(ORDERS)}
    for order in orders:
        if order.ref not in recorded:
            _append(ORDERS, [order.ref, order.day.isoformat(), order.total])
```

See `import_statement` and `status` in the ledger example, and `upsert_environment` in the environments example, which answers `added`, `updated` or `unchanged`.

**Apps over a text format.** When the data is a format with its own checker, such as a Beancount ledger, give the agent a few precise tools rather than one that takes the whole file:

- **Queries that find lines and entries:** one that searches the text and gives each entry's first line and its whole text, and one for each question the agent asks before writing, such as where each account stops, an account's postings with a running balance, or how a payee was filed before.
- **One action per job,** such as an import that takes a statement's rows, skips those already there, and writes the rest in one fixed layout. Keep an action that appends raw text for what no job covers.
- **One action that replaces exact text,** refused unless the text appears once. The agent copies it from the search query.
- **Invariants that pass the checker's errors** as `<file>:<line>: <message>`, followed by the entry, when every role that may write may also read it.
- **A transaction for a big write:** the agent imports, checks with the app's queries, fixes, then commits. `instructions.md` says so.
- **A result that says what happened:** what was added, what was skipped as already there, and what was chosen for the caller.
- **A rule that the data includes only files the app declares and runs no code,** when the format can include files or load plugins, as Beancount's `include` and `plugin` do. A ledger kept as a file a month declares `files=["**/*.beancount"]`, and its top file includes each month by its name, so an import into a new month writes its file and its `include` line in one change: allow only includes of names the app declares. The ledger example keeps one file, so it refuses every `include`. Check it by parsing only, and load the file only once it passes. An app that needs a plugin lists the ones it allows. An argument a library reads, such as a query, mustn't read other files either: beanquery's `CREATE TABLE … USING 'csv:/path'` does, so allow only the statements the app needs.
- **Libraries that write cache files next to the data:** turn the cache off at the top of `app.py`, since a change that leaves a file the app doesn't declare is refused. Beancount's is `loader.initialize(use_cache=False)`.

See `import_statement`, `fix_entry`, `find_entries`, `valid` and `reconciled` in the ledger example: `read_example("ledger", "app.py")`.

**Sending to another system.** Use an outbox file, declared like any data file:

1. An action queues an item with a stable key, in the same change as its own. Queuing it again changes nothing.
2. A query lists the items not sent yet.
3. The agent sends each one with its own connector, putting the key in what it sends, such as an email's subject.
4. A second action marks the item sent, with the other system's reference.
5. An invariant ensures a sent item never changes.

`instructions.md` tells the agent: if unsure whether an item went out, look its key up in the other system before sending again. No example sends anything: write these few lines yourself, after the import recipe's pattern.

## Before you call check_app

- [ ] `app.py` starts with the block, then a one-sentence docstring.
- [ ] Every action, query and view has a docstring, and every argument a type, plus a description unless it's obvious.
- [ ] With several roles, every action, query and view names its roles.
- [ ] Every function, argument and field of a model has a lower-case ASCII name, aliases included, and a view whose tab needs other words has a `title`.
- [ ] Each rule is in its place, and refusals name things as the person knows them.
- [ ] Code reading data files handles a file that doesn't exist yet.
- [ ] A query over data that grows takes an argument that narrows it.

`check_app(app)` then builds the app. It returns the problems of its definition, each with what to fix, or the definition itself: its tools, rules, roles and files. It also runs every rule on the current data and lays out every view.

## Not possible yet: stop and say so

If a request needs one of these, tell the person it isn't possible yet and offer the alternative. Don't work around it: `check_app` rejects `subprocess`, `os.system` and the like, and any change to `sys.path`, `site.addsitedir` included, anywhere in `app.py`.

- **A screen that reaches the network or brings its own JavaScript.** Offer: a view that calls the app's queries and actions (see `read_guide("views")`).
- **Code in several files.** A draft holds one module, `app.py`. Offer: keep it in `app.py`, or use a published package, listed in the block at the top.
- **An external program,** such as `hledger`. Programs can't be declared. Offer: a Python library that does the same, listed in the block at the top.
- **Records only one person may read,** such as "my spending". `ctx.caller.id` names the signed-in person, so an action can record who made a change, and a query or a view can show each person their own records. But everyone with a role reads the same data files, through every query that role may use: filtering by `ctx.caller.id` or `ctx.caller.token_id` hides records, it doesn't protect them. Offer: filter by `ctx.caller.id`, and tell the person that members can still reach each other's records through the other queries.
