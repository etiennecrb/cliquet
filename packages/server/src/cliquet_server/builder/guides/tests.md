# Writing the tests

Tests are Python files in the draft, named `tests/test_<name>.py`. They use Cliquet's harness and pytest. `run_tests(app)` runs them.

`read_example("meeting-rooms", "tests/test_meeting_rooms.py")`, `read_example("environments", "tests/test_environments.py")` and `read_example("ledger", "tests/test_ledger.py")` are complete test files. A shortened version of the first:

```python
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from app import add_room, book, bookings, cancel, no_overlap, office

from cliquet.testing import Connection, Denied, InvalidArguments, World, given

# Thursday 26 November 2026, 08:00 in Paris.
NOW = datetime(2026, 11, 26, 8, 0, tzinfo=ZoneInfo("Europe/Paris"))
FRIDAY = date(2026, 11, 27)

ROOMS = """\
id,name,capacity,opens,closes,days
oak,Oak,8,08:00,19:00,"mon,tue,wed,thu,fri"
"""

BOOKINGS = """\
id,room,date,start,end,attendees,title,booked_by,status
B-0001,oak,2026-11-27,14:00,15:30,8,Client call,camille,booked
"""


def world() -> World:
    return given(office, {"rooms.csv": ROOMS, "bookings.csv": BOOKINGS}, now=NOW)


def member() -> Connection:
    return world().connect("member", id="sam")


def book_oak(conn: Connection, start: str, end: str = "11:00", **more):
    args = {"room": "oak", "date_": FRIDAY, "start": start, "end": end}
    return conn.call(book, **{**args, "attendees": 4, "title": "Sync", **more})


def test_a_booking_is_numbered_and_listed() -> None:
    m = member()
    made = book_oak(m, "10:00").expect_ok()
    assert made.id == "B-0002" and made.booked_by == "sam"
    assert made in m.query(bookings, mine=True)


def test_a_clash_is_refused_with_the_booking_it_meets() -> None:
    # A refusal names the booking as the person knows it, not a line in a file.
    book_oak(member(), "15:00", "16:00").expect_refused(
        message="Oak is already booked 14:00–15:30 (Client call, B-0001)"
    )


def test_an_overlap_written_straight_into_the_file_is_refused() -> None:
    row = "B-0002,oak,2026-11-27,15:00,16:00,2,Sneaky,sam,booked\n"
    world().change("bookings.csv", append=row).expect_refused(by=no_overlap)


def test_a_member_cannot_cancel_someone_else_s_booking() -> None:
    member().call(cancel, booking="B-0001").expect_refused(
        message="B-0001 was booked by camille: only they or an admin may cancel it"
    )


def test_a_time_not_written_hh_mm_is_an_invalid_argument() -> None:
    with pytest.raises(InvalidArguments, match="pattern"):
        book_oak(member(), "9:00")


def test_members_cannot_add_rooms() -> None:
    with pytest.raises(Denied):
        member().call(add_room, room="elm", name="Elm", capacity=2, opens="09:00", closes="17:00")
```

## The harness

- **Setup.** `given(app, files, now=...)` returns a `World`: the app on these files only, with the clock fixed. `now` has a time zone, and `ctx.now` is that instant in the app's `timezone`. Without `now`, the clock is the time the test starts. Files not given start absent: leave a file out to start with no data. A file a pattern declares is given by its name, such as `"2026/01.beancount"`, and a pattern that matches no file given starts with none. An empty string makes an empty file, with no header line. Tests never read the draft's `data/` files: they are the app's first data, gone from the draft after the first publish. `check_app` and `run_tests` refuse a test that names a path in `data/`.
- **Callers.** `world.connect(role)` returns a `Connection`. Leave the role out when the app has one role or none. `world.connect(role, id="ana")` calls as the person whose login is `ana`, for a rule on `ctx.caller.id`: two ids are two people. Without it, `ctx.caller.id` is `"test"`. `world.connect(role, token_id="tok_a")` calls through an API token, for a rule on `ctx.caller.token_id`: two of them are two agents of one person. Without it, `token_id` is `None`, as on the web.
- **Actions.** `call(action, **args)` returns an outcome:
  - `.expect_ok()` when the change was kept. It returns what the action returned, as its own type: `imported = owner.call(import_statement, ...).expect_ok()` gives an `Imported`.
  - `.expect_refused(by=invariant)` or `.expect_refused(message="...")` when it was refused. `message` is compared whole. Both return the messages, so you can check their wording.
  - `.commit` is `None` when nothing changed.
  - **A file argument** takes the file itself, never a reference: a `Path` to a file the test writes, such as `tmp_path / "march.csv"` with pytest's `tmp_path`, or the file's bytes. The action reads it as it reads a file the person uploads: `owner.call(import_export, export=path).expect_ok()`. A `str` is refused, since an agent passes a reference there.
- **Queries.** `query(query, **args)` returns the result, rebuilt from the JSON an agent gets, as its own type, as `expect_ok` does. Don't call the query function yourself in a test: it needs a `ctx`. Compare the result to models with plain `assert`. A query that refuses raises `Refused`.
- **Views.** `view(view, **args)` lays a view out for that caller and returns the screen: see "Testing views" below.
- **Errors.** An argument breaking its `Field` raises `InvalidArguments`. A role that may not call raises `Denied`. Test them with `pytest.raises`. `cliquet.testing` exports both, and `Refused`.
- **Testing an invariant alone.** `world.change(file, replace=(old, new))` or `append=` edits a file directly, bypassing actions, and returns an outcome like `call`. A file a pattern declares may be a new one, in a new folder. The invariants see it as `ctx.action == "app_change"`.
- **A write that depends on a read.** `connection.data_version` is the `app_data_version` a query's answer gives it now, and `connection.if_unchanged_since(version).call(...)` makes a call as an agent that passes it: refused if the data changed meanwhile. Use it when `instructions.md` tells agents to pass it: read, let another caller change the data, and check that the call is refused, then that it runs with the new version.
- **Transactions.** `connection.begin()` returns a `Transaction`, with the same `call`, `query` and `view`, which read and change its data only, then `commit()`, which returns an outcome like `call`, and `rollback()`. Use it when `instructions.md` tells agents to use one: check that the calls together pass, that a query inside sees them and one outside doesn't, then commit. A refusal inside a transaction counts for the rule that every invariant refuses once. A call with a transaction that ended raises `InvalidArguments`, saying how it ended.
- **Arguments by name.** Pass them by name, through small typed helpers like `staff()` above.

Import the app's functions from `app`, and the harness from `cliquet.testing`. `pytest` is there for the tests: don't list it in the block at the top of `app.py`.

## Testing views

`view` lays out a view for a caller, with the same checks as when it is served, and returns the screen as the browser gets it, as a dict. `screen["arguments"]` are the arguments that draw it again, except those left at their default, and `str(screen)` holds every text it shows and every tool it calls:

```python
def test_the_week_opens_on_this_week() -> None:
    screen = member().view(week)
    assert screen["arguments"] == {"week": "2026-11-23"}
    assert "Client call" in str(screen) and "Oak" in str(screen)


def test_only_admins_have_the_admin_screen() -> None:
    w = world()
    assert "Add a room" in str(w.connect("admin").view(administration))
    with pytest.raises(Denied):
        w.connect("member").view(administration)
```

Test what a form does by calling its action, as for any other call.

## What to cover

- Each action, once where it works: `expect_ok`, then a query that shows the change. When the action returns a result, check it too.
- **Every invariant must refuse at least once,** through an action or `world.change`. Otherwise the run fails, and its output names the invariant.
- Each `Refused` an action raises, with its message.
- A rule that follows a running state, such as a stock or who holds a tool: a sequence that goes there and back, such as lend, return, lend again.
- With several roles: a role that may not use a tool, with `Denied`.
- The app with no data yet: `given(app, {}, now=NOW)`.
- Each view, for each role that sees something different, with no data, and in each state its arguments can put it in.

## Running them

`run_tests(app)` checks the app's definition, then runs every test, in the app's own environment. It returns whether they passed, and the output. The output says which test failed and why. Fix the app or the test, and run them again until they pass.

`run_tests(app, only="sale")` runs only the tests whose name holds `sale`.

## Trying a call by hand

`try_call(app, tool, role, arguments)` runs one action or query as an agent would call it, on the preview: see step 7 of `read_guide("start")`. It returns the result or the refusal, in the words an agent will get.

To rehearse a transaction as an agent would, call `app_begin_transaction` with `try_call`, pass its id as `app_transaction` to the calls, check the result with a query in it, then call `app_commit_transaction` or `app_rollback_transaction`. The preview has one transaction at a time, shared by every role. A change to `app.py`, its tests or `instructions.md` ends it, as a publish would.

To rehearse a write that depends on a read, call the query with `try_call`, then the action with the answer's `app_data_version` as `app_if_unchanged_since`. Make another change in between to see the refusal an agent gets.

It shows how a call reads to an agent. It doesn't replace a test. It can't give an action a file, nor call `app_ask_for_file`: the preview takes no upload, so test an action that takes a file in the tests.
