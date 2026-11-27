---
icon: lucide/layers
description: "The parts of a Cliquet app, the path of every change, where the data lives, and the sandbox that runs an app's code."
---

# Architecture

This guide describes what a Cliquet app is made of, how a change goes through it, where its data lives, and the sandbox that runs its code. You never need to read an app's code, but knowing its parts helps you ask for what you want.

## The division of work

An app does two kinds of work. Most of its code usually goes into the large, fiddly kind: reading documents, entering and sorting records, reports and search, talking to other services. The rest is small, but must never fail: keeping the data valid, deciding who sees and changes what, and remembering every change. AI agents now do the first kind well, so a Cliquet app does only the second.

| Work | Done by |
| --- | --- |
| Reading statements, receipts, emails and photos | The agent, which sends the result to one of the app's actions |
| Entering and sorting records | The agent, following the app's instructions |
| Search, reports and one-off questions | The agent, through the app's queries, which give exact answers |
| Banks, calendars and email | The agent, with its own connectors: the app connects to nothing |
| Keeping the data valid | Cliquet, through the app's rules: a change that breaks one is refused, whoever makes it |
| Access, history and undo | Cliquet, the same way for every app |
| Screens | Cliquet, from the app's views |

So an app stays small enough for an agent to write in an hour, and strict enough that a weaker agent can't damage it.

## The parts of an app

An app is one short Python file, `app.py`, with its data files, its instructions for agents, and its tests. It declares five kinds of parts:

| Part | What it is | In the meeting rooms |
| --- | --- | --- |
| **Data** | Plain files, like the sheets of a spreadsheet | `rooms.csv`, `bookings.csv`, `closing_days.csv` |
| **Actions** | The only ways the data can change, each open to some roles | Book a room, move or cancel a booking |
| **Rules** | What must always be true of the data. In the code, they are called *invariants* | Bookings of the same room never overlap |
| **Queries** | Questions the app answers, without changing anything | Which rooms are free, and why the others aren't |
| **Screens** | Pages that show the data, with buttons for the actions. In the code, they are called *views* | The week, the day, the search for a free room |

Cliquet provides everything around them: storage and history, undo, transactions, sign-in, roles and sharing, an MCP server URL and a web page for each app, and the sandbox. These are the parts that go wrong when an agent writes a whole app from scratch. In Cliquet, they are written once and tested, and your assistant writes only what is particular to your app.

The **Tools and rules** tab of an app's page in the console lists its actions, queries, screens and rules, for each role.

### A rule in code

A rule is a short function that lists what is wrong with the data. An empty list means the rule holds:

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

The builder's guides, which Cliquet gives your assistant, describe how to write an app. You can [read them on GitHub](https://github.com/etiennecrb/cliquet/tree/main/packages/server/src/cliquet_server/builder/guides).

## A change, step by step

Every change goes the same way, whoever asks for it: you, a colleague on a screen, or an agent.

<picture class="cq-diagram"><source media="(max-width: 40em)" srcset="/docs/assets/diagrams/change-tall.svg"><img src="/docs/assets/diagrams/change.svg" alt="A change is tried on a copy of the data, every rule is checked, then the change is kept and saved in the history, or refused with the reason."></picture>

1. Someone asks for a change, such as booking Oak from 15:00 to 16:00. Cliquet checks that their role may use the action.
2. Cliquet runs the action on a copy of the data, then checks every rule against the result.
3. If every rule holds, the change is kept and saved in the [history](../history.md). If one breaks, nothing is kept, and Cliquet says why: here, Oak is already booked from 14:00 to 15:30.

So the data can't break a rule, even when an agent makes a mistake.

## Data and history

An app's data is plain files, such as CSV or JSON, in the app's own folder. The app's code can read and write only that folder. You can [download](account.md#download-an-app) the files at any time and open them in a spreadsheet.

Each app has its own git repository, and each kept change is a commit in it, with who made it, with which role, and why. That is what the history shows, and what undo reverses.

Once an app is published, its data changes only through its actions, or through a *migration*: a short script that comes with a new version and updates the data to its new shape.

## The sandbox

The *sandbox* is where an app's code runs. Cliquet never runs an app's code on the server, which holds everyone's data. Each call goes to the sandbox of the app's owner instead. On app.cliquet.dev, each owner has their own sandbox, a small virtual machine called a Fly Sprite, and no one else's code runs in it.

The sandbox holds the apps' code, never their data. For each call, Cliquet sends the app only its own files, and takes back the files it changed. Cliquet then checks the answer as if the code could have gone wrong: only the app's own files, sizes within limits, and screens checked again. Roles are checked before any code runs.

Within one owner's sandbox, there is no wall between that owner's apps: one app could read another's code, but never its data. A query or a screen gets its files read-only.

### No network

Once a sandbox is set up, its network is shut for good. An app can't send data anywhere, or fetch anything. Data from elsewhere comes in through the app's actions, which your agents call.

### The same answer every time

An app takes the current time only from Cliquet's clock, `ctx.now`, and may not use chance or start other programs. So the same data always gives the same result, which makes tests reliable and makes a change safe to try twice. These are checks on the app's code, not a wall: the sandbox and the checks of every answer are the wall.

### Limits

Each call has limits on its time, on file sizes and, where the sandbox allows, on memory. A call over a limit is stopped, and nothing is kept.

## Errors

When a call fails because of the app's code, Cliquet keeps the error for up to 30 days. An error may quote your data, so only the owner reads its full text, on the app's **Errors** tab. Anyone else sees the kind of error and a reference to give the owner.

A refusal is not an error. When a change breaks a rule or an action refuses it, whoever asked sees why.

## On your own server

A server you run yourself can give each owner a Docker container instead of a Sprite: no network, a read-only system, and a user of its own. A container shares the host's kernel, which a Sprite doesn't. A server can also run apps with no sandbox, for your own apps on your own machine, but never on a public URL. See [Sandboxes](self-hosting/sandboxes.md).
