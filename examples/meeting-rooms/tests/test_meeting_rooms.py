from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app import (
    Booking,
    ClosingDay,
    add_room,
    administration,
    availability,
    book,
    bookings,
    cancel,
    close,
    closing_days,
    day,
    find_room,
    fits_the_room,
    move,
    no_overlap,
    not_on_closing_days,
    office,
    reopen,
    rooms,
    update_room,
    week,
    well_formed_bookings,
    well_formed_closures,
    well_formed_rooms,
    within_hours,
)

from cliquet.testing import Connection, Denied, InvalidArguments, Refused, World, given

# Thursday 26 November 2026, 08:00 in Paris.
NOW = datetime(2026, 11, 26, 8, 0, tzinfo=ZoneInfo("Europe/Paris"))
FRIDAY = date(2026, 11, 27)

ROOMS = """\
id,name,capacity,opens,closes,days
oak,Oak,8,08:00,19:00,"mon,tue,wed,thu,fri"
pine,Pine,4,08:00,19:00,"mon,tue,wed,thu,fri"
"""

CLOSURES = """\
date,room,reason
2026-12-25,,Christmas Day
2026-12-01,pine,Repainting
"""

BOOKINGS = """\
id,room,date,start,end,attendees,title,booked_by,status
B-0001,oak,2026-11-27,14:00,15:30,8,Client call,camille,booked
B-0002,pine,2026-11-27,10:00,11:00,3,Design review,sam,booked
B-0003,oak,2026-11-27,09:00,10:00,4,Standup,sam,cancelled
"""


def world() -> World:
    return given(
        office,
        {"rooms.csv": ROOMS, "closing_days.csv": CLOSURES, "bookings.csv": BOOKINGS},
        now=NOW,
    )


def member(w: World | None = None, who: str = "sam") -> Connection:
    return (w or world()).connect("member", id=who)


def admin(w: World | None = None) -> Connection:
    return (w or world()).connect("admin", id="boss")


def booking(
    conn: Connection,
    room: str = "oak",
    start: str = "10:00",
    end: str = "11:00",
    day: date = FRIDAY,
    attendees: int = 4,
):
    return conn.call(
        book, room=room, date_=day, start=start, end=end, attendees=attendees, title="Sync"
    )


# Booking.


def test_a_booking_is_numbered_and_listed() -> None:
    m = member()
    made = booking(m).expect_ok()
    assert made == Booking(
        id="B-0004",
        room="oak",
        room_name="Oak",
        date=FRIDAY,
        start="10:00",
        end="11:00",
        attendees=4,
        title="Sync",
        booked_by="sam",
    )
    assert made in m.query(bookings, mine=True)


def test_back_to_back_bookings_are_fine() -> None:
    m = member()
    booking(m, start="13:00", end="14:00").expect_ok()
    booking(m, start="15:30", end="16:00").expect_ok()


def test_a_clash_is_refused() -> None:
    booking(member(), start="15:00", end="16:00").expect_refused(
        message="Oak is already booked 14:00–15:30 (Client call, B-0001)"
    )


def test_a_cancelled_booking_frees_its_slot() -> None:
    booking(member(), start="09:00", end="10:00").expect_ok()


def test_outside_hours_is_refused() -> None:
    booking(member(), start="18:00", end="19:30").expect_refused(
        message="Oak is open 08:00–19:00 only"
    )


def test_a_closed_weekday_is_refused() -> None:
    booking(member(), day=date(2026, 11, 28)).expect_refused(
        message="Oak is closed on Saturdays (open Mon–Fri)"
    )


def test_an_office_closing_day_is_refused() -> None:
    booking(member(), day=date(2026, 12, 25)).expect_refused(
        message="Fri 25 December 2026 is a closing day for the office (Christmas Day)"
    )


def test_a_room_closing_day_closes_that_room_only() -> None:
    m = member()
    booking(m, room="pine", day=date(2026, 12, 1)).expect_refused(
        message="Pine is closed on Tue 1 December 2026 (Repainting)"
    )
    booking(m, room="oak", day=date(2026, 12, 1)).expect_ok()


def test_too_many_people_is_refused() -> None:
    booking(member(), room="pine", start="12:00", end="13:00", attendees=5).expect_refused(
        message="5 people won't fit: Pine seats 4"
    )


def test_every_problem_is_said_at_once() -> None:
    booking(member(), room="pine", start="10:30", end="11:30", attendees=6).expect_refused(
        message="6 people won't fit: Pine seats 4; "
        "Pine is already booked 10:00–11:00 (Design review, B-0002)"
    )


def test_the_past_unknown_rooms_and_backwards_times_are_refused() -> None:
    m = member()
    booking(m, day=date(2026, 11, 26), start="07:00", end="07:30").expect_refused(
        message="Thu 26 November 2026 at 07:00 is already past"
    )
    booking(m, room="cedar").expect_refused(
        message="There is no room cedar. See `rooms` for their ids."
    )
    booking(m, start="11:00", end="10:00").expect_refused(
        message="The meeting must end after it starts: 11:00–10:00"
    )


def test_a_bad_time_or_no_one_is_an_invalid_argument() -> None:
    with pytest.raises(InvalidArguments):
        booking(member(), start="9:00")
    with pytest.raises(InvalidArguments):
        booking(member(), attendees=0)


# Cancelling.


def test_a_member_cancels_their_own_booking_and_rebooks_it() -> None:
    w = world()
    m = member(w)
    m.call(cancel, booking="B-0002").expect_ok()
    assert all(b.id != "B-0002" for b in m.query(bookings))
    m.call(cancel, booking="B-0002").expect_refused(
        message="There is no booking B-0002 to cancel. See `bookings`."
    )
    booking(m, room="pine", start="10:00", end="11:00", attendees=3).expect_ok()


def test_a_member_cannot_cancel_someone_elses_booking_but_an_admin_can() -> None:
    w = world()
    member(w).call(cancel, booking="B-0001").expect_refused(
        message="B-0001 was booked by camille: only they or an admin may cancel it"
    )
    admin(w).call(cancel, booking="B-0001").expect_ok()


def test_a_started_booking_cannot_be_cancelled() -> None:
    w = given(
        office,
        {"rooms.csv": ROOMS, "bookings.csv": BOOKINGS},
        now=datetime(2026, 11, 27, 10, 15, tzinfo=ZoneInfo("Europe/Paris")),
    )
    member(w).call(cancel, booking="B-0002").expect_refused(
        message="Booking B-0002 (Pine, Fri 27 November 2026, 10:00–11:00) has already started"
    )


# Moving.


def test_a_booking_moves_keeping_its_number_and_length() -> None:
    w = world()
    m = member(w)
    moved = m.call(move, booking="B-0002", start="16:00").expect_ok()
    assert (moved.id, moved.start, moved.end, moved.room) == ("B-0002", "16:00", "17:00", "pine")
    moved = m.call(move, booking="B-0002", room="oak", date_=date(2026, 11, 30)).expect_ok()
    assert (moved.room_name, moved.date) == ("Oak", date(2026, 11, 30))
    assert [b.id for b in m.query(bookings, since=date(2026, 11, 30), days=1)] == ["B-0002"]


def test_a_move_is_refused_as_a_booking_is() -> None:
    w = world()
    m = member(w)
    m.call(move, booking="B-0002", room="oak", start="14:30").expect_refused(
        message="Oak is already booked 14:00–15:30 (Client call, B-0001)"
    )
    m.call(move, booking="B-0002", attendees=5).expect_refused(
        message="5 people won't fit: Pine seats 4"
    )
    m.call(move, booking="B-0002", start="18:30").expect_refused(
        message="Pine is open 08:00–19:00 only"
    )
    m.call(move, booking="B-0001", start="16:00").expect_refused(
        message="B-0001 was booked by camille: only they or an admin may move it"
    )
    m.call(move, booking="B-0002", room="cedar").expect_refused(
        message="There is no room cedar. See `rooms` for their ids."
    )
    m.call(move, booking="B-0002", start="10:00").expect_refused(
        message="Nothing to change: give a new date, start, end, room or attendees."
    )
    admin(w).call(move, booking="B-0001", start="16:00").expect_ok()


# Rooms and closing days: a change that would break a standing booking is refused.


def test_an_admin_adds_and_changes_rooms() -> None:
    a = admin()
    a.call(
        add_room, room="cedar", name="Cedar", capacity=6, opens="09:00", closes="17:00"
    ).expect_ok()
    a.call(update_room, room="cedar", capacity=10, days="sat,mon").expect_ok()
    cedar = next(r for r in a.query(rooms) if r.id == "cedar")
    assert (cedar.capacity, cedar.days, cedar.opens) == (10, ["mon", "sat"], "09:00")
    a.call(
        add_room, room="cedar", name="Cedar", capacity=6, opens="09:00", closes="17:00"
    ).expect_refused(message="There is already a room cedar")
    a.call(
        add_room, room="elm", name="Elm", capacity=6, opens="17:00", closes="09:00"
    ).expect_refused(message="A room must open before it closes: 17:00–09:00")


def test_shrinking_a_room_under_a_booking_is_refused() -> None:
    admin().call(update_room, room="oak", capacity=6).expect_refused(by=fits_the_room)


def test_shortening_hours_under_a_booking_is_refused() -> None:
    messages = admin().call(update_room, room="oak", closes="15:00").expect_refused(by=within_hours)
    assert messages == [
        "Booking B-0001 (Oak, Fri 27 November 2026, 14:00–15:30): Oak is open 08:00–15:00 only"
    ]


def test_closing_a_booked_day_is_refused_until_cancelled() -> None:
    w = world()
    a = admin(w)
    messages = a.call(close, date_=FRIDAY, reason="Bridge day", room="oak").expect_refused(
        by=not_on_closing_days
    )
    assert messages == [
        "Booking B-0001 (Oak, Fri 27 November 2026, 14:00–15:30): "
        "Oak is closed on Fri 27 November 2026 (Bridge day)"
    ]
    a.call(cancel, booking="B-0001").expect_ok()
    a.call(close, date_=FRIDAY, reason="Bridge day", room="oak").expect_ok()
    a.call(close, date_=FRIDAY, reason="Again", room="oak").expect_refused(
        message="Those days are already closing days"
    )
    a.call(reopen, date_=FRIDAY, room="oak").expect_ok()
    a.call(reopen, date_=FRIDAY, room="oak").expect_refused(
        message="None of those days is a closing day"
    )


def test_closing_several_days_in_a_row_and_reopening_them() -> None:
    a = admin()
    a.call(
        close, date_=date(2026, 12, 21), until=date(2026, 12, 25), reason="Winter break"
    ).expect_ok()
    closed = a.query(closing_days, since=date(2026, 12, 21), days=5)
    # Christmas Day was already closed: it keeps its own reason.
    assert [(c.date.day, c.reason) for c in closed] == [
        (21, "Winter break"),
        (22, "Winter break"),
        (23, "Winter break"),
        (24, "Winter break"),
        (25, "Christmas Day"),
    ]
    assert closed[0] == ClosingDay(
        date=date(2026, 12, 21), room=None, room_name=None, reason="Winter break"
    )
    a.call(reopen, date_=date(2026, 12, 21), until=date(2026, 12, 25)).expect_ok()
    assert a.query(closing_days, since=date(2026, 12, 21), days=5) == []
    a.call(close, date_=date(2026, 12, 25), until=date(2026, 12, 21), reason="Oops").expect_refused(
        message="Mon 21 December 2026 is before Fri 25 December 2026"
    )
    a.call(close, date_=FRIDAY, reason="Nowhere", room="cedar").expect_refused(
        message="There is no room cedar. See `rooms` for their ids."
    )


def test_members_cannot_administration() -> None:
    with pytest.raises(Denied):
        member().call(add_room, room="elm", name="Elm", capacity=2, opens="09:00", closes="17:00")
    with pytest.raises(Denied):
        member().call(close, date_=FRIDAY, reason="Mine")
    with pytest.raises(Denied):
        member().view(administration)


# The invariants, straight on the files.


def test_overlapping_bookings_written_directly_are_refused() -> None:
    row = "B-0009,oak,2026-11-27,15:00,16:00,2,Sneaky,sam,booked\n"
    world().change("bookings.csv", append=row).expect_refused(by=no_overlap)


def test_malformed_bookings_are_refused() -> None:
    w = world()
    w.change(
        "bookings.csv", append="B-0001,oak,2026-11-30,10:00,11:00,2,Twin,sam,cancelled\n"
    ).expect_refused(by=well_formed_bookings)
    w.change(
        "bookings.csv", append="B-0009,cedar,2026-11-30,10:00,11:00,2,Nowhere,sam,booked\n"
    ).expect_refused(by=well_formed_bookings)


def test_malformed_rooms_are_refused() -> None:
    world().change("rooms.csv", append='elm,Elm,0,09:00,17:00,"mon"\n').expect_refused(
        by=well_formed_rooms
    )


def test_malformed_closing_days_are_refused() -> None:
    w = world()
    w.change("closing_days.csv", append="2026-12-25,,Noël\n").expect_refused(
        by=well_formed_closures
    )
    w.change("closing_days.csv", append="2026-12-26,cedar,Gone\n").expect_refused(
        by=well_formed_closures
    )


# Queries.


def test_availability_says_why_a_room_is_not_free() -> None:
    oak, pine = member().query(availability, date_=FRIDAY, start="10:30", end="11:30", attendees=4)
    assert oak.free and oak.why_not == []
    assert not pine.free
    assert pine.why_not == ["Pine is already booked 10:00–11:00 (Design review, B-0002)"]


def test_availability_refuses_a_time_already_past() -> None:
    with pytest.raises(Refused, match="Thu 26 November 2026 at 07:00 is already past"):
        member().query(availability, date_=NOW.date(), start="07:00", end="07:30")


def test_availability_lists_the_free_rooms_smallest_first() -> None:
    free = member().query(availability, date_=FRIDAY, start="12:00", end="13:00", attendees=2)
    assert [(a.room, a.free) for a in free] == [("pine", True), ("oak", True)]


def test_bookings_narrow_by_room_and_days() -> None:
    m = member()
    assert [b.id for b in m.query(bookings)] == ["B-0002", "B-0001"]
    assert [b.id for b in m.query(bookings, room="oak")] == ["B-0001"]
    assert m.query(bookings, days=1) == []
    with pytest.raises(Refused, match="There is no room cedar"):
        m.query(bookings, room="cedar")


def test_an_empty_app() -> None:
    w = given(office, {}, now=NOW)
    assert w.connect("member").query(rooms) == []
    assert w.connect("member").query(closing_days) == []
    assert "No meeting room yet." in str(w.connect("member").view(week))
    assert "No meeting room yet." in str(w.connect("member").view(day))
    assert "No meeting room yet." in str(w.connect("admin").view(administration))
    w.connect("admin").call(
        add_room, room="oak", name="Oak", capacity=8, opens="08:00", closes="19:00"
    ).expect_ok()
    w.connect("member").call(
        book, room="oak", date_=FRIDAY, start="10:00", end="11:00", attendees=2, title="First"
    ).expect_ok()


# Views.


def test_the_week_shows_bookings_and_closing_days() -> None:
    m = member()
    this = m.view(week)
    assert this["arguments"] == {"week": "2026-11-23"}
    text = str(this)
    assert "This week" in text and "Client call" in text and "Standup" not in text
    assert "Sat 28" not in text  # no room opens on weekends
    later = str(m.view(week, week=date(2026, 12, 24)))
    assert "Week of 21 December" in later and "Christmas Day" in later
    assert "Closed: Repainting" in str(m.view(week, week=date(2026, 12, 1)))


def test_the_day_offers_cancel_only_for_own_bookings() -> None:
    screen = str(member().view(day, day=FRIDAY))
    assert "Client call" in screen and "Design review" in screen and "Standup" not in screen
    assert "B-0002" in screen and "B-0001" not in screen  # only Sam's own booking has Cancel
    assert "B-0001" in str(member(who="camille").view(day, day=FRIDAY))


def test_the_day_offers_book_only_where_a_room_is_open() -> None:
    m = member()
    christmas = str(m.view(day, day=date(2026, 12, 25)))
    assert "The office is closed: Christmas Day" in christmas and "find_room" not in christmas
    saturday = str(m.view(day, day=date(2026, 11, 28)))
    assert "Closed on Saturdays." in saturday and "find_room" not in saturday
    repainting = str(m.view(day, day=date(2026, 12, 1)))
    assert "Closed: Repainting." in repainting
    both_open = str(m.view(day, day=date(2026, 12, 2)))
    # One Book button fewer while Pine is being repainted.
    assert 0 < repainting.count("find_room") < both_open.count("find_room")
    assert "find_room" in str(m.view(day))
    past = str(m.view(day, day=date(2026, 11, 25)))
    assert "This day is past" in past and "find_room" not in past


def test_the_book_screen_lists_free_rooms_first_then_why_not() -> None:
    m = member()
    empty = m.view(find_room)
    assert empty["arguments"] == {"day": "2026-11-26"}
    assert "Find a room" in str(empty) and "Free" not in str(empty)
    asked = m.view(find_room, day=FRIDAY, start="10:30", end="11:30", attendees=3, title="Sync")
    text = str(asked)
    assert "Free 10:30–11:30" in text and "Not free" in text
    # Oak takes it, with a Book button; Pine says why it can't.
    assert text.index("Oak") < text.index("Not free") < text.index("Pine is already booked")
    assert text.count("'data-call': 'book'") == 1


def test_booking_from_the_book_screen_says_its_number() -> None:
    w = world()
    m = member(w)
    booking(m, room="pine", start="14:00", end="15:00").expect_ok()
    done = m.view(find_room, day=FRIDAY, start="14:00", end="15:00", booked="pine")
    assert "Booked B-0004: Pine, 14:00–15:00." in str(done) and "Book another" in str(done)


def test_the_book_screen_refuses_a_room_that_doesn_t_exist() -> None:
    with pytest.raises(Refused, match="There is no room cedar"):
        member().view(find_room, day=FRIDAY, room="cedar")


def test_the_book_screen_shows_a_room_s_day_when_the_day_view_opened_it() -> None:
    pine = member().view(find_room, day=FRIDAY, room="pine")
    assert pine["arguments"] == {"day": "2026-11-27", "room": "pine"}
    assert "Design review" in str(pine) and "Open 08:00–19:00, 4 seats" in str(pine)


def test_the_book_screen_says_why_it_cannot_search() -> None:
    m = member()
    assert "The office is closed: Christmas Day." in str(
        m.view(find_room, day=date(2026, 12, 25), start="10:00", end="11:00")
    )
    assert "Thu 26 November 2026 at 07:00 is already past." in str(
        m.view(find_room, start="07:00", end="08:00")
    )
    assert "The meeting must end after it starts: 11:00–10:00." in str(
        m.view(find_room, day=FRIDAY, start="11:00", end="10:00")
    )
    saturday = str(m.view(find_room, day=date(2026, 11, 28), start="10:00", end="11:00"))
    assert "No room is free then." in saturday and "Oak is closed on Saturdays" in saturday


def test_the_admin_screen() -> None:
    w = world()
    admin(w).call(
        close, date_=date(2026, 12, 28), until=date(2026, 12, 31), reason="Winter break"
    ).expect_ok()
    screen = str(admin(w).view(administration))
    assert "Mon–Fri, 08:00–19:00" in screen and "Add a room" in screen
    assert '{"room":"oak"}' in screen  # Edit lays the screen out again with the room
    assert "Christmas Day" in screen and "Repainting" in screen
    assert "Mon 28 – Thu 31 December 2026" in screen and "Whole office" in screen


def test_the_admin_screen_adds_or_edits_a_room_as_a_step() -> None:
    w = world()
    a = admin(w)
    new = a.view(administration, add=True)
    assert new["arguments"] == {"add": True}
    assert "Add a room" in str(new) and "add_room" in str(new) and "update_room" not in str(new)
    oak = a.view(administration, room="oak")
    assert oak["arguments"] == {"room": "oak"}
    assert "Edit Oak" in str(oak) and "update_room" in str(oak) and "Monday to Friday" in str(oak)
    a.call(update_room, room="oak", days="mon,fri").expect_ok()
    # Days outside the presets stay offered.
    assert "Mon, Fri" in str(a.view(administration, room="oak"))
    assert "Changes saved." in str(a.view(administration, room="oak", saved=True))
    assert "Room added." in str(a.view(administration, add=True, saved=True))
    with pytest.raises(Refused):
        a.view(administration, room="cedar")
    with pytest.raises(Denied):
        member(w).view(administration, add=True)


def test_a_weekly_meeting_is_planned_in_a_transaction() -> None:
    m = member()
    plan = m.begin()
    for n in range(4):
        monday = date(2026, 11, 30) + timedelta(days=7 * n)
        plan.call(
            book, room="oak", date_=monday, start="11:00", end="12:00", attendees=5, title="Team"
        ).expect_ok()
    inside = plan.query(bookings, since=date(2026, 11, 30), days=28, mine=True)
    assert [b.title for b in inside] == ["Team"] * 4
    assert m.query(bookings, since=date(2026, 11, 30), days=28, mine=True) == []
    plan.commit().expect_ok()
    assert len(m.query(bookings, since=date(2026, 11, 30), days=28, mine=True)) == 4


def test_a_plan_that_breaks_a_rule_is_refused_inside_and_rolled_back() -> None:
    m = member()
    plan = m.begin()
    plan.call(
        book, room="oak", date_=FRIDAY, start="10:00", end="11:00", attendees=2, title="Team"
    ).expect_ok()
    plan.call(
        book, room="oak", date_=FRIDAY, start="14:30", end="15:00", attendees=2, title="Team"
    ).expect_refused(message="Oak is already booked 14:00–15:30 (Client call, B-0001)")
    plan.rollback()
    assert [b.id for b in m.query(bookings, room="oak")] == ["B-0001"]
