# /// script
# requires-python = ">=3.14"
# dependencies = ["cliquet-sdk>=0.0.1,<0.1"]
#
# [tool.uv.sources]
# cliquet-sdk = { path = "../../packages/sdk", editable = true }
# ///
"""Meeting rooms of a small office: rooms with their seats and opening hours, closing days,
and bookings that never clash."""

import csv
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated

from cliquet import (
    ActionContext,
    App,
    BaseModel,
    Field,
    InvariantContext,
    QueryContext,
    Refused,
    Screen,
)

ROOMS = "rooms.csv"
CLOSURES = "closing_days.csv"
BOOKINGS = "bookings.csv"
COLUMNS = {
    ROOMS: ["id", "name", "capacity", "opens", "closes", "days"],
    CLOSURES: ["date", "room", "reason"],
    BOOKINGS: ["id", "room", "date", "start", "end", "attendees", "title", "booked_by", "status"],
}

office = App(
    "meeting-rooms",
    title="Meeting rooms",
    files=[ROOMS, CLOSURES, BOOKINGS],
    roles=["admin", "member"],
    accent="#4338ca",
    icon="calendar-clock",
    timezone="Europe/Paris",
    language="en",
)

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_PATTERN = f"({'|'.join(WEEKDAYS)})"
LONGEST_CLOSURE = 92

# Argument types: what agents see before calling.
RoomId = Annotated[str, Field(description="A room's id, such as oak, as listed by `rooms`")]
BookingId = Annotated[
    str,
    Field(
        pattern=r"^B-\d{4,}$",
        description="A booking's number, such as B-0007, as listed by `bookings`",
    ),
]
Time = Annotated[
    str,
    Field(
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="A time of day, as HH:MM on a 24-hour clock",
    ),
]
Day = Annotated[date, Field(alias="date", description="The day, as YYYY-MM-DD")]
People = Annotated[int, Field(gt=0, le=500, description="How many people will attend")]
Days = Annotated[
    str,
    Field(
        pattern=rf"^{DAY_PATTERN}(,{DAY_PATTERN})*$",
        description="The days of the week the room is open, comma-separated, "
        "such as mon,tue,wed,thu,fri",
    ),
]
Seats = Annotated[int, Field(gt=0, le=500, description="How many people the room seats")]
Name = Annotated[str, Field(pattern=r"\S", max_length=80)]
Scope = Annotated[
    str | None,
    Field(description="One room, as listed by `rooms`; leave it out for the whole office"),
]


class Room(BaseModel):
    id: str
    name: str
    capacity: Annotated[int, Field(description="Seats")]
    opens: Annotated[str, Field(description="HH:MM")]
    closes: Annotated[str, Field(description="HH:MM")]
    days: Annotated[list[str], Field(description="Days of the week it is open: mon to sun")]


class ClosingDay(BaseModel):
    date: date
    room: Annotated[str | None, Field(description="The room closed, none when the whole office is")]
    room_name: str | None
    reason: str


class Booking(BaseModel):
    id: str
    room: str
    room_name: str
    date: date
    start: Annotated[str, Field(description="HH:MM")]
    end: Annotated[str, Field(description="HH:MM")]
    attendees: int
    title: str
    booked_by: Annotated[str, Field(description="The Cliquet login of who booked it")]


class Availability(BaseModel):
    room: str
    name: str
    capacity: int
    free: bool
    why_not: Annotated[list[str], Field(description="Why the room can't take it, empty when free")]


# Reading and writing the files.


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


def _write(file: str, rows: list[dict[str, str]]) -> None:
    with open(file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS[file])
        writer.writeheader()
        writer.writerows(rows)


def _rooms() -> dict[str, dict[str, str]]:
    return {r["id"]: r for r in _read(ROOMS)}


def _closures() -> dict[tuple[str, str], str]:
    """Each closing day's reason, by (date, room); the room is "" when the whole office is
    closed."""
    return {(c["date"], c["room"]): c["reason"] for c in _read(CLOSURES)}


def _active() -> list[dict[str, str]]:
    """The bookings that stand: every one not cancelled."""
    return [b for b in _read(BOOKINGS) if b["status"] == "booked"]


def _known_room(room: str) -> dict[str, str]:
    rooms = _rooms()
    if room not in rooms:
        raise Refused(f"There is no room {room}. See `rooms` for their ids.")
    return rooms[room]


# How things read for people.


def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%a} {d.day} {d:%B %Y}"


def _days_label(days: str) -> str:
    """mon,tue,wed,thu,fri reads Mon–Fri; a broken run lists its days."""
    index = sorted(WEEKDAYS.index(d) for d in days.split(","))
    names = [WEEKDAYS[i].capitalize() for i in index]
    if len(index) == 7:
        return "Every day"
    if len(index) > 2 and index == list(range(index[0], index[-1] + 1)):
        return f"{names[0]}–{names[-1]}"
    return ", ".join(names)


def _people(count: int) -> str:
    return "1 person" if count == 1 else f"{count} people"


def _label(b: dict[str, str], rooms: dict[str, dict[str, str]]) -> str:
    """How a refusal names a booking: its number, room, day and times."""
    room = rooms[b["room"]]["name"] if b["room"] in rooms else b["room"]
    return f"Booking {b['id']} ({room}, {_day(b['date'])}, {b['start']}–{b['end']})"


def _at(day: str, hhmm: str, ctx: ActionContext | QueryContext) -> datetime:
    return datetime.combine(
        date.fromisoformat(day), time.fromisoformat(hhmm), tzinfo=ctx.now.tzinfo
    )


def _clock(hhmm: str) -> int:
    """Minutes since midnight."""
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _plus(hhmm: str, minutes: int) -> str:
    """The time of day `minutes` later, as HH:MM. Past midnight it reads 24:00 or more,
    which no room's hours allow."""
    total = _clock(hhmm) + minutes
    return f"{total // 60:02d}:{total % 60:02d}"


# The four rules, one function each, shared by the invariants, `book`, `move` and
# `availability`.


def _outside_hours(b: dict[str, str], room: dict[str, str]) -> str | None:
    d = date.fromisoformat(b["date"])
    if WEEKDAYS[d.weekday()] not in room["days"].split(","):
        return f"{room['name']} is closed on {d:%A}s (open {_days_label(room['days'])})"
    if b["start"] < room["opens"] or b["end"] > room["closes"]:
        return f"{room['name']} is open {room['opens']}–{room['closes']} only"
    return None


def _closing_day(
    b: dict[str, str], room: dict[str, str], closures: dict[tuple[str, str], str]
) -> str | None:
    if (b["date"], "") in closures:
        return f"{_day(b['date'])} is a closing day for the office ({closures[b['date'], '']})"
    if (b["date"], b["room"]) in closures:
        return f"{room['name']} is closed on {_day(b['date'])} ({closures[b['date'], b['room']]})"
    return None


def _too_many(b: dict[str, str], room: dict[str, str]) -> str | None:
    if int(b["attendees"]) > int(room["capacity"]):
        return f"{_people(int(b['attendees']))} won't fit: {room['name']} seats {room['capacity']}"
    return None


def _overlap(a: dict[str, str], b: dict[str, str]) -> bool:
    return (
        a["room"] == b["room"]
        and a["date"] == b["date"]
        and a["start"] < b["end"]
        and b["start"] < a["end"]
    )


def _problems(
    b: dict[str, str],
    rooms: dict[str, dict[str, str]],
    closures: dict[tuple[str, str], str],
    active: list[dict[str, str]],
) -> list[str]:
    """Everything that stops this booking from standing among the `active` ones, in the
    person's words."""
    room = rooms[b["room"]]
    found = [_outside_hours(b, room), _closing_day(b, room, closures), _too_many(b, room)]
    clashes = [
        f"{room['name']} is already booked {o['start']}–{o['end']} ({o['title']}, {o['id']})"
        for o in active
        if o["id"] != b["id"] and _overlap(b, o)
    ]
    return [p for p in found if p] + clashes


def _timing(day: str, start: str, end: str, ctx: ActionContext | QueryContext) -> str | None:
    """Why a meeting can't be booked at these times in any room, or None."""
    if end <= start:
        return f"The meeting must end after it starts: {start}–{end}"
    if _at(day, start, ctx) < ctx.now:
        return f"{_day(day)} at {start} is already past"
    return None


def _check(b: dict[str, str], ctx: ActionContext) -> None:
    """Refuse a booking, new or moved, that can't stand, with every reason at once."""
    if problem := _timing(b["date"], b["start"], b["end"], ctx):
        raise Refused(problem)
    if problems := _problems(b, _rooms(), _closures(), _active()):
        raise Refused("; ".join(problems))


# Actions.


@office.action(roles=["member", "admin"])
def book(
    ctx: ActionContext,
    room: RoomId,
    date_: Day,
    start: Time,
    end: Time,
    attendees: People,
    title: Annotated[
        str, Field(pattern=r"\S", max_length=120, description="What the meeting is for")
    ],
) -> Booking:
    """Book a room for a meeting on one day. Refused if it clashes, is outside the room's
    hours, on a closing day, or too big for the room. Check `availability` first to find a
    room."""
    rooms = {room: _known_room(room)}
    numbers = [int(b["id"].removeprefix("B-")) for b in _read(BOOKINGS)]
    booking = {
        "id": f"B-{max(numbers, default=0) + 1:04d}",
        "room": room,
        "date": date_.isoformat(),
        "start": start,
        "end": end,
        "attendees": str(attendees),
        "title": title.strip(),
        "booked_by": ctx.caller.id,
        "status": "booked",
    }
    _check(booking, ctx)
    _append(BOOKINGS, [booking[c] for c in COLUMNS[BOOKINGS]])
    return _booking(booking, rooms)


def _own(booking: str, rows: list[dict[str, str]], ctx: ActionContext, verb: str) -> dict[str, str]:
    """The standing booking the caller may change, or a refusal saying why not."""
    found = next((b for b in rows if b["id"] == booking and b["status"] == "booked"), None)
    if found is None:
        raise Refused(f"There is no booking {booking} to {verb}. See `bookings`.")
    if ctx.caller.role != "admin" and found["booked_by"] != ctx.caller.id:
        raise Refused(
            f"{booking} was booked by {found['booked_by']}: only they or an admin may {verb} it"
        )
    if _at(found["date"], found["start"], ctx) <= ctx.now:
        raise Refused(f"{_label(found, _rooms())} has already started")
    return found


@office.action(roles=["member", "admin"])
def cancel(ctx: ActionContext, booking: BookingId) -> None:
    """Cancel a booking that hasn't started. Members cancel their own; an admin cancels any."""
    rows = _read(BOOKINGS)
    _own(booking, rows, ctx, "cancel")
    _write(BOOKINGS, [{**b, "status": "cancelled"} if b["id"] == booking else b for b in rows])


@office.action(roles=["member", "admin"])
def move(
    ctx: ActionContext,
    booking: BookingId,
    date_: Annotated[date | None, Field(alias="date", description="The new day")] = None,
    start: Annotated[
        Time | None, Field(description="The new start; alone, it keeps the length")
    ] = None,
    end: Annotated[Time | None, Field(description="The new end")] = None,
    room: Annotated[str | None, Field(description="The new room, as listed by `rooms`")] = None,
    attendees: Annotated[People | None, Field(description="The new number of people")] = None,
) -> Booking:
    """Move a booking that hasn't started to another day, time or room, or change how many
    attend. What is left out stays, and it keeps its number. Refused as `book` is when the
    new slot doesn't work. Members move their own; an admin moves any."""
    rows = _read(BOOKINGS)
    found = _own(booking, rows, ctx, "move")
    if room is not None:
        _known_room(room)
    if start is not None and end is None:
        end = _plus(start, _clock(found["end"]) - _clock(found["start"]))
    change = {
        "date": date_.isoformat() if date_ else None,
        "start": start,
        "end": end,
        "room": room,
        "attendees": str(attendees) if attendees else None,
    }
    moved = found | {k: v for k, v in change.items() if v is not None}
    if moved == found:
        raise Refused("Nothing to change: give a new date, start, end, room or attendees.")
    _check(moved, ctx)
    _write(BOOKINGS, [moved if b["id"] == booking else b for b in rows])
    return _booking(moved, _rooms())


@office.action(roles=["admin"])
def add_room(
    ctx: ActionContext,
    room: Annotated[
        str, Field(pattern=r"^[a-z][a-z0-9-]{0,23}$", description="A short new id, such as oak")
    ],
    name: Annotated[Name, Field(description="The room's name, such as Oak")],
    capacity: Seats,
    opens: Time,
    closes: Time,
    days: Days = "mon,tue,wed,thu,fri",
) -> None:
    """Add a meeting room, with its seats and its opening days and hours."""
    if room in _rooms():
        raise Refused(f"There is already a room {room}")
    if closes <= opens:
        raise Refused(f"A room must open before it closes: {opens}–{closes}")
    _append(ROOMS, [room, name.strip(), capacity, opens, closes, _normalise(days)])


@office.action(roles=["admin"])
def update_room(
    ctx: ActionContext,
    room: RoomId,
    name: Annotated[Name | None, Field(description="A new name")] = None,
    capacity: Annotated[Seats | None, Field(description="New seats")] = None,
    opens: Annotated[Time | None, Field(description="New opening time")] = None,
    closes: Annotated[Time | None, Field(description="New closing time")] = None,
    days: Annotated[Days | None, Field(description="New opening days of the week")] = None,
) -> None:
    """Change a room's name, seats, hours or days; what is left out stays. Refused while a
    booking would no longer fit: move or cancel it first."""
    _known_room(room)
    change = {
        "name": name.strip() if name else None,
        "capacity": str(capacity) if capacity else None,
        "opens": opens,
        "closes": closes,
        "days": _normalise(days) if days else None,
    }
    change = {k: v for k, v in change.items() if v is not None}
    _write(ROOMS, [{**r, **change} if r["id"] == room else r for r in _read(ROOMS)])


def _span(first: date, until: date | None) -> list[date]:
    last = until or first
    if last < first:
        raise Refused(f"{_day(last.isoformat())} is before {_day(first.isoformat())}")
    if (last - first).days >= LONGEST_CLOSURE:
        raise Refused(f"Close at most {LONGEST_CLOSURE} days at a time")
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def _scope(room: str | None) -> str:
    if room:
        _known_room(room)
    return room or ""


@office.action(roles=["admin"])
def close(
    ctx: ActionContext,
    date_: Annotated[date, Field(alias="date", description="The first closing day")],
    reason: Annotated[Name, Field(description="Such as Christmas Day, or Repainting")],
    until: Annotated[
        date | None, Field(description="The last closing day, for several days in a row")
    ] = None,
    room: Scope = None,
) -> None:
    """Mark closing days: the whole office, or one room, for a day or from `date` to `until`.
    Refused while bookings stand then: move or cancel them first."""
    scope = _scope(room)
    closures = _closures()
    days = [d for d in _span(date_, until) if (d.isoformat(), scope) not in closures]
    if not days:
        raise Refused("Those days are already closing days")
    for d in days:
        _append(CLOSURES, [d.isoformat(), scope, reason.strip()])


@office.action(roles=["admin"])
def reopen(
    ctx: ActionContext,
    date_: Annotated[
        date,
        Field(alias="date", description="The first day to open again, as listed by `closing_days`"),
    ],
    until: Annotated[date | None, Field(description="The last day to open again")] = None,
    room: Scope = None,
) -> None:
    """Open the office, or one room, again on closing days."""
    scope = _scope(room)
    days = {d.isoformat() for d in _span(date_, until)}
    rows = _read(CLOSURES)
    kept = [c for c in rows if not (c["date"] in days and c["room"] == scope)]
    if len(kept) == len(rows):
        raise Refused("None of those days is a closing day")
    _write(CLOSURES, kept)


def _normalise(days: str) -> str:
    return ",".join(d for d in WEEKDAYS if d in days.split(","))


# Invariants: what always holds, however the data changes.


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


def _breaking(rule: Callable[[dict[str, str], dict[str, str]], str | None]) -> list[str]:
    """Each standing booking that breaks a rule about it and its room, with why."""
    rooms = _rooms()
    return [
        f"{_label(b, rooms)}: {problem}"
        for b in _active()
        if b["room"] in rooms and (problem := rule(b, rooms[b["room"]]))
    ]


@office.invariant("Every booking is within its room's opening days and hours")
def within_hours(ctx: InvariantContext) -> list[str]:
    return _breaking(_outside_hours)


@office.invariant("No booking falls on a closing day")
def not_on_closing_days(ctx: InvariantContext) -> list[str]:
    closures = _closures()
    return _breaking(lambda b, room: _closing_day(b, room, closures))


@office.invariant("No booking has more people than its room seats")
def fits_the_room(ctx: InvariantContext) -> list[str]:
    return _breaking(_too_many)


@office.invariant("Every booking has its own number, a known room, and ends after it starts")
def well_formed_bookings(ctx: InvariantContext) -> list[str]:
    rooms = _rooms()
    seen: set[str] = set()
    problems = []
    for b in _read(BOOKINGS):
        if b["id"] in seen:
            problems.append(f"Booking number {b['id']} is used twice")
        seen.add(b["id"])
        if b["room"] not in rooms:
            problems.append(f"{_label(b, rooms)}: there is no room {b['room']}")
        if b["end"] <= b["start"]:
            problems.append(f"{_label(b, rooms)} ends before it starts")
    return problems


@office.invariant("Every room has its own id, seats someone, and opens before it closes")
def well_formed_rooms(ctx: InvariantContext) -> list[str]:
    seen: set[str] = set()
    problems = []
    for r in _read(ROOMS):
        if r["id"] in seen:
            problems.append(f"Room id {r['id']} is used twice")
        seen.add(r["id"])
        if int(r["capacity"]) < 1:
            problems.append(f"{r['name']} seats no one")
        if r["closes"] <= r["opens"]:
            problems.append(f"{r['name']} closes at {r['closes']}, before it opens at {r['opens']}")
    return problems


@office.invariant("Each closing day is listed once, for the office or a known room")
def well_formed_closures(ctx: InvariantContext) -> list[str]:
    rooms = _rooms()
    seen: set[tuple[str, str]] = set()
    problems = []
    for c in _read(CLOSURES):
        where = rooms[c["room"]]["name"] if c["room"] in rooms else c["room"] or "the office"
        if (c["date"], c["room"]) in seen:
            problems.append(f"{_day(c['date'])} is listed twice as a closing day for {where}")
        seen.add((c["date"], c["room"]))
        if c["room"] and c["room"] not in rooms:
            problems.append(f"{_day(c['date'])} closes {c['room']}, which isn't a room")
    return problems


# Queries.


def _room(r: dict[str, str]) -> Room:
    return Room(
        id=r["id"],
        name=r["name"],
        capacity=int(r["capacity"]),
        opens=r["opens"],
        closes=r["closes"],
        days=r["days"].split(","),
    )


def _booking(b: dict[str, str], rooms: dict[str, dict[str, str]]) -> Booking:
    return Booking(
        id=b["id"],
        room=b["room"],
        room_name=rooms[b["room"]]["name"] if b["room"] in rooms else b["room"],
        date=date.fromisoformat(b["date"]),
        start=b["start"],
        end=b["end"],
        attendees=int(b["attendees"]),
        title=b["title"],
        booked_by=b["booked_by"],
    )


@office.query(roles=["member", "admin"])
def rooms(ctx: QueryContext) -> list[Room]:
    """The meeting rooms: each one's id, name, seats, and opening days and hours."""
    return [_room(r) for r in _read(ROOMS)]


@office.query(roles=["member", "admin"])
def closing_days(
    ctx: QueryContext,
    since: Annotated[date | None, Field(description="From this day, today by default")] = None,
    days: Annotated[int, Field(ge=1, le=366, description="How many days from `since`")] = 366,
) -> list[ClosingDay]:
    """The days the office or a room is closed, soonest first."""
    first = since or ctx.now.date()
    last = first + timedelta(days=days - 1)
    rooms = _rooms()
    found = sorted(
        (c for c in _read(CLOSURES) if first.isoformat() <= c["date"] <= last.isoformat()),
        key=lambda c: (c["date"], c["room"]),
    )
    return [
        ClosingDay(
            date=date.fromisoformat(c["date"]),
            room=c["room"] or None,
            room_name=rooms[c["room"]]["name"] if c["room"] in rooms else None,
            reason=c["reason"],
        )
        for c in found
    ]


@office.query(roles=["member", "admin"])
def bookings(
    ctx: QueryContext,
    since: Annotated[date | None, Field(description="The first day, today by default")] = None,
    days: Annotated[int, Field(ge=1, le=92, description="How many days from `since`")] = 7,
    room: Annotated[str | None, Field(description="Only this room, as listed by `rooms`")] = None,
    mine: Annotated[bool, Field(description="Only the caller's own bookings")] = False,
) -> list[Booking]:
    """The bookings that stand over a few days, in time order. Cancelled ones aren't listed."""
    if room is not None:
        _known_room(room)
    first = since or ctx.now.date()
    last = first + timedelta(days=days - 1)
    rooms = _rooms()
    found = [
        b
        for b in _active()
        if first.isoformat() <= b["date"] <= last.isoformat()
        and (room is None or b["room"] == room)
        and (not mine or b["booked_by"] == ctx.caller.id)
    ]
    found.sort(key=lambda b: (b["date"], b["start"], b["room"]))
    return [_booking(b, rooms) for b in found]


@office.query(roles=["member", "admin"])
def availability(
    ctx: QueryContext,
    date_: Day,
    start: Time,
    end: Time,
    attendees: Annotated[int, Field(gt=0, description="How many people")] = 1,
) -> list[Availability]:
    """Which rooms could take a meeting, the free ones first and smallest first, and why each
    other one can't. Refused for a time already past, as `book` is."""
    if problem := _timing(date_.isoformat(), start, end, ctx):
        raise Refused(problem)
    wanted = {"id": "", "date": date_.isoformat(), "start": start, "end": end}
    rooms, closures, active = _rooms(), _closures(), _active()
    answer = []
    for r in rooms.values():
        problems = _problems(
            wanted | {"attendees": str(attendees), "room": r["id"]}, rooms, closures, active
        )
        answer.append(
            Availability(
                room=r["id"],
                name=r["name"],
                capacity=int(r["capacity"]),
                free=not problems,
                why_not=problems,
            )
        )
    return sorted(answer, key=lambda a: (not a.free, a.capacity))


# Screens.


def _closed_text(d: date, room: dict[str, str], closures: dict[tuple[str, str], str]) -> str | None:
    """Why a room is closed on a day, for a screen, or None when it's open."""
    iso = d.isoformat()
    if (iso, "") in closures:
        return closures[iso, ""]
    if (iso, room["id"]) in closures:
        return f"Closed: {closures[iso, room['id']]}"
    if WEEKDAYS[d.weekday()] not in room["days"].split(","):
        return "Closed"
    return None


WEEK = """
<header>
  <h2>{{ heading }}</h2>
  <p class="text-base-content/60">{{ span }}</p>
</header>
<div class="join">
  <a class="btn btn-sm join-item" aria-label="Previous week" data-view
     data-args='{{ {"week": prev} | tojson }}'><span data-icon="chevron-left"></span></a>
  <a class="btn btn-sm join-item" data-view data-args='{{ {"week": this} | tojson }}'>This week</a>
  <a class="btn btn-sm join-item" aria-label="Next week" data-view
     data-args='{{ {"week": next} | tojson }}'><span data-icon="chevron-right"></span></a>
</div>
{% if not rows %}
<div class="alert alert-soft" role="status">
  <span data-icon="door-closed"></span><span>No meeting room yet.</span>
</div>
{% else %}
<div class="stats card-border">
  <div class="stat">
    <div class="stat-title">Bookings</div>
    <div class="stat-value">{{ count }}</div>
    <div class="stat-desc">Yours: {{ mine }}</div>
  </div>
  <div class="stat">
    <div class="stat-title">Hours booked</div>
    <div class="stat-value">{{ hours }}</div>
    <div class="stat-desc">Across {{ rows | length }} rooms</div>
  </div>
</div>
<section class="card card-border layout-only-narrow">
  <div class="card-body">
    {% for d in days %}
    <div class="layout-row layout-between">
      <h3 class="card-title">
        <a class="link link-hover {{ 'text-primary' if d.today }}" data-view="day"
           data-args='{{ {"day": d.iso} | tojson }}'>{{ d.long }}</a>
      </h3>
      {% if d.today %}<span class="badge badge-sm badge-primary">Today</span>{% endif %}
    </div>
    {% if d.closed %}
    <p class="text-warning"><small>The office is closed: {{ d.closed }}</small></p>
    {% elif d.bookings %}
    <ul class="list">
      {% for b in d.bookings %}
      <li class="list-row">
        <span class="badge badge-sm {{ b.tone }} layout-nowrap">{{ b.start }}–{{ b.end }}</span>
        <div class="list-col-grow">
          <div>{{ b.title }}</div>
          <small class="text-base-content/60">{{ b.room_name }}</small>
        </div>
      </li>
      {% endfor %}
    </ul>
    {% else %}
    <p class="text-base-content/60"><small>Nothing booked.</small></p>
    {% endif %}
    {% if not loop.last %}<div class="divider"></div>{% endif %}
    {% endfor %}
  </div>
</section>
<div class="layout-only-wide layout-scroll-x">
      <table class="table table-sm table-pin-cols">
        <thead>
          <tr>
            <th>Room</th>
            {% for d in days %}
            <th class="layout-nowrap">
              <a class="link link-hover {{ d.tone }}" data-view="day"
                 data-args='{{ {"day": d.iso} | tojson }}'>{{ d.label }}</a>
              {% if d.closed %}
              <div><small class="text-warning">{{ d.closed }}</small></div>
              {% endif %}
            </th>
            {% endfor %}
          </tr>
        </thead>
        <tbody>
          {% for row in rows %}
          <tr>
            <th>
              <div class="layout-nowrap">{{ row.name }}</div>
              <small class="text-base-content/60 layout-nowrap">{{ row.capacity }} seats</small>
            </th>
            {% for cell in row.cells %}
            <td>
              {% if cell.closed %}
              <small class="text-base-content/60">{{ cell.closed }}</small>
              {% else %}
              <div class="layout-stack layout-gap-2">
                {% for b in cell.bookings %}
                <div class="layout-stack layout-gap-0">
                  <span class="badge badge-sm {{ b.tone }} layout-nowrap">
                    {{ b.start }}–{{ b.end }}</span>
                  <small>{{ b.title }}</small>
                </div>
                {% endfor %}
              </div>
              {% endif %}
            </td>
            {% endfor %}
          </tr>
          {% endfor %}
        </tbody>
      </table>
</div>
{% endif %}
"""

# The caller's own bookings in the accent; the others plain, readable in both themes.
MINE, OTHERS = "badge-soft badge-primary", "badge-ghost"


@office.view(roles=["member", "admin"], title="Week")
def week(
    ctx: QueryContext,
    week: Annotated[
        date | None, Field(description="Any day of the week to show, this week by default")
    ] = None,
) -> Screen:
    """The week at a glance: every room's bookings day by day, the caller's own in colour."""
    today = ctx.now.date()
    monday = (week or today) - timedelta(days=(week or today).weekday())
    sunday = monday + timedelta(days=6)
    room_rows = _read(ROOMS)
    names = {r["id"]: r["name"] for r in room_rows}
    closures = _closures()
    active = sorted(
        (b for b in _active() if monday.isoformat() <= b["date"] <= sunday.isoformat()),
        key=lambda b: (b["date"], b["start"], b["room"]),
    )
    shown_bookings = [
        b
        | {
            "tone": MINE if b["booked_by"] == ctx.caller.id else OTHERS,
            "room_name": names.get(b["room"], b["room"]),
        }
        for b in active
    ]
    # Saturday and Sunday show only when a room opens then, or something is booked.
    shown = [
        monday + timedelta(days=n)
        for n in range(7)
        if n < 5
        or any(WEEKDAYS[n] in r["days"].split(",") for r in room_rows)
        or any(b["date"] == (monday + timedelta(days=n)).isoformat() for b in active)
    ]
    days = [
        {
            "iso": d.isoformat(),
            "label": f"{d:%a} {d.day}",
            "long": f"{d:%A} {d.day}",
            "today": d == today,
            "tone": "text-primary" if d == today else "text-base-content/60" if d < today else "",
            "closed": closures.get((d.isoformat(), "")),
            "bookings": [b for b in shown_bookings if b["date"] == d.isoformat()],
        }
        for d in shown
    ]
    rows = []
    for r in room_rows:
        cells = []
        for d in shown:
            booked = [
                b for b in shown_bookings if b["room"] == r["id"] and b["date"] == d.isoformat()
            ]
            # The header already gives an office closing day's reason.
            closed = "Closed" if (d.isoformat(), "") in closures else _closed_text(d, r, closures)
            cells.append({"closed": None if booked else closed, "bookings": booked})
        rows.append({"name": r["name"], "capacity": r["capacity"], "cells": cells})
    minutes = sum(_clock(b["end"]) - _clock(b["start"]) for b in active)
    return Screen(
        WEEK,
        args={"week": monday.isoformat()},
        heading="This week" if monday <= today <= sunday else f"Week of {monday.day} {monday:%B}",
        span=f"{monday.day} {monday:%B} – {sunday.day} {sunday:%B %Y}",
        prev=(monday - timedelta(days=7)).isoformat(),
        next=(monday + timedelta(days=7)).isoformat(),
        this=today.isoformat(),
        days=days,
        rows=rows,
        count=len(active),
        mine=sum(1 for b in active if b["booked_by"] == ctx.caller.id),
        hours=f"{minutes / 60:.1f}".removesuffix(".0"),
    )


DAY = """
<header>
  <h2>{{ heading }}</h2>
  <p class="text-base-content/60">{{ year }}</p>
</header>
<div class="layout-row layout-between">
  <div class="join">
    <a class="btn btn-sm join-item" aria-label="Previous day" data-view
       data-args='{{ {"day": prev} | tojson }}'><span data-icon="chevron-left"></span></a>
    <a class="btn btn-sm join-item" data-view data-args='{{ {"day": today} | tojson }}'>Today</a>
    <a class="btn btn-sm join-item" aria-label="Next day" data-view
       data-args='{{ {"day": next} | tojson }}'><span data-icon="chevron-right"></span></a>
  </div>
  <form class="join" data-view>
    <input class="input input-sm join-item" type="date" name="day" value="{{ day }}"
           aria-label="Day" required>
    <button class="btn btn-sm join-item">Go</button>
  </form>
</div>
{% if not schedule %}
<div class="alert alert-soft" role="status">
  <span data-icon="door-closed"></span><span>No meeting room yet.</span>
</div>
{% endif %}
{% if office_closed %}
<div class="alert alert-soft alert-warning" role="status">
  <span data-icon="calendar-off"></span><span>The office is closed: {{ office_closed }}</span>
</div>
{% elif past and schedule %}
<p class="text-base-content/60">This day is past: nothing more can be booked.</p>
{% endif %}
{% for room in schedule %}
<section class="card card-border">
  <div class="card-body">
    <div class="layout-row layout-between">
      <div class="layout-row layout-gap-3">
        <h3 class="card-title">{{ room.name }}</h3>
        <span class="badge badge-soft">{{ room.capacity }} seats</span>
      </div>
      {% if room.bookable %}
      <a class="btn btn-sm btn-primary" data-view="find_room"
         data-args='{{ {"day": day, "room": room.id} | tojson }}'>
        <span data-icon="calendar-plus"></span> Book</a>
      {% endif %}
    </div>
    {% if room.bookings %}
    <ul class="list">
      {% for b in room.bookings %}
      <li class="list-row">
        <strong class="layout-nowrap">{{ b.start }}–{{ b.end }}</strong>
        <div class="list-col-grow">
          <div>{{ b.title }}</div>
          <small class="text-base-content/60">{{ b.booked_by }} · {{ people(b.attendees) }}</small>
        </div>
        {% if b.id in cancellable %}
        <form data-call="cancel" data-args='{{ {"booking": b.id} | tojson }}'>
          <button class="btn btn-sm btn-ghost">Cancel</button>
        </form>
        {% endif %}
      </li>
      {% endfor %}
    </ul>
    {% else %}
    <p class="text-base-content/60">{{ room.empty }}</p>
    {% endif %}
  </div>
</section>
{% endfor %}
"""


def _open_after(d: date, room: dict[str, str], ctx: QueryContext) -> bool:
    """Whether some of the room's opening hours that day are still ahead."""
    return _at(d.isoformat(), room["closes"], ctx) > ctx.now


@office.view(roles=["member", "admin"])
def day(
    ctx: QueryContext,
    day: Annotated[date | None, Field(description="The day to show, today by default")] = None,
) -> Screen:
    """A day's bookings room by room, with a Book button on each room still open, and Cancel
    on the bookings the caller may cancel."""
    today = ctx.now.date()
    shown = day or today
    iso = shown.isoformat()
    closures = _closures()
    standing = sorted((b for b in _active() if b["date"] == iso), key=lambda b: b["start"])
    schedule = []
    for r in _read(ROOMS):
        closed = _closed_text(shown, r, closures)
        if (iso, "") in closures:
            empty = "Closed: the office is closed."
        elif closed == "Closed":
            empty = f"Closed on {shown:%A}s."
        elif closed:
            empty = f"{closed}."
        else:
            empty = f"Free all day, {r['opens']}–{r['closes']}."
        schedule.append(
            {
                "id": r["id"],
                "name": r["name"],
                "capacity": int(r["capacity"]),
                "bookable": closed is None and _open_after(shown, r, ctx),
                "empty": empty,
                "bookings": [b for b in standing if b["room"] == r["id"]],
            }
        )
    cancellable = [
        b["id"]
        for b in standing
        if _at(b["date"], b["start"], ctx) > ctx.now
        and (ctx.caller.role == "admin" or b["booked_by"] == ctx.caller.id)
    ]
    return Screen(
        DAY,
        args={"day": iso},
        day=iso,
        heading=f"{shown:%A} {shown.day} {shown:%B}",
        year="Today" if shown == today else str(shown.year),
        prev=(shown - timedelta(days=1)).isoformat(),
        next=(shown + timedelta(days=1)).isoformat(),
        today=today.isoformat(),
        office_closed=closures.get((iso, "")),
        schedule=schedule,
        cancellable=cancellable,
        past=shown < today,
        people=_people,
    )


FIND = """
<header>
  <h2>Book a room</h2>
  <p class="text-base-content/60">{{ heading }}</p>
</header>
{% if made %}
<div class="alert alert-soft alert-success" role="status">
  <span data-icon="calendar-check"></span>
  <span>Booked {{ made.id }}: {{ made.room_name }}, {{ made.start }}–{{ made.end }}.</span>
</div>
<div class="layout-row">
  <a class="btn btn-primary" data-view="day" data-args='{{ {"day": day} | tojson }}'>See the day</a>
  <a class="btn btn-outline" data-view="find_room" data-args='{{ {"day": day} | tojson }}'>
    Book another</a>
</div>
{% else %}
<section class="card card-border">
  <div class="card-body">
    <form class="layout-stack layout-gap-3" data-view="find_room" data-args='{{ keep | tojson }}'>
      <div class="layout-cols-2 layout-gap-3">
        <fieldset class="fieldset">
          <legend class="fieldset-legend">Day</legend>
          <input class="input" type="date" name="day" value="{{ day }}" required>
        </fieldset>
        <fieldset class="fieldset">
          <legend class="fieldset-legend">People</legend>
          <input class="input" type="number" name="attendees" min="1" value="{{ attendees }}"
                 required>
        </fieldset>
        <fieldset class="fieldset">
          <legend class="fieldset-legend">From</legend>
          <input class="input" type="time" name="start" value="{{ start }}" step="300" required>
        </fieldset>
        <fieldset class="fieldset">
          <legend class="fieldset-legend">To</legend>
          <input class="input" type="time" name="end" value="{{ end }}" step="300" required>
        </fieldset>
      </div>
      <fieldset class="fieldset">
        <legend class="fieldset-legend">What for</legend>
        <input class="input" name="title" value="{{ title }}" placeholder="Such as Weekly planning"
               maxlength="120" required>
      </fieldset>
      <button class="btn btn-primary"><span data-icon="search"></span> Find a room</button>
    </form>
  </div>
</section>
{% if problem %}
<div class="alert alert-soft alert-warning" role="status">
  <span data-icon="calendar-off"></span><span>{{ problem }}.</span>
</div>
{% elif free is not none %}
<section class="card card-border">
  <div class="card-body">
    <h3 class="card-title">Free {{ start }}–{{ end }}</h3>
    {% if free %}
    <ul class="list">
      {% for r in free %}
      <li class="list-row">
        <div class="list-col-grow">
          <div>{{ r.name }}</div>
          <small class="text-base-content/60">{{ r.capacity }} seats</small>
        </div>
        <form data-call="book" data-args='{{ r.call | tojson }}'
              data-then='{{ {"booked": r.room} | tojson }}'>
          <button class="btn btn-sm btn-primary">
            <span data-icon="calendar-plus"></span> Book</button>
        </form>
      </li>
      {% endfor %}
    </ul>
    {% else %}
    <p class="text-base-content/60">No room is free then.</p>
    {% endif %}
  </div>
</section>
{% if not_free %}
<section class="card card-border">
  <div class="card-body">
    <h3 class="card-title">Not free</h3>
    <ul class="list">
      {% for r in not_free %}
      <li class="list-row">
        <div class="list-col-grow">
          <div>{{ r.name }}</div>
          {% for why in r.why_not %}
          <div><small class="text-base-content/60">{{ why }}</small></div>
          {% endfor %}
        </div>
      </li>
      {% endfor %}
    </ul>
  </div>
</section>
{% endif %}
{% elif chosen %}
<section class="card card-border">
  <div class="card-body">
    <h3 class="card-title">{{ chosen.name }}</h3>
    <p class="text-base-content/60">
      <small>Open {{ chosen.opens }}–{{ chosen.closes }}, {{ chosen.capacity }} seats</small>
    </p>
    {% if day_bookings %}
    <ul class="list">
      {% for b in day_bookings %}
      <li class="list-row">
        <span class="badge badge-sm badge-ghost layout-nowrap">{{ b.start }}–{{ b.end }}</span>
        <div class="list-col-grow">{{ b.title }}</div>
      </li>
      {% endfor %}
    </ul>
    {% else %}
    <p class="text-base-content/60">Nothing booked yet that day.</p>
    {% endif %}
  </div>
</section>
{% endif %}
{% endif %}
"""


@office.view(roles=["member", "admin"], title="Book")
def find_room(
    ctx: QueryContext,
    day: Annotated[date | None, Field(description="The day, today by default")] = None,
    start: Annotated[Time | None, Field(description="From")] = None,
    end: Annotated[Time | None, Field(description="To")] = None,
    attendees: Annotated[int, Field(gt=0, description="How many people")] = 2,
    title: Annotated[str, Field(max_length=120, description="What the meeting is for")] = "",
    room: Annotated[
        str | None, Field(description="A room to show first, as listed by `rooms`")
    ] = None,
    booked: Annotated[str | None, Field(description="The room just booked, to say so")] = None,
) -> Screen:
    """Find a free room and book it: the day, the times, how many people and what for, then
    each room that can take it with a Book button, and why each other one can't. With only
    `room`, that room's bookings that day."""
    if room is not None:
        _known_room(room)
    shown = day or ctx.now.date()
    iso = shown.isoformat()
    closures = _closures()
    rooms = _rooms()
    chosen = _room(rooms[room]) if room else None
    made = next(
        (
            _booking(b, rooms)
            for b in _active()
            if (b["room"], b["date"], b["start"]) == (booked, iso, start)
        ),
        None,
    )
    problem, free, not_free, day_bookings = None, None, [], []
    if (iso, "") in closures:
        problem = f"The office is closed: {closures[iso, '']}"
    elif start and end and not (problem := _timing(iso, start, end, ctx)):
        answers = availability(ctx, date_=shown, start=start, end=end, attendees=attendees)
        answers.sort(key=lambda a: a.room != room)  # the room asked for first
        call = {"date": iso, "start": start, "end": end, "attendees": attendees, "title": title}
        free = [a.model_dump() | {"call": call | {"room": a.room}} for a in answers if a.free]
        not_free = [a for a in answers if not a.free]
    elif chosen and not problem:
        day_bookings = sorted(
            (b for b in _active() if b["room"] == chosen.id and b["date"] == iso),
            key=lambda b: b["start"],
        )
    state = {"day": iso, "start": start, "end": end, "attendees": attendees, "title": title}
    args = {k: v for k, v in (state | {"room": room, "booked": booked}).items() if v}
    return Screen(
        FIND,
        args=args,
        day=iso,
        start=start or "",
        end=end or "",
        attendees=attendees,
        title=title,
        heading=f"{shown:%A} {shown.day} {shown:%B %Y}",
        keep={"room": room} if room else {},
        chosen=chosen,
        made=made,
        problem=problem,
        free=free,
        not_free=not_free,
        day_bookings=day_bookings,
    )


ADMIN = """
{% if editing %}
<header>
  <h2>{{ heading }}</h2>
  {% if room %}<p class="text-base-content/60">Id {{ room.id }}</p>{% endif %}
</header>
{% if saved %}
<div class="alert alert-soft alert-success" role="status">
  <span data-icon="check"></span><span>{{ saved }}</span>
</div>
{% endif %}
<section class="card card-border">
  <div class="card-body">
    {% if room %}
    <form class="layout-stack layout-gap-3" data-call="update_room"
          data-args='{{ {"room": room.id} | tojson }}' data-then='{"saved": true}'>
      {{ room_fields(room, day_options) }}
      <div class="layout-row layout-between">
        <a class="btn btn-ghost" data-view="administration">Back</a>
        <button class="btn btn-primary">Save changes</button>
      </div>
    </form>
    {% else %}
    <form class="layout-stack layout-gap-3" data-call="add_room" data-then='{"saved": true}'>
      <fieldset class="fieldset">
        <legend class="fieldset-legend">Id</legend>
        <input class="input" name="room" placeholder="Such as oak" required>
      </fieldset>
      {{ room_fields(room, day_options) }}
      <div class="layout-row layout-between">
        <a class="btn btn-ghost" data-view="administration">Back</a>
        <button class="btn btn-primary"><span data-icon="plus"></span> Add room</button>
      </div>
    </form>
    {% endif %}
  </div>
</section>
{% else %}
<header>
  <h2>Admin</h2>
</header>
<div role="tablist" class="tabs tabs-border">
  <input type="radio" name="section" class="tab" aria-label="Rooms" checked>
  <div class="tab-content">
    <div class="layout-stack">
      {% if rooms %}
      <ul class="list">
        {% for r in rooms %}
        <li class="list-row">
          <div class="list-col-grow">
            <div>
              {{ r.name }} <span class="badge badge-sm badge-soft">{{ r.capacity }} seats</span>
            </div>
            <small class="text-base-content/60">
              {{ days_label(r.days) }}, {{ r.opens }}–{{ r.closes }}</small>
          </div>
          <a class="btn btn-sm btn-ghost" data-view
             data-args='{{ {"room": r.id} | tojson }}'><span data-icon="pencil"></span> Edit</a>
        </li>
        {% endfor %}
      </ul>
      {% else %}
      <p class="text-base-content/60">No meeting room yet.</p>
      {% endif %}
      <div class="layout-row">
        <a class="btn btn-primary" data-view data-args='{"add": true}'>
          <span data-icon="plus"></span> Add a room</a>
      </div>
    </div>
  </div>
  <input type="radio" name="section" class="tab" aria-label="Closing days">
  <div class="tab-content">
    <div class="layout-stack">
      {% if closures %}
      <ul class="list">
        {% for c in closures %}
        <li class="list-row">
          <div class="list-col-grow">
            <div>{{ c.reason }}</div>
            <small class="text-base-content/60">{{ c.when }} · {{ c.where }}</small>
          </div>
          <form data-call="reopen" data-args='{{ c.args | tojson }}'>
            <button class="btn btn-sm btn-ghost">Reopen</button>
          </form>
        </li>
        {% endfor %}
      </ul>
      {% else %}
      <p class="text-base-content/60">No closing day ahead.</p>
      {% endif %}
      <form class="layout-stack layout-gap-3" data-call="close">
        <div class="layout-cols-2 layout-gap-3">
          <fieldset class="fieldset">
            <legend class="fieldset-legend">From</legend>
            <input class="input" type="date" name="date" required>
          </fieldset>
          <fieldset class="fieldset">
            <legend class="fieldset-legend">To (optional)</legend>
            <input class="input" type="date" name="until">
          </fieldset>
        </div>
        <div class="layout-cols-2 layout-gap-3">
          <fieldset class="fieldset">
            <legend class="fieldset-legend">What closes</legend>
            <select class="select" name="room">
              <option value="" selected>The whole office</option>
              {% for r in rooms %}<option value="{{ r.id }}">{{ r.name }} only</option>{% endfor %}
            </select>
          </fieldset>
          <fieldset class="fieldset">
            <legend class="fieldset-legend">Reason</legend>
            <input class="input" name="reason" placeholder="Such as Christmas Day" required>
          </fieldset>
        </div>
        <button class="btn btn-primary">
          <span data-icon="calendar-off"></span> Add closing days</button>
      </form>
    </div>
  </div>
</div>
{% endif %}
"""

ROOM_FIELDS = """
{% macro room_fields(room, day_options) %}
<div class="layout-cols-3 layout-gap-3">
  <fieldset class="fieldset layout-span-2">
    <legend class="fieldset-legend">Name</legend>
    <input class="input" name="name" value="{{ room.name if room else '' }}"
           placeholder="Such as Oak" required>
  </fieldset>
  <fieldset class="fieldset">
    <legend class="fieldset-legend">Seats</legend>
    <input class="input" type="number" name="capacity" min="1"
           value="{{ room.capacity if room else '' }}" required>
  </fieldset>
</div>
<div class="layout-cols-2 layout-gap-3">
  <fieldset class="fieldset">
    <legend class="fieldset-legend">Opens</legend>
    <input class="input" type="time" name="opens" value="{{ room.opens if room else '08:00' }}"
           required>
  </fieldset>
  <fieldset class="fieldset">
    <legend class="fieldset-legend">Closes</legend>
    <input class="input" type="time" name="closes" value="{{ room.closes if room else '19:00' }}"
           required>
  </fieldset>
</div>
<fieldset class="fieldset">
  <legend class="fieldset-legend">Open on</legend>
  <select class="select" name="days" required>
    {% for value, label, chosen in day_options %}
    {% if chosen %}<option value="{{ value }}" selected>{{ label }}</option>
    {% else %}<option value="{{ value }}">{{ label }}</option>{% endif %}
    {% endfor %}
  </select>
</fieldset>
{% endmacro %}
"""

PRESETS = [
    ("mon,tue,wed,thu,fri", "Monday to Friday"),
    ("mon,tue,wed,thu", "Monday to Thursday"),
    ("mon,tue,wed,thu,fri,sat", "Monday to Saturday"),
    (",".join(WEEKDAYS), "Every day"),
]


def _closure_runs(days: list[ClosingDay]) -> list[dict[str, object]]:
    """Closing days in a row, for the same place and reason, as one line with one Reopen."""
    runs: list[list[ClosingDay]] = []
    for c in sorted(days, key=lambda c: (c.room or "", c.reason, c.date)):
        last = runs[-1][-1] if runs else None
        same = last and (last.room, last.reason) == (c.room, c.reason)
        if same and c.date - last.date == timedelta(days=1):
            runs[-1].append(c)
        else:
            runs.append([c])
    runs.sort(key=lambda run: run[0].date)
    lines: list[dict[str, object]] = []
    for run in runs:
        first, a, b = run[0], run[0].date, run[-1].date
        args: dict[str, object] = {"date": a.isoformat()}
        if b != a:
            args["until"] = b.isoformat()
        if first.room:
            args["room"] = first.room
        if a == b:
            when = _day(a.isoformat())
        elif (a.year, a.month) == (b.year, b.month):
            when = f"{a:%a} {a.day} – {_day(b.isoformat())}"
        else:
            when = f"{_day(a.isoformat())} – {_day(b.isoformat())}"
        where = first.room_name or "Whole office"
        lines.append({"reason": first.reason, "when": when, "where": where, "args": args})
    return lines


@office.view(roles=["admin"], title="Admin")
def administration(
    ctx: QueryContext,
    room: Annotated[str | None, Field(description="A room to edit, as listed by `rooms`")] = None,
    add: Annotated[bool, Field(description="Show the form to add a room")] = False,
    saved: Annotated[bool, Field(description="Show that the room was saved")] = False,
) -> Screen:
    """The rooms, to add or edit one, and the closing days ahead, to add or reopen them. With
    `room`, or `add`, the form to edit that room or to add one."""
    if room:
        _known_room(room)
    listed = rooms(ctx)
    found = next((r for r in listed if r.id == room), None)
    current = ",".join(found.days) if found else PRESETS[0][0]
    options = list(PRESETS)
    if current not in dict(options):
        options.append((current, _days_label(current)))
    state = {"room": room, "add": add and not room, "saved": saved}
    return Screen(
        ROOM_FIELDS + ADMIN,
        args={k: v for k, v in state.items() if v},
        editing=bool(found) or add,
        room=found,
        heading=f"Edit {found.name}" if found else "Add a room",
        saved=(("Changes saved." if found else "Room added.") if saved else None),
        day_options=[(value, label, value == current) for value, label in options],
        rooms=listed,
        closures=_closure_runs(closing_days(ctx)),
        days_label=lambda days: _days_label(",".join(days)),
    )
