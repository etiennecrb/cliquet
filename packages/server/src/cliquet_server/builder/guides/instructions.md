# Writing instructions.md

`instructions.md` is sent to every agent that connects to the app. It is all that agent knows about the app, besides the description of each tool. Write it for the agent using the app:

- what the app keeps;
- each procedure, step by step;
- what each refusal means and what to do;
- which query answers which question.

Start from the person's existing skill or procedure if there is one, or else from the request: one section per procedure, then the refusals, then the questions.

`read_example("meeting-rooms", "instructions.md")`, `read_example("environments", "instructions.md")` and `read_example("ledger", "instructions.md")` are complete ones. The first, shortened:

```markdown
# Meeting rooms

The meeting rooms of a small office: each room's seats and opening hours, the closing days, and the bookings. You book, move and cancel rooms for people, find a free room, and say who has a room when. Every time is local to Paris, on a 24-hour clock (`HH:MM`).

## Roles

- `member`: see rooms and bookings; book rooms; move and cancel their own bookings before they start.
- `admin`: the same, plus move and cancel anyone's booking, add or change rooms, and set closing days.

## Booking a room

1. If the person didn't name a room, call `availability` with the day, start, end and the number of people. It lists the free rooms first, smallest first: offer the first one. For the others, `why_not` says why.
2. Call `book` with the room's id, the day, start, end, how many people will attend, and what the meeting is for. It answers the booking's number, such as `B-0012`: give it to the person.
3. Remark: what the person asked, such as "asked for a room for the design review".

## When a write is refused

- **Bookings of the same room never overlap:** two bookings of a room share some time. A booking may start the minute another ends.
- **No booking has more people than its room seats:** after `update_room` lowered a room's seats. Move the listed bookings to a larger room, or keep the seats.

## Questions

- "Is a room free at 3 tomorrow?": `availability`.
- "What are my bookings?": `bookings` with `mine: true`.

## The screens

`week` is a screen for people: every room's bookings day by day. Show it when someone asks what's on this week, where your client can draw it. To answer a question yourself, use the queries.
```

## What goes in

- **What the app keeps,** in two or three sentences, and what the agent does with it.
- **The roles,** when the app has several: what each one may do.
- **One section per procedure.** Say which tool to call, how many times, and with what. Say where each argument comes from, such as a receipt's number. Say what not to pass.
- **The refusals.** One line per invariant, under the rule's own words, since the agent sees those words when a call is refused. Say what it means and what to do next.
- **The questions** people ask, each with the query that answers it.
- **When to look,** for an app agents should consult before they act elsewhere, such as a register of environments before a deploy: say when, in a section of its own, and that they never guess what the app knows.
- **The screen,** when the app has a view: what it shows, and when to show it.
- **The remark,** in each procedure that writes: what its `app_remark` says.

## Ask for `app_remark` with every write

The history says who changed what, and when. `app_remark` says why: one line kept with the change. Cliquet asks agents for it with every change. Ask for it in `instructions.md` too: in each procedure that writes, say what this app's remark holds:

- **A write from a document:** where it came from, such as "statement of March, line 14".
- **A write the person asked for:** what they asked, such as "asked to fix a typo in the name".

Never people's names or the values: the change already holds the values, and history keeps the remark for good. A change made on a screen carries no remark.

## When to tell agents to use a transaction

Every agent with a role that may write can begin a transaction, call the actions in it, read the result through the app's queries or show it on a view, then commit or roll back. Cliquet already tells agents how. Say in `instructions.md` *when* this app wants one:

- **A write the person should check before it is kept,** such as a large import: "Import a statement in a transaction, show `month` with the same transaction, and commit once the person agrees."
- **Several writes that belong together,** such as a month of bank entries and the fixes they need: "Import the month and make its fixes in one transaction, check `unbalanced` is empty, then commit."
- **A write that's unusual,** such as a correction, or a deletion of several entries: "Delete the duplicates in a transaction, show `month` to the person, and commit once they agree."

Say what to check before committing, with which query or view. Leave it out for small writes that stand on their own: each call is checked by the rules and kept at once.

## When to tell agents to pass `app_if_unchanged_since`

Every query's answer names the version of the data it read, as `app_data_version`. An action called with that version as `app_if_unchanged_since` is refused, without running, if the data changed meanwhile: the agent reads again and decides again. Cliquet already tells agents how. Say in `instructions.md` *which* writes of this app need it: those that depend on what the agent just read.

- **A write decided from a count or a balance,** such as a reorder: "Before `order`, read `to_reorder`, and pass its `app_data_version` to `order` as `app_if_unchanged_since`."
- **A write that replaces what was read,** such as a whole list sent back: "Read the connections with `environment`, then call `set_connections` with that answer's `app_data_version`."

Leave it out for a write that is right whatever the others did, such as recording a sale that happened. An invariant still catches a broken rule either way. For several writes that belong together, use a transaction: it is kept only if nobody changed the data since it began.

## What stays out

Cliquet adds this to every app's instructions by itself. Don't repeat it:

- the caller's role, and that the tools a role can't use aren't listed;
- that every change is checked against the rules, and that a refused call changes nothing;
- `app_history`, `app_changes_since` and `app_revert`;
- `app_request_id`, for a call sent again after a lost connection;
- what `app_remark` is, and its limits;
- how to pass a query's `app_data_version` as `app_if_unchanged_since`, and what a refusal for changed data means;
- how to use a transaction: `app_begin_transaction`, the `app_transaction` argument, `app_transaction_calls`, `app_commit_transaction` and `app_rollback_transaction`.

Don't describe each tool's arguments either: the agent has their descriptions, from `app.py`.
