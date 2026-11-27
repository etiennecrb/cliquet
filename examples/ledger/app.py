# /// script
# requires-python = ">=3.14"
# dependencies = ["cliquet-sdk>=0.0.1,<0.1", "beancount>=3.2,<4", "beanquery>=0.2,<0.3"]
#
# [tool.uv.sources]
# cliquet-sdk = { path = "../../packages/sdk", editable = true }
# ///
"""A household's Beancount ledger, kept by agents from bank statements: its budget, net worth
and the return of each asset."""

import calendar
import csv
import itertools
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from functools import cache, lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, cast, get_args

import beanquery
from beancount import loader
from beancount.core import getters, prices
from beancount.core.data import Balance, Document, Open, Price, Transaction, filter_txns
from beancount.core.inventory import Inventory
from beancount.parser import parser, printer
from beanquery.parser import ast as bql_ast

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

# Beancount writes a cache next to the ledger, which isn't one of the app's files.
loader.initialize(use_cache=False)

BOOK = "ledger.beancount"
RULES = "rules.csv"
CLASSES = "classes.csv"

BOTH = ["owner", "reader"]
OWNER = ["owner"]

ledger = App(
    "ledger",
    files=[BOOK, RULES, CLASSES],
    roles=BOTH,
    accent="#1e3a8a",
    icon="wallet",
    timezone="Europe/Paris",
    language="en",
)

# Plugins shipped with Beancount itself: library code, reading no other file.
ALLOWED_PLUGINS = ("beancount.plugins.",)

AccountName = Annotated[
    str,
    Field(
        description="An account's full name, such as Assets:Bank:Checking, as listed by `accounts`"
    ),
]
Text = Annotated[str, Field(pattern=r"\S", description="Part of it, in any case")]
Period = Literal["ytd", "q1", "q2", "q3", "q4", "year"]
PERIOD_DESC = (
    "ytd: 1 January to today's day of the year; q1 to q4: a calendar quarter; year: the whole "
    "year. The same window is taken in every year, so years compare."
)
Year = Annotated[int | None, Field(ge=1990, le=2100, description="This year by default")]


# Reading the ledger. ---------------------------------------------------------------


@lru_cache(maxsize=4)
def _parse(text: str) -> tuple[list[Any], list[Any], dict[str, Any]]:
    """The ledger's entries as written, before Beancount's plugins and checks."""
    return parser.parse_string(text)


def _outside(text: str) -> list[str]:
    """What in the ledger would read another file or run code other than Beancount's own
    plugins. Parsing alone does neither."""
    entries, _, options = _parse(text)
    found = [f'include "{name}"' for name in options["include"]]
    found += [
        f'plugin "{name}"' for name, _ in options["plugin"] if not name.startswith(ALLOWED_PLUGINS)
    ]
    found += [f'option "{o}"' for o in ("documents", "insert_pythonpath") if options[o]]
    found += [f"document {e.filename}" for e in entries if isinstance(e, Document)]
    return found


def _book_text(path: str | Path = BOOK) -> str:
    """The ledger's text, empty before the first entry."""
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return ""


@lru_cache(maxsize=4)
def _load_text(text: str) -> tuple[list[Any], list[Any], dict[str, Any]]:
    if _outside(text):
        raise Refused(
            "The ledger names another file or a plugin outside Beancount: remove it with "
            "`fix_entry`."
        )
    return loader.load_string(text)


def _load(path: str | Path = BOOK) -> tuple[list[Any], list[Any], dict[str, Any]]:
    """The ledger's entries in date order, its errors and its options."""
    return _load_text(_book_text(path))


@dataclass(frozen=True)
class Roots:
    assets: str
    liabilities: str
    income: str
    expenses: str
    currency: str


def _roots(options: dict[str, Any]) -> Roots:
    return Roots(
        options["name_assets"],
        options["name_liabilities"],
        options["name_income"],
        options["name_expenses"],
        (options["operating_currency"] or ["EUR"])[0],
    )


@dataclass(frozen=True)
class Posted:
    day: date
    account: str
    number: Decimal
    currency: str
    entry: Transaction = field(compare=False)


def _postings(entries: list[Any]) -> list[Posted]:
    return [
        Posted(t.date, p.account, p.units.number, p.units.currency, t)
        for t in filter_txns(entries)
        for p in t.postings
        if p.units is not None and p.units.number is not None
    ]


def _balances(entries: list[Any]) -> list[Balance]:
    return [e for e in entries if isinstance(e, Balance)]


def _opened(entries: list[Any], names: list[str] | tuple[str, ...] = ()) -> dict[str, Open]:
    """The open accounts by name, or a refusal if one of `names` isn't one of them."""
    opened = {
        name: opening
        for name, (opening, closing) in getters.get_account_open_close(entries).items()
        if closing is None
    }
    missing = [n for n in names if n not in opened]
    if missing:
        raise Refused(f"Not an open account: {', '.join(missing)}. See `accounts`.")
    return opened


# Config files: classification rules and asset classes. ------------------------------

RULE_COLUMNS = ["pattern", "direction", "min", "max", "account", "when", "note"]
CLASS_COLUMNS = ["pattern", "class", "depth", "upkeep"]


def _read_csv(file: str) -> list[dict[str, str]]:
    try:
        with open(file, newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def _write_csv(file: str, columns: list[str], rows: list[dict[str, str]]) -> None:
    with open(file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows({c: row.get(c, "") for c in columns} for row in rows)


Direction = Literal["", "credit", "debit"]
When = Literal["always", "fallback"]


class Rule(BaseModel):
    pattern: Annotated[
        str,
        Field(
            pattern=r"\S",
            description="A regular expression searched, ignoring case, in the narration: the "
            "statement's detail lines then its label, such as `REF 4471 DIRECT DEBIT CITY POWER`",
        ),
    ]
    direction: Annotated[Direction, Field(description="Only credits, only debits, or both")] = ""
    min: Annotated[Decimal | None, Field(description="Smallest amount it applies to")] = None
    max: Annotated[Decimal | None, Field(description="Largest amount it applies to")] = None
    account: Annotated[
        str,
        Field(
            description="The other side, as listed by `accounts`. Empty: the line needs a split "
            "or the person's answer, as `note` says"
        ),
    ] = ""
    when: Annotated[
        When,
        Field(
            description="always: wins over the ledger's history; fallback: used only when the "
            "history knows nothing, and the choice is listed for review"
        ),
    ] = "always"
    note: str = ""


def _rules() -> list[Rule]:
    return [
        Rule(
            pattern=r["pattern"],
            # The rules_valid invariant keeps these two columns to their choices.
            direction=cast(Direction, r["direction"]),
            min=Decimal(r["min"]) if r["min"] else None,
            max=Decimal(r["max"]) if r["max"] else None,
            account=r["account"],
            when=cast(When, r["when"] or "always"),
            note=r["note"],
        )
        for r in _read_csv(RULES)
    ]


def _rule_row(rule: Rule) -> dict[str, str]:
    return {
        "pattern": rule.pattern,
        "direction": rule.direction,
        "min": "" if rule.min is None else f"{rule.min:f}",
        "max": "" if rule.max is None else f"{rule.max:f}",
        "account": rule.account,
        "when": rule.when,
        "note": rule.note,
    }


Upkeep = Literal["", "monthly-prices", "yearly-prices", "revalue", "interest"]


class AssetClass(BaseModel):
    pattern: Annotated[
        str,
        Field(
            pattern=r"\S",
            description="A regular expression matched at the start of an asset or liability "
            "account, such as ^Assets:Broker(:|$)",
        ),
    ]
    name: Annotated[
        str, Field(pattern=r"\S", description="The class, such as Stocks, as screens show it")
    ]
    depth: Annotated[
        int,
        Field(
            ge=1,
            le=6,
            description="How many parts after the root name one asset, such as 2 for "
            "Assets:Insurance:Life",
        ),
    ] = 2
    upkeep: Annotated[
        Upkeep,
        Field(
            description="What `checkup` asks for its assets: monthly-prices for listed funds, "
            "yearly-prices for a yearly statement's values, revalue for what is booked at cost, "
            "interest for the rate a savings account served, or nothing"
        ),
    ] = ""


def _classes() -> list[AssetClass]:
    return [
        AssetClass(
            pattern=r["pattern"],
            name=r["class"],
            depth=int(r["depth"] or 2),
            upkeep=cast(Upkeep, r["upkeep"]),  # classes_valid keeps it to its choices
        )
        for r in _read_csv(CLASSES)
    ]


OTHER_CLASS = "Other"


def _class_of(account: str, classes: list[AssetClass]) -> AssetClass | None:
    return next((c for c in classes if re.search(c.pattern, account)), None)


def _upkeep(account: str, classes: list[AssetClass]) -> Upkeep:
    """What `checkup` asks for an account, from its class: "" for nothing."""
    found = _class_of(account, classes)
    return found.upkeep if found else ""


def _classify_account(account: str, classes: list[AssetClass]) -> tuple[str, str]:
    """An asset or liability account's class and the asset it belongs to, such as
    ("Life insurance", "Insurance:Life")."""
    parts = account.split(":")
    found = _class_of(account, classes)
    if found:
        return found.name, ":".join(parts[1 : 1 + found.depth])
    return OTHER_CLASS, ":".join(parts[1:3])


# Valuation. --------------------------------------------------------------------------


@dataclass
class Holding:
    account: str
    cost: Decimal = Decimal(0)
    value: Decimal = Decimal(0)
    unvalued: set[str] = field(default_factory=set)  # commodities with no price


def _value(inv: Inventory, pm: Any, day: date, cur: str, account: str) -> Holding:
    h = Holding(account)
    for pos in inv:
        units = pos.units
        if units.currency == cur:
            h.cost += units.number
            h.value += units.number
            continue
        cost = (
            units.number * pos.cost.number
            if pos.cost is not None and pos.cost.currency == cur and pos.cost.number is not None
            else Decimal(0)
        )
        h.cost += cost
        _, rate = prices.get_price(pm, (units.currency, cur), day)
        if rate is None:
            h.value += cost
            if units.number:
                h.unvalued.add(units.currency)
        else:
            h.value += units.number * rate
    return h


@lru_cache(maxsize=4)
def _price_map(text: str) -> Any:
    return prices.build_price_map(_load_text(text)[0])


def _snapshots(days: list[date]) -> dict[date, dict[str, Holding]]:
    """Each asset and liability account's cost and market value at the end of each day."""
    text = _book_text()
    entries, _, options = _load_text(text)
    r, pm = _roots(options), _price_map(text)
    wanted = (r.assets + ":", r.liabilities + ":")
    invs: dict[str, Inventory] = defaultdict(Inventory)
    txns = list(filter_txns(entries))
    i, out = 0, {}
    for day in sorted(set(days)):
        while i < len(txns) and txns[i].date <= day:
            for p in txns[i].postings:
                if p.account.startswith(wanted):
                    invs[p.account].add_position(p)
            i += 1
        out[day] = {
            a: _value(inv, pm, day, r.currency, a) for a, inv in invs.items() if not inv.is_empty()
        }
    return out


# Periods. ----------------------------------------------------------------------------


def _clip(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _window(year: int, period: str, today: date) -> tuple[date, date] | None:
    """The period's first and last day in a year, cut at today; None if it hasn't started."""
    if period == "ytd":
        start, end = date(year, 1, 1), _clip(year, today.month, today.day)
    elif period == "year":
        start, end = date(year, 1, 1), date(year, 12, 31)
    else:
        q = int(period[1])
        start = date(year, 3 * q - 2, 1)
        end = _clip(year, 3 * q, 31)
    if start > today:
        return None
    return start, min(end, today)


# Queries for agents. -----------------------------------------------------------------


class BqlResult(BaseModel):
    columns: list[str]
    types: list[str]
    rows: Annotated[
        list[list[str | None]],
        Field(
            description="Every value as text: numbers, amounts, inventories and dates as "
            "Beancount writes them"
        ),
    ]
    truncated: Annotated[bool, Field(description="More rows than `limit`: narrow the query")]


READ_STATEMENTS = (bql_ast.Select, bql_ast.Balances, bql_ast.Journal, bql_ast.Print)


def _cell(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (set, frozenset, list, tuple)):
        return ", ".join(sorted(str(v) for v in value))
    if isinstance(value, Decimal):
        return f"{value:f}"
    return str(value)


@ledger.query(roles=BOTH)
def bql(
    ctx: QueryContext,
    query: Annotated[
        str,
        Field(
            pattern=r"\S",
            description="One BQL statement, such as SELECT year, sum(convert(position,'EUR')) "
            "WHERE account ~ '^Expenses' GROUP BY year",
        ),
    ],
    limit: Annotated[int, Field(ge=1, le=5000)] = 500,
) -> BqlResult:
    """Run a read-only beanquery (BQL) statement on the ledger: SELECT, BALANCES, JOURNAL or
    PRINT. For anything the other queries don't answer."""
    try:
        statement = beanquery.parser.parse(query)
    except beanquery.ParseError as e:
        raise Refused(f"BQL syntax error: {e}") from None
    if not isinstance(statement, READ_STATEMENTS):
        raise Refused(
            "Only SELECT, BALANCES, JOURNAL and PRINT are allowed: the ledger is changed through "
            "the app's actions."
        )
    entries, errors, options = _load()
    conn = beanquery.connect("beancount:", entries=entries, errors=errors, options=options)
    try:
        cursor = conn.execute(query)
        rows = cursor.fetchall()
    except (beanquery.CompilationError, beanquery.ProgrammingError, beanquery.DataError) as e:
        raise Refused(f"BQL error: {e}") from None
    columns = [c.name for c in cursor.description]
    types = [getattr(c.datatype, "__name__", str(c.datatype)) for c in cursor.description]
    if isinstance(statement, bql_ast.Print) and rows and not isinstance(rows[0], tuple):
        rows = [(printer.format_entry(r[0]) if r else "",) for r in rows]
    return BqlResult(
        columns=columns,
        types=types,
        rows=[[_cell(v) for v in row] for row in rows[:limit]],
        truncated=len(rows) > limit,
    )


class AccountInfo(BaseModel):
    name: str
    currencies: list[str]
    opened: date
    asset_class: Annotated[
        str | None, Field(description="For an asset or liability, its class, as in `classes`")
    ]


@ledger.query(roles=BOTH)
def accounts(
    ctx: QueryContext,
    text: Annotated[str | None, Field(description="Part of the name, in any case")] = None,
) -> list[AccountInfo]:
    """The open accounts, by their exact names. Write entries with these names."""
    entries, _, options = _load()
    r, classes = _roots(options), _classes()
    return [
        AccountInfo(
            name=name,
            currencies=list(o.currencies or []),
            opened=o.date,
            asset_class=_classify_account(name, classes)[0]
            if name.startswith((r.assets + ":", r.liabilities + ":"))
            else None,
        )
        for name, o in sorted(_opened(entries).items())
        if text is None or text.casefold() in name.casefold()
    ]


class AccountStatus(BaseModel):
    account: str
    last_entry: date | None
    last_balance_day: Annotated[date | None, Field(description="Its last `balance` assertion")]
    last_balance: str | None
    balance: Annotated[dict[str, Decimal], Field(description="Units held now, by commodity")]


@ledger.query(roles=BOTH)
def status(
    ctx: QueryContext,
    text: Annotated[
        str | None, Field(description="Part of the account's name, in any case")
    ] = None,
) -> list[AccountStatus]:
    """Where each open asset and liability account stops: its last entry, its last `balance`
    and what it holds now. Import a statement from the day of its last `balance`."""
    entries, _, options = _load()
    r = _roots(options)
    last: dict[str, date] = {}
    totals: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for p in _postings(entries):
        last[p.account] = p.day
        totals[p.account][p.currency] += p.number
    checks = {b.account: b for b in _balances(entries)}
    return [
        AccountStatus(
            account=name,
            last_entry=last.get(name),
            last_balance_day=checks[name].date if name in checks else None,
            last_balance=str(checks[name].amount) if name in checks else None,
            balance={c: n for c, n in sorted(totals[name].items()) if n},
        )
        for name in sorted(_opened(entries))
        if name.startswith((r.assets + ":", r.liabilities + ":"))
        and (text is None or text.casefold() in name.casefold())
    ]


class Found(BaseModel):
    line: int
    text: Annotated[str, Field(description="The whole entry, ready to copy into `fix_entry`")]


@ledger.query(roles=BOTH)
def find_entries(
    ctx: QueryContext, text: Text, limit: Annotated[int, Field(ge=1, le=200)] = 20
) -> list[Found]:
    """Each entry whose text contains `text`, latest first: its line and its whole text."""
    found: list[Found] = []
    inside = False
    for number, line in enumerate(_book_text().splitlines(), 1):
        if line[:1].isdigit():
            found.append(Found(line=number, text=line))
            inside = True
        elif inside and line[:1] in (" ", "\t") and line.strip():
            found[-1].text += "\n" + line
        else:
            inside = False
    hits = [f for f in found if text.casefold() in f.text.casefold()]
    return list(reversed(hits))[:limit]


class Chunk(BaseModel):
    first: int
    last: int
    lines: Annotated[int, Field(description="The ledger's number of lines")]
    text: str


@ledger.query(roles=BOTH)
def export_ledger(
    ctx: QueryContext,
    from_line: Annotated[int, Field(ge=1)] = 1,
    count: Annotated[int, Field(ge=1, le=10000)] = 5000,
) -> Chunk:
    """The ledger's text, a part at a time, to keep a local copy, such as for fava."""
    lines = _book_text().splitlines(keepends=True)
    part = lines[from_line - 1 : from_line - 1 + count]
    return Chunk(
        first=from_line, last=from_line + len(part) - 1, lines=len(lines), text="".join(part)
    )


# Classification. ---------------------------------------------------------------------


def _narration(label: str, detail: str) -> str:
    """As the ledger writes it: a card line's label alone, else the detail lines then the
    label, with single spaces."""
    label, detail = " ".join(label.split()), " ".join(detail.split())
    if label.startswith("CARD ") or not detail:
        return label
    return f"{detail} {label}"


def _merchant(narration: str) -> str | None:
    """A merchant key: CARD:<name>, or <type>:<counterparty> for a debit or a transfer."""
    m = re.search(r"CARD \d{2}/\d{2}/\d{2} (.*?) \*\d+", narration)
    if m:
        return "CARD:" + " ".join(m.group(1).split()).upper()
    m = re.search(
        r"(DIRECT DEBIT|TRANSFER FROM|TRANSFER TO|TRANSFER)\s+"
        r"([A-Z0-9][A-Z0-9 .'&*-]+?)(?:\s+REF|\s+--|$)",
        narration,
    )
    return f"{m.group(1)}:{m.group(2).strip().upper()}" if m else None


def _history(entries: list[Any], bank: str) -> dict[str, Counter[str]]:
    """For each merchant key, how often each other account was used, on one bank account."""
    mapping: dict[str, Counter[str]] = defaultdict(Counter)
    for t in filter_txns(entries):
        if t.flag == "P" or not any(p.account == bank for p in t.postings):
            continue
        key = _merchant(t.narration or "")
        others = [p.account for p in t.postings if p.account != bank]
        if key and others:
            mapping[key][others[0]] += 1
    return mapping


class StatementLine(BaseModel):
    day: date
    label: Annotated[
        str,
        Field(
            description="The statement's label line, such as `CARD 29/05/26 CORNER SHOP *4821` "
            "or `DIRECT DEBIT CITY POWER`"
        ),
    ]
    detail: Annotated[
        str, Field(description="The detail lines under it, joined; empty for a card")
    ] = ""
    amount: Annotated[Decimal, Field(ge=0, description="As on the statement, without sign")]
    credit: Annotated[bool, Field(description="In the credit column")]


Source = Literal["rule", "history", "fallback", "manual", "unknown"]


class Suggestion(BaseModel):
    day: date
    narration: Annotated[str, Field(description="As the entry will be written")]
    amount: Annotated[Decimal, Field(description="Signed: negative when money leaves")]
    account: Annotated[
        str | None,
        Field(
            description="The suggested other side; none when the line needs a split or the "
            "person's answer"
        ),
    ]
    source: Source
    note: str
    review: Annotated[bool, Field(description="List it for the person to check")]


def _matches(rule: Rule, narration: str, line: StatementLine) -> bool:
    if rule.direction and (rule.direction == "credit") != line.credit:
        return False
    if rule.min is not None and line.amount < rule.min:
        return False
    if rule.max is not None and line.amount > rule.max:
        return False
    return re.search(rule.pattern, narration, re.IGNORECASE) is not None


@ledger.query(roles=OWNER)
def classify(
    ctx: QueryContext,
    lines: list[StatementLine],
    bank: AccountName = "Assets:Bank:Checking",
) -> list[Suggestion]:
    """Suggest the other account for each statement line: the rules marked `always` first,
    then the ledger's history for that merchant, then the `fallback` rules, then Extras for an
    unknown debit. Lines with `review` go to the person."""
    entries, _, options = _load()
    _opened(entries, [bank])
    rules, history = _rules(), _history(entries, bank)
    extras = f"{_roots(options).expenses}:Living:Extras"
    out = []
    for line in lines:
        narr = _narration(line.label, line.detail)
        rule = next((r for r in rules if r.when == "always" and _matches(r, narr, line)), None)
        fallback = next(
            (r for r in rules if r.when == "fallback" and _matches(r, narr, line)), None
        )
        key = _merchant(narr)
        pick: tuple[str | None, Source, str, bool]
        if rule:
            source: Source = "rule" if rule.account else "manual"
            pick = (rule.account or None, source, rule.note, not rule.account)
        elif key and key in history:
            (account, n), total = history[key].most_common(1)[0], sum(history[key].values())
            pick = (account, "history", f"{n} of {total} earlier entries", n * 3 < total * 2)
        elif fallback:
            source = "fallback" if fallback.account else "manual"
            pick = (fallback.account or None, source, fallback.note, True)
        elif line.credit:
            pick = (None, "unknown", "Unknown credit: ask the person.", True)
        else:
            pick = (extras, "fallback", "Unknown merchant: Extras by default.", True)
        account, source, note, review = pick
        out.append(
            Suggestion(
                day=line.day,
                narration=narr,
                amount=line.amount if line.credit else -line.amount,
                account=account,
                source=source,
                note=note,
                review=review,
            )
        )
    return out


@ledger.query(roles=BOTH)
def rules(ctx: QueryContext) -> list[Rule]:
    """The classification rules, in the order they apply."""
    return _rules()


@ledger.query(roles=BOTH)
def classes(ctx: QueryContext) -> list[AssetClass]:
    """How asset and liability accounts are grouped into classes and assets, first match
    wins, and what `checkup` asks for each class."""
    return _classes()


# Writing. ----------------------------------------------------------------------------


def _quoted(text: str) -> str:
    return '"' + " ".join(text.split()).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _append(text: str) -> None:
    with open(BOOK, "a") as f:
        f.write(text)


class Leg(BaseModel):
    account: AccountName
    amount: Annotated[
        Decimal | None, Field(description="Leave it out on one leg at most: Beancount fills it in")
    ] = None


class ImportLine(StatementLine):
    account: Annotated[
        str | None,
        Field(description="The other side, as `classify` suggests and the person confirmed"),
    ] = None
    legs: Annotated[
        list[Leg],
        Field(
            description="For a split entry, such as a salary or a loan: the other legs, in "
            "place of `account`"
        ),
    ] = Field([])


class Imported(BaseModel):
    added: int
    skipped: Annotated[
        list[str],
        Field(description="Already in the ledger: same day, amount and narration on the account"),
    ]
    balances: Annotated[list[str], Field(description="The `balance` assertions written")]


@ledger.action(roles=OWNER)
def import_statement(
    ctx: ActionContext,
    lines: list[ImportLine],
    opening_balance: Annotated[Decimal, Field(description="The balance at the statement's top")],
    closing_balance: Annotated[Decimal, Field(description="The balance at its end")],
    period_start: Annotated[date, Field(description="The statement's first day")],
    period_end: Annotated[date, Field(description="Its last day")],
    bank: AccountName = "Assets:Bank:Checking",
) -> Imported:
    """Write a bank statement: refused unless opening + lines = closing to the cent. Skips the
    lines already there, then asserts the opening balance on period_start and the closing
    balance the day after period_end."""
    lines_total = sum((x.amount if x.credit else -x.amount for x in lines), Decimal())
    total = opening_balance + lines_total
    if total != closing_balance:
        raise Refused(
            f"The statement doesn't reconcile: {opening_balance:f} + lines = {total:f}, not "
            f"{closing_balance:f} (off by {total - closing_balance:+f}). The parse is wrong: a "
            "line is missing, doubled or in the wrong column."
        )
    if period_end < period_start:
        raise Refused("period_end is before period_start.")
    outside = [x for x in lines if not period_start <= x.day <= period_end]
    if outside:
        raise Refused(
            "Lines outside the period: " + "; ".join(f"{x.day} {x.label}" for x in outside)
        )
    entries, _, options = _load()
    cur = _roots(options).currency
    named = [bank] + [x.account for x in lines if x.account]
    _opened(entries, named + [g.account for x in lines for g in x.legs])
    bad = [x for x in lines if bool(x.account) == bool(x.legs)]
    if bad:
        raise Refused(
            "Give each line either `account` or `legs`: "
            + "; ".join(f"{x.day} {x.label} {x.amount:f}" for x in bad)
            + ". Run `classify`, and ask the person about the lines it can't settle."
        )
    there = Counter(
        (t.date, p.units.number, " ".join((t.narration or "").split()))
        for t in filter_txns(entries)
        for p in t.postings
        if p.account == bank and p.units is not None
    )
    text, skipped, added = "", [], 0
    for x in sorted(lines, key=lambda x: x.day):
        narr = _narration(x.label, x.detail)
        signed = x.amount if x.credit else -x.amount
        key = (x.day, signed, narr)
        if there[key] > 0:
            there[key] -= 1
            skipped.append(f"{x.day} {narr} {signed:f}")
            continue
        legs = x.legs or [Leg(account=x.account or "")]
        text += f"\n{x.day} * {_quoted(narr)}\n  {bank}  {signed:.2f} {cur}\n"
        for g in legs:
            text += f"  {g.account}" + (
                f"  {g.amount:.2f} {cur}\n" if g.amount is not None else "\n"
            )
        added += 1
    written = {(b.date, b.account) for b in _balances(entries)}
    done = []
    checks = ((period_start, opening_balance), (period_end + timedelta(days=1), closing_balance))
    for day, amount in checks:
        if (day, bank) not in written:
            text += f"\n{day} balance {bank}  {amount:.2f} {cur}\n"
            done.append(f"{day} {amount:.2f}")
    if text:
        _append(text)
    return Imported(added=added, skipped=skipped, balances=done)


@ledger.action(roles=OWNER)
def record_balance(
    ctx: ActionContext,
    account: AccountName,
    day: Annotated[date, Field(description="The balance holds at the start of this day")],
    amount: Decimal,
    currency: Annotated[
        str | None,
        Field(description="Such as EUR or WORLD; the account's first currency by default"),
    ] = None,
    pad_from: Annotated[
        str | None,
        Field(
            description="An account that absorbs the gap through a `pad` the day before, such "
            "as Expenses:Living:Essentials for a prepaid meal card"
        ),
    ] = None,
) -> None:
    """Assert a real balance read in the bank's app or a statement, optionally padded, such as
    a meal card's: its pad on the 1st absorbs the month's top-ups and spending."""
    entries, _, options = _load()
    opened = _opened(entries, [account] + ([pad_from] if pad_from else []))
    cur = currency or (opened[account].currencies or [_roots(options).currency])[0]
    text = ""
    if pad_from:
        text += f"\n{day - timedelta(days=1)} pad {account} {pad_from}"
    text += f"\n{day} balance {account}  {amount:f} {cur}\n"
    _append(text)


class PriceLine(BaseModel):
    day: date
    commodity: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9'._-]*$", description="Such as WORLD")]
    price: Annotated[Decimal, Field(ge=0)]


class PricesAdded(BaseModel):
    added: int
    skipped: Annotated[list[str], Field(description="A price for that day was already there")]


@ledger.action(roles=OWNER)
def add_prices(ctx: ActionContext, lines: list[PriceLine]) -> PricesAdded:
    """Add `price` directives in the operating currency, such as a fund's monthly closes or a
    life insurance fund's value on 31 December. Skips a commodity's day already priced."""
    entries, _, options = _load()
    cur = _roots(options).currency
    known = {c for o in _opened(entries).values() for c in (o.currencies or [])}
    known |= {p.currency for p in _postings(entries)}
    unknown = sorted({x.commodity for x in lines} - known)
    if unknown:
        raise Refused(f"No account holds {', '.join(unknown)}. See `accounts` for commodities.")
    there = {(e.date, e.currency) for e in _parse(_book_text())[0] if isinstance(e, Price)}
    text, skipped = "", []
    for x in sorted(lines, key=lambda x: (x.day, x.commodity)):
        if (x.day, x.commodity) in there:
            skipped.append(f"{x.day} {x.commodity}")
            continue
        there.add((x.day, x.commodity))
        text += f"{x.day} price {x.commodity:<10} {x.price:f} {cur}\n"
    if text:
        _append("\n" + text)
    return PricesAdded(added=len(lines) - len(skipped), skipped=skipped)


@ledger.action(roles=OWNER)
def add_entries(
    ctx: ActionContext,
    entries: Annotated[
        str,
        Field(
            pattern=r"\S",
            description="Beancount entries on the accounts of `accounts`, flagged `*`",
        ),
    ],
) -> None:
    """Append entries in Beancount syntax, for what no other action covers: a purchase of
    shares, a yearly statement, a share sale, a new account (`open`)."""
    _append("\n" + entries.strip() + "\n")


@ledger.action(roles=OWNER, destructive=True)
def fix_entry(
    ctx: ActionContext,
    old: Annotated[str, Field(pattern=r"\S", description="Exact text, as `find_entries` gives it")],
    new: Annotated[str, Field(description="What replaces it")],
) -> None:
    """Replace one exact piece of the ledger, such as an entry's narration or amount."""
    text = _book_text()
    count = len(re.findall(f"(?={re.escape(old)})", text))
    if count == 0:
        raise Refused("`old` isn't in the ledger. Copy it from `find_entries`, spaces included.")
    if count > 1:
        raise Refused(
            f"`old` appears {count} times. Add a line before or after it so that it appears once."
        )
    Path(BOOK).write_text(text.replace(old, new))


class Recategorized(BaseModel):
    changed: Annotated[list[str], Field(description="The first line of each entry changed")]


@ledger.action(roles=OWNER)
def recategorize(
    ctx: ActionContext,
    from_account: Annotated[
        str,
        Field(
            pattern=r"\S", description="The account the postings are on now, as `accounts` lists"
        ),
    ],
    to_account: Annotated[AccountName, Field(description="Where they go, as `accounts` lists")],
    text: Annotated[
        str | None,
        Field(description="Only entries whose text contains this, in any case, such as CITY POWER"),
    ] = None,
    from_day: Annotated[date | None, Field(description="Only entries on or after this day")] = None,
    to_day: Annotated[date | None, Field(description="Only entries on or before this day")] = None,
    expected: Annotated[
        int | None,
        Field(
            ge=1,
            description="How many entries you expect to change: refused if a different number "
            "matches",
        ),
    ] = None,
) -> Recategorized:
    """Move the postings on one account to another in the matching entries, amounts and
    layout unchanged: many entries in one change, in place of `fix_entry` text by text."""
    if from_account == to_account:
        raise Refused("from_account and to_account are the same account.")
    entries = _load()[0]
    _opened(entries, [to_account])
    if from_account not in getters.get_account_open_close(entries):
        raise Refused(f"There is no account {from_account}. See `accounts`.")
    lines = _book_text().splitlines(keepends=True)
    posting = re.compile(rf"^(\s+)({re.escape(from_account)})(?=\s|$)(\s*)(.*)$", re.DOTALL)
    changed, i = [], 0
    while i < len(lines):
        if not re.match(r"^\d{4}-\d{2}-\d{2}\s", lines[i]):
            i += 1
            continue
        j = i + 1
        while j < len(lines) and lines[j][:1] in (" ", "\t") and lines[j].strip():
            j += 1
        block = lines[i:j]
        day = date.fromisoformat(lines[i][:10])
        wanted = (
            (from_day is None or day >= from_day)
            and (to_day is None or day <= to_day)
            and (text is None or text.casefold() in "".join(block).casefold())
        )
        hits = [k for k in range(i + 1, j) if posting.match(lines[k])] if wanted else []
        for k in hits:
            m = posting.match(lines[k])
            assert m is not None
            indent, _, gap, rest = m.groups()
            if rest.strip():  # keep the amount in its column
                gap = " " * max(2, len(gap) + len(from_account) - len(to_account))
            lines[k] = f"{indent}{to_account}{gap}{rest}"
        if hits:
            changed.append(lines[i].rstrip("\n"))
        i = j
    if not changed:
        raise Refused(
            f"No entry matches with a posting on {from_account}. Check the account, the text "
            "and the days with `find_entries`."
        )
    if expected is not None and len(changed) != expected:
        listed = "; ".join(changed[:10]) + (" …" if len(changed) > 10 else "")
        raise Refused(f"{len(changed)} entries match, not {expected}: {listed}")
    Path(BOOK).write_text("".join(lines))
    return Recategorized(changed=changed)


@ledger.action(roles=OWNER)
def save_rule(
    ctx: ActionContext,
    rule: Rule,
    before: Annotated[
        str | None,
        Field(description="Put it just before the rule with this pattern; at the end by default"),
    ] = None,
) -> None:
    """Add a classification rule, or replace the one with the same pattern and direction."""
    current = [r for r in _rules() if (r.pattern, r.direction) != (rule.pattern, rule.direction)]
    at = next((i for i, r in enumerate(current) if r.pattern == before), len(current))
    if before is not None and at == len(current):
        raise Refused(f"No rule has the pattern {before!r}. See `rules`.")
    current.insert(at, rule)
    _write_csv(RULES, RULE_COLUMNS, [_rule_row(r) for r in current])


@ledger.action(roles=OWNER)
def delete_rule(
    ctx: ActionContext, pattern: Annotated[str, Field(description="As listed by `rules`")]
) -> None:
    """Remove the classification rules with this pattern."""
    current = _rules()
    kept = [r for r in current if r.pattern != pattern]
    if len(kept) == len(current):
        raise Refused(f"No rule has the pattern {pattern!r}. See `rules`.")
    _write_csv(RULES, RULE_COLUMNS, [_rule_row(r) for r in kept])


@ledger.action(roles=OWNER)
def set_classes(ctx: ActionContext, classes: list[AssetClass]) -> None:
    """Replace the asset classes: how screens group asset and liability accounts, first match
    wins, and what `checkup` asks for each. Send the whole list, in order."""
    rows = [
        {"pattern": c.pattern, "class": c.name, "depth": str(c.depth), "upkeep": c.upkeep}
        for c in classes
    ]
    _write_csv(CLASSES, CLASS_COLUMNS, rows)


# The rules. --------------------------------------------------------------------------

NOT_CHECKED = "Not checked: the ledger names another file or a plugin outside Beancount."
EXCEPTIONAL_KEY = "exceptional"


@ledger.invariant("The ledger reads no other file and runs only Beancount's own plugins")
def contained(ctx: InvariantContext) -> list[str]:
    if not Path(BOOK).exists():
        return []
    return [f"{BOOK}: {found} can't be used here: remove it." for found in _outside(_book_text())]


@ledger.invariant("The ledger passes Beancount's check, balance assertions included")
def valid(ctx: InvariantContext) -> list[str]:
    try:
        errors = _load()[1]
    except Refused:
        return [NOT_CHECKED]
    return [
        f"{BOOK}:{e.source['lineno']}: {e.message}"
        + (f"\n{printer.format_entry(e.entry).rstrip()}" if e.entry else "")
        for e in errors
    ]


def _cutoff(cutoffs: dict[str, date], account: str) -> tuple[date, str]:
    parts = account.split(":")
    covering = [":".join(parts[:i]) for i in range(1, len(parts) + 1)]
    return max(((cutoffs[a], a) for a in covering if a in cutoffs), default=(date.min, account))


def _confirmed(entries: list[Any], cutoffs: dict[str, date]) -> list[Posted]:
    """The postings dated before a balance that covers their account."""
    cutoff = cache(lambda account: _cutoff(cutoffs, account)[0])  # once per account
    return [p for p in _postings(entries) if p.day < cutoff(p.account)]


@ledger.invariant("What a balance assertion confirmed never changes")
def reconciled(ctx: InvariantContext) -> list[str]:
    if not (ctx.before / BOOK).exists():
        return []
    try:
        old, new = _load(ctx.before / BOOK)[0], _load()[0]
    except Refused:
        return [NOT_CHECKED]
    undo = ctx.action == "app_revert"
    kept = Counter((b.date, b.account, b.amount) for b in _balances(new))
    gone = []
    for b in _balances(old):
        key = (b.date, b.account, b.amount)
        if kept[key]:
            kept[key] -= 1
        else:
            gone.append(b)
    problems = [
        f"The balance of {b.account} on {b.date} was removed: it was confirmed."
        for b in ([] if undo else gone)
    ]
    cutoffs: dict[str, date] = {}
    for b in _balances(old):
        if not (undo and b in gone):
            cutoffs[b.account] = max(b.date, cutoffs.get(b.account, date.min))
    was, now = Counter(_confirmed(old, cutoffs)), Counter(_confirmed(new, cutoffs))
    for p in was - now:
        cutoff, by = _cutoff(cutoffs, p.account)
        problems.append(
            f'The entry of {p.day} "{p.entry.narration}" {p.number:f} {p.currency} on '
            f"{p.account} comes before the balance of {by} on {cutoff}: it can't change. "
            f"Add a correcting entry dated {cutoff} or later."
        )
    for p in now - was:
        cutoff, by = _cutoff(cutoffs, p.account)
        problems.append(
            f'The entry of {p.day} "{p.entry.narration}" {p.number:f} {p.currency} on '
            f"{p.account} is dated before the balance of {by} on {cutoff}, which is "
            f"confirmed. Date it {cutoff} or later."
        )
    return problems


def _added_lines(ctx: InvariantContext) -> list[str]:
    before = ctx.before / BOOK
    old = Counter(before.read_text().splitlines()) if before.exists() else Counter()
    new = Counter(_book_text().splitlines())
    return list((new - old).elements())


@ledger.invariant("New entries are flagged `*`: complete, never `!`")
def complete(ctx: InvariantContext) -> list[str]:
    if not (ctx.before / BOOK).exists():
        return []  # the first data is taken as it is
    return [
        f"`{line.strip()}`: flag it `*` once the person confirmed it; ask them instead of "
        "writing an entry you're unsure of."
        for line in _added_lines(ctx)
        if re.match(r"^\d{4}-\d{2}-\d{2}\s+[^\s*\w\"]", line)
    ]


ASSET_ITEMS = re.compile(
    r":(Dividends|Interest|Gains|Sales|Fees(-[\w-]+)?|Social-Charges|Property-Tax|"
    r"Repairs(-[\w-]+)?|Loan-[\w-]+)$"
)


def _account_problems(name: str, r: Roots, assets: list[str]) -> list[str]:
    exp, inc = r.expenses + ":", r.income + ":"
    if not name.startswith((exp, inc)):
        return []
    rest = name.split(":", 1)[1]
    if rest.startswith(r.assets + ":"):
        asset = rest.split(":", 1)[1].rsplit(":", 1)[0] if rest.count(":") >= 2 else ""
        target = f"{r.assets}:{asset}"
        if not asset or not any(a == target or a.startswith(target + ":") for a in assets):
            return [
                f"{name}: `{r.assets}:<asset>:<item>` needs an asset account "
                f"{target if asset else r.assets + ':<asset>'} in the ledger."
            ]
        return []
    if ASSET_ITEMS.search(name):
        return [
            f"{name} looks linked to an asset: open it as "
            f"{name.split(':')[0]}:{r.assets}:<asset>:<item>, so that the asset's return can be "
            "asked."
        ]
    if name.startswith(exp) and not re.match(
        rf"^{re.escape(exp)}(Living:(Essentials|Extras)|Taxes:[^:]+)$", name
    ):
        return [
            f"{name}: day-to-day spending goes to {exp}Living:Essentials or :Extras, taxes to "
            f"{exp}Taxes:<tax>, and an asset's costs to {exp}{r.assets}:<asset>:<item>."
        ]
    return []


@ledger.invariant(
    "Every new income or expense account is Essentials or Extras, a tax, or an asset's own item"
)
def asset_accounts(ctx: InvariantContext) -> list[str]:
    if not (ctx.before / BOOK).exists():
        return []
    try:
        old, new = _load(ctx.before / BOOK), _load()
    except Refused:
        return [NOT_CHECKED]
    was = set(getters.get_account_open_close(old[0]))
    every = getters.get_account_open_close(new[0])
    r = _roots(new[2])
    assets = [a for a in every if a.startswith(r.assets + ":")]
    return [p for name in sorted(set(every) - was) for p in _account_problems(name, r, assets)]


@ledger.invariant("Every new income or expense account says whether it is exceptional")
def exceptional_declared(ctx: InvariantContext) -> list[str]:
    if not (ctx.before / BOOK).exists():
        return []
    try:
        old, new = _load(ctx.before / BOOK), _load()
    except Refused:
        return [NOT_CHECKED]
    was = set(getters.get_account_open_close(old[0]))
    r = _roots(new[2])
    return [
        f"{e.account}: say on its `open` whether it is one-off, with a line "
        f"`  {EXCEPTIONAL_KEY}: TRUE` (a share sale and its fees, a revaluation, a one-off bonus) "
        f"or `  {EXCEPTIONAL_KEY}: FALSE` (a salary, dividends, interest, running costs)."
        for e in new[0]
        if isinstance(e, Open)
        and e.account.startswith((r.income + ":", r.expenses + ":"))
        and e.account not in was
        and not isinstance(e.meta.get(EXCEPTIONAL_KEY), bool)
    ]


@ledger.invariant("Every classification rule reads and points at an open account")
def rules_valid(ctx: InvariantContext) -> list[str]:
    problems = []
    try:
        opened = _opened(_load()[0])
    except Refused:
        opened = {}
    for row in _read_csv(RULES):
        try:
            re.compile(row["pattern"])
        except re.error as e:
            problems.append(f"Rule {row['pattern']!r}: not a regular expression ({e}).")
        if row["account"] and opened and row["account"] not in opened:
            problems.append(f"Rule {row['pattern']!r}: {row['account']} isn't an open account.")
        if row["direction"] not in get_args(Direction) or row["when"] not in ("", *get_args(When)):
            problems.append(f"Rule {row['pattern']!r}: direction or when isn't one of the choices.")
    return problems


@ledger.invariant("Every asset class reads as a regular expression, with a known upkeep")
def classes_valid(ctx: InvariantContext) -> list[str]:
    problems = []
    for row in _read_csv(CLASSES):
        try:
            re.compile(row["pattern"])
        except re.error as e:
            problems.append(
                f"Class {row['class']}: {row['pattern']!r} isn't a regular expression ({e})."
            )
        if row["upkeep"] not in get_args(Upkeep):
            problems.append(f"Class {row['class']}: {row['upkeep']!r} isn't a known upkeep.")
    return problems


# Analysis: balance sheet, net worth, results, asset performance. --------------------


class SheetLine(BaseModel):
    account: str
    asset_class: str
    asset: str
    cost: Decimal
    value: Annotated[Decimal, Field(description="At market, with the latest price on that day")]
    start_value: Annotated[Decimal, Field(description="Its value at the end of `since`")]
    start_cost: Annotated[Decimal, Field(description="Its cost at the end of `since`")]
    flows: Annotated[
        Decimal,
        Field(
            description="Money moved in (positive) or out between the two days, at the value it "
            "moved at: transfers, deposits, purchases, sales. value - start_value - flows is what "
            "the account earned on its own"
        ),
    ]
    unvalued: Annotated[list[str], Field(description="Commodities with no price: counted at cost")]


class BalanceSheet(BaseModel):
    as_of: date
    since: date
    assets: Decimal
    liabilities: Decimal
    net: Decimal
    start_net: Decimal
    lines: list[SheetLine]


def _moved(p: Any, cur: str) -> Decimal:
    """A posting's amount at the value it moved at: its price, else its cost."""
    u = p.units
    if u.currency == cur:
        return u.number
    if p.price is not None and p.price.currency == cur and p.price.number is not None:
        return u.number * p.price.number
    if p.cost is not None and p.cost.currency == cur and p.cost.number is not None:
        return u.number * p.cost.number
    return Decimal(0)


def _flows_by_account(
    entries: list[Any], r: Roots, classes: list[AssetClass], since: date, as_of: date
) -> dict[str, Decimal]:
    """Money moved in or out of each asset and liability account after `since` up to `as_of`.
    An entry within one asset and its own Income:Assets or Expenses:Assets accounts (interest,
    fees, reinvested dividends) moves nothing: it is the asset's return."""
    sides = (r.assets + ":", r.liabilities + ":")
    own = (f"{r.income}:{r.assets}:", f"{r.expenses}:{r.assets}:")
    out: dict[str, Decimal] = defaultdict(Decimal)
    for t in filter_txns(entries):
        if not since < t.date <= as_of:
            continue
        keys, outside = set(), False
        for p in t.postings:
            if p.account.startswith(sides):
                keys.add(_classify_account(p.account, classes))
            elif p.account.startswith(own):
                parts = p.account.split(":")
                keys.add(_classify_account(":".join([r.assets, *parts[2:-1]]), classes))
            else:
                outside = True
        internal = (
            not outside and len(keys) == 1 and any(p.account.startswith(own) for p in t.postings)
        )
        if internal:
            continue
        for p in t.postings:
            if p.account.startswith(sides) and p.units is not None:
                out[p.account] += _moved(p, r.currency)
    return out


def _sheet(as_of: date, since: date) -> BalanceSheet:
    snaps = _snapshots([since, as_of])
    entries, _, options = _load()
    r, classes = _roots(options), _classes()
    flows = _flows_by_account(entries, r, classes, since, as_of)
    now, before = snaps[as_of], snaps[since]
    lines = []
    for account in sorted(set(now) | set(before)):
        h, b = now.get(account, Holding(account)), before.get(account, Holding(account))
        if not (h.value or b.value or h.cost):
            continue
        cls, asset = _classify_account(account, classes)
        lines.append(
            SheetLine(
                account=account,
                asset_class=cls,
                asset=asset,
                cost=h.cost,
                value=h.value,
                start_value=b.value,
                start_cost=b.cost,
                flows=flows.get(account, Decimal()),
                unvalued=sorted(h.unvalued),
            )
        )
    assets = sum((x.value for x in lines if x.account.startswith(r.assets + ":")), Decimal())
    debts = sum((x.value for x in lines if x.account.startswith(r.liabilities + ":")), Decimal())
    return BalanceSheet(
        as_of=as_of,
        since=since,
        assets=assets,
        liabilities=debts,
        net=assets + debts,
        start_net=sum((x.start_value for x in lines), Decimal()),
        lines=lines,
    )


@ledger.query(roles=BOTH)
def balance_sheet(
    ctx: QueryContext,
    as_of: Annotated[
        date | None, Field(description="The day, at its end; today by default")
    ] = None,
    since: Annotated[
        date | None,
        Field(description="The day to compare with; 31 December of the year before by default"),
    ] = None,
) -> BalanceSheet:
    """Every asset and liability account at market value and at cost on a day, with its value
    on an earlier day: the balance sheet. Liabilities are negative."""
    day = as_of or ctx.now.date()
    return _sheet(day, since or date(day.year - 1, 12, 31))


class WorthPoint(BaseModel):
    day: date
    net: Decimal
    assets: Decimal
    liabilities: Decimal
    by_class: Annotated[dict[str, Decimal], Field(description="Market value per asset class")]
    net_cost: Annotated[Decimal, Field(description="Net worth at cost")]
    by_class_cost: Annotated[dict[str, Decimal], Field(description="Cost per asset class")]


def _worth(days: list[date]) -> list[WorthPoint]:
    snaps = _snapshots(days)
    r, classes = _roots(_load()[2]), _classes()
    out = []
    for day in sorted(snaps):
        by: dict[str, Decimal] = defaultdict(Decimal)
        by_cost: dict[str, Decimal] = defaultdict(Decimal)
        assets = debts = cost = Decimal()
        for account, h in snaps[day].items():
            cls = _classify_account(account, classes)[0]
            by[cls] += h.value
            by_cost[cls] += h.cost
            cost += h.cost
            if account.startswith(r.liabilities + ":"):
                debts += h.value
            else:
                assets += h.value
        out.append(
            WorthPoint(
                day=day,
                net=assets + debts,
                assets=assets,
                liabilities=debts,
                by_class={k: v for k, v in sorted(by.items()) if v},
                net_cost=cost,
                by_class_cost={k: v for k, v in sorted(by_cost.items()) if v},
            )
        )
    return out


@ledger.query(roles=BOTH)
def net_worth(
    ctx: QueryContext,
    step: Literal["month", "quarter", "year"] = "quarter",
    from_year: Annotated[
        int | None, Field(ge=1990, le=2100, description="Five years back by default")
    ] = None,
) -> list[WorthPoint]:
    """Net worth at the end of each month, quarter or year, and today, at market value, with
    the value of each asset class."""
    today = ctx.now.date()
    first = from_year or today.year - 4
    months = {"month": range(1, 13), "quarter": (3, 6, 9, 12), "year": (12,)}[step]
    days = [_clip(y, m, 31) for y in range(first, today.year + 1) for m in months]
    return _worth([d for d in days if d < today] + [today])


BUCKETS = [
    "Work income",
    "Other income",
    "Asset income",
    "One-off income",
    "Living",
    "Taxes",
    "Asset costs",
    "Other spending",
    "One-off costs",
]
# What a month runs on: one-off income and costs are kept apart.
INCOME_BUCKETS = ["Work income", "Other income", "Asset income"]
EXPENSE_BUCKETS = ["Living", "Taxes", "Asset costs", "Other spending"]
# Work income: an employer's salary or bonus, such as Income:Acme:Salary, and unemployment
# benefit. Every other income outside the assets (allowances, refunds, gifts) is other income.
WORK = re.compile(r":(Salary|Bonus|Unemployment)(:|$)")
LIVING = ("Essentials", "Extras")


def _exceptional(entries: list[Any]) -> frozenset[str]:
    """The income and expense accounts whose `open` says `exceptional: TRUE`."""
    return frozenset(
        e.account for e in entries if isinstance(e, Open) and e.meta.get(EXCEPTIONAL_KEY) is True
    )


def _bucket(account: str, r: Roots, exceptional: frozenset[str] = frozenset()) -> str | None:
    if account in exceptional:
        if account.startswith(r.income + ":"):
            return "One-off income"
        if account.startswith(r.expenses + ":"):
            return "One-off costs"
    if account.startswith(f"{r.income}:{r.assets}:"):
        return "Asset income"
    if account.startswith(r.income + ":"):
        return "Work income" if WORK.search(account) else "Other income"
    if account.startswith(f"{r.expenses}:Living:"):
        return "Living"
    if account.startswith(f"{r.expenses}:Taxes:"):
        return "Taxes"
    if account.startswith(f"{r.expenses}:{r.assets}:"):
        return "Asset costs"
    if account.startswith(r.expenses + ":"):
        return "Other spending"
    return None


def _flows(entries: list[Any], r: Roots, start: date, end: date) -> dict[str, Decimal]:
    """Income (positive) and expenses (positive) per account between two days, in the
    operating currency only."""
    out: dict[str, Decimal] = defaultdict(Decimal)
    for p in _postings(entries):
        if start <= p.day <= end and p.currency == r.currency and _bucket(p.account, r):
            out[p.account] += -p.number if p.account.startswith(r.income + ":") else p.number
    return out


class YearResult(BaseModel):
    year: int
    start: date
    end: date
    months: Annotated[Decimal, Field(description="Months in the window, for monthly averages")]
    income: Annotated[
        Decimal,
        Field(
            description="Recurring income: work, other income (allowances, refunds, gifts) and "
            "the assets' dividends and interest, one-off income left out"
        ),
    ]
    exceptional: Annotated[
        Decimal,
        Field(
            description="One-off income (share sales, revaluations, a one-off bonus) minus "
            "one-off costs (sale fees): the accounts opened with `exceptional: TRUE`"
        ),
    ]
    expenses: Annotated[Decimal, Field(description="Recurring spending, one-off costs left out")]
    savings: Annotated[Decimal, Field(description="income - expenses: recurring only")]
    buckets: Annotated[
        dict[str, Decimal],
        Field(
            description="Work income (salaries, bonuses, unemployment), Other income, Asset "
            "income, One-off income, Living, Taxes, Asset costs, Other spending, One-off costs"
        ),
    ]
    accounts: Annotated[
        dict[str, Decimal], Field(description="Per income and expense account, income positive")
    ]


@ledger.query(roles=BOTH)
def results(
    ctx: QueryContext,
    period: Annotated[Period, Field(description=PERIOD_DESC)] = "ytd",
    from_year: Annotated[
        int | None, Field(ge=1990, le=2100, description="Five years back by default")
    ] = None,
) -> list[YearResult]:
    """Income, spending and savings in the same window of each year (year to date, a quarter
    or the whole year), living costs apart from taxes and asset costs, so years compare.
    One-off income, such as a share sale, is kept apart from recurring income."""
    entries, _, options = _load()
    r, today = _roots(options), ctx.now.date()
    out = []
    for year in range(from_year or today.year - 4, today.year + 1):
        window = _window(year, period, today)
        if window is not None:
            out.append(_year_result(entries, r, year, *window))
    return out


def _year_result(entries: list[Any], r: Roots, year: int, start: date, end: date) -> YearResult:
    flows, exceptional = _flows(entries, r, start, end), _exceptional(entries)
    buckets = {b: Decimal() for b in BUCKETS}
    for account, n in flows.items():
        buckets[_bucket(account, r, exceptional) or "Other spending"] += n
    income = sum((buckets[b] for b in INCOME_BUCKETS), Decimal())
    spent = sum((buckets[b] for b in EXPENSE_BUCKETS), Decimal())
    days = (end - start).days + 1
    return YearResult(
        year=year,
        start=start,
        end=end,
        # A tenth of a month at least, so that a window of a day or two still divides.
        months=max((Decimal(days) / Decimal("30.4375")).quantize(Decimal("0.1")), Decimal("0.1")),
        income=income,
        exceptional=buckets["One-off income"] - buckets["One-off costs"],
        expenses=spent,
        savings=income - spent,
        buckets=buckets,
        accounts={a: n for a, n in sorted(flows.items()) if n},
    )


class MonthResult(BaseModel):
    month: Annotated[str, Field(description="YYYY-MM")]
    income: Annotated[Decimal, Field(description="Recurring income, one-off income left out")]
    exceptional: Annotated[Decimal, Field(description="One-off income minus one-off costs")]
    expenses: Annotated[Decimal, Field(description="Recurring spending, one-off costs left out")]
    savings: Annotated[Decimal, Field(description="income - expenses")]
    buckets: dict[str, Decimal]


@ledger.query(roles=BOTH)
def monthly(ctx: QueryContext, year: Year = None) -> list[MonthResult]:
    """Each month of a year, up to today: recurring income against spending, so that a month
    spent above what came in shows. One-off income, such as a share sale, is apart."""
    entries, _, options = _load()
    r, today = _roots(options), ctx.now.date()
    year = year or today.year
    exceptional = _exceptional(entries)
    sums: dict[int, dict[str, Decimal]] = defaultdict(lambda: {b: Decimal() for b in BUCKETS})
    for p in _postings(entries):
        if p.day.year == year and p.day <= today and p.currency == r.currency:
            b = _bucket(p.account, r, exceptional)
            if b:
                sums[p.day.month][b] += (
                    -p.number if p.account.startswith(r.income + ":") else p.number
                )
    last = today.month if year == today.year else 12
    out = []
    for m in range(1, last + 1):
        b = sums[m]
        income = sum((b[k] for k in INCOME_BUCKETS), Decimal())
        spent = sum((b[k] for k in EXPENSE_BUCKETS), Decimal())
        out.append(
            MonthResult(
                month=f"{year}-{m:02d}",
                income=income,
                exceptional=b["One-off income"] - b["One-off costs"],
                expenses=spent,
                savings=income - spent,
                buckets=dict(b),
            )
        )
    return out


class AssetResult(BaseModel):
    asset_class: str
    asset: str
    start_value: Decimal
    value: Decimal
    cost: Decimal
    income: Annotated[
        Decimal,
        Field(
            description="Dividends, interest, gains and sales booked to its Income:Assets accounts"
        ),
    ]
    costs: Annotated[Decimal, Field(description="Booked to its Expenses:Assets accounts")]
    latent_change: Annotated[Decimal, Field(description="The change of value minus cost")]
    result: Annotated[
        Decimal,
        Field(
            description="income - costs + latent_change: its return in the window, money paid "
            "in left out"
        ),
    ]
    unvalued: list[str]


@ledger.query(roles=BOTH)
def performance(
    ctx: QueryContext,
    period: Annotated[Period, Field(description=PERIOD_DESC)] = "ytd",
    year: Year = None,
) -> list[AssetResult]:
    """Each asset's return in a window: its own income and costs, plus the change of its
    unrealised gain. What is booked at cost, such as a home, shows only its costs until it is
    revalued."""
    entries, _, options = _load()
    r, today, classes = _roots(options), ctx.now.date(), _classes()
    window = _window(year or today.year, period, today)
    if window is None:
        raise Refused("That period hasn't started yet.")
    start, end = window
    snaps = _snapshots([start - timedelta(days=1), end])
    rows: dict[tuple[str, str], AssetResult] = {}

    def row(account: str) -> AssetResult:
        key = _classify_account(account, classes)
        zero = Decimal()
        return rows.setdefault(
            key,
            AssetResult(
                asset_class=key[0],
                asset=key[1],
                start_value=zero,
                value=zero,
                cost=zero,
                income=zero,
                costs=zero,
                latent_change=zero,
                result=zero,
                unvalued=[],
            ),
        )

    for which, day in (("start", start - timedelta(days=1)), ("end", end)):
        for account, h in snaps[day].items():
            if not account.startswith(r.assets + ":"):
                continue
            a = row(account)
            if which == "start":
                a.start_value += h.value
                a.latent_change -= h.value - h.cost
            else:
                a.value += h.value
                a.cost += h.cost
                a.latent_change += h.value - h.cost
                a.unvalued = sorted(set(a.unvalued) | h.unvalued)
    for account, n in _flows(entries, r, start, end).items():
        parts = account.split(":")
        if len(parts) < 4 or parts[1] != r.assets:
            continue
        a = row(":".join([r.assets, *parts[2:-1]]))
        if account.startswith(r.income + ":"):
            a.income += n
        else:
            a.costs += n
    for a in rows.values():
        a.result = a.income - a.costs + a.latent_change
    return sorted(
        (a for a in rows.values() if a.value or a.start_value or a.income or a.costs),
        key=lambda a: (a.asset_class, -a.value),
    )


class Task(BaseModel):
    task: str
    state: Literal["todo", "ok", "info"]
    detail: str


def _monthly_accounts(entries: list[Any]) -> dict[str, date]:
    """Accounts whose last three balance assertions are each at most 40 days apart, with the
    last one's day: they are checked against a monthly statement."""
    by: dict[str, list[date]] = defaultdict(list)
    for b in _balances(entries):
        by[b.account].append(b.date)
    out = {}
    for account, days in by.items():
        days = sorted(days)[-3:]
        if len(days) == 3 and all((b - a).days <= 40 for a, b in itertools.pairwise(days)):
            out[account] = days[-1]
    return out


@ledger.query(roles=BOTH)
def checkup(ctx: QueryContext) -> list[Task]:
    """What the ledger needs now: the monthly statements, then what each asset class's upkeep
    asks (monthly prices, yearly values, a revaluation, the rate a savings account served),
    then the entries filed against a rule or whose filing changed."""
    text = _book_text()
    entries, _, options = _load_text(text)
    r, today, classes = _roots(options), ctx.now.date(), _classes()
    tasks = []
    open_now = _opened(entries)
    for account, last in sorted(_monthly_accounts(entries).items()):
        if account not in open_now or (today - last).days > 365:
            continue  # closed, or no longer followed monthly
        late = (today - last).days > 40
        detail = f"Last checked balance on {last}" + (
            ": import the next statement." if late else "."
        )
        tasks.append(
            Task(task=f"Statement of {account}", state="todo" if late else "ok", detail=detail)
        )
    invs: dict[str, Inventory] = defaultdict(Inventory)
    for t in filter_txns(entries):
        for p in t.postings:
            if p.account.startswith(r.assets + ":"):
                invs[p.account].add_position(p)
    held: dict[str, set[str]] = defaultdict(set)  # commodity -> upkeeps of the classes holding it
    for account, inv in invs.items():
        upkeep = _upkeep(account, classes)
        for pos in inv:
            if pos.units.currency != r.currency and pos.units.number:
                held[pos.units.currency].add(upkeep)
    priced: dict[str, date] = {}
    for e in _parse(text)[0]:
        if isinstance(e, Price):
            priced[e.currency] = max(e.date, priced.get(e.currency, date.min))
    yearly_due = date(today.year - 1 if today.month >= 3 else today.year - 2, 12, 31)
    for commodity, upkeeps in sorted(held.items()):
        last = priced.get(commodity)
        since = f"Last price on {last}" if last else "No price yet"
        if "monthly-prices" in upkeeps:
            ok = last is not None and (today - last).days <= 31
            detail = since + ("." if ok else ": add the month's closing prices with `add_prices`.")
            tasks.append(
                Task(task=f"Price of {commodity}", state="ok" if ok else "todo", detail=detail)
            )
        elif "yearly-prices" in upkeeps:
            ok = last is not None and last >= yearly_due
            detail = f"{since}; one on {yearly_due} or later is due."
            tasks.append(
                Task(task=f"Value of {commodity}", state="ok" if ok else "todo", detail=detail)
            )
    gains: dict[str, date] = defaultdict(lambda: date.min)
    for p in _postings(entries):
        if p.account.startswith(f"{r.income}:{r.assets}:") and p.account.endswith(":Gains"):
            asset = ":".join(p.account.split(":")[2:-1])
            gains[asset] = max(gains[asset], p.day)
    at_cost: dict[str, set[str]] = defaultdict(set)  # asset -> commodities
    for a, inv in invs.items():
        if _upkeep(a, classes) == "revalue" and not inv.is_empty():
            at_cost[_classify_account(a, classes)[1]] |= {pos.units.currency for pos in inv}
    for asset, commodities in sorted(at_cost.items()):
        last = max([gains[asset]] + [priced.get(c, date.min) for c in commodities])
        ok = last > date.min and (today - last).days <= 400
        detail = (
            f"Last revalued on {last}." if last > date.min else "Booked at cost: never revalued."
        ) + ("" if ok else " Estimate its market value with the person and book it.")
        tasks.append(
            Task(task=f"Revaluation of {asset}", state="ok" if ok else "todo", detail=detail)
        )
    year = today.year - 1
    savings = [a for a in invs if _upkeep(a, classes) == "interest"]
    flows = _flows(entries, r, date(year, 1, 1), date(year, 12, 31))
    month_ends = [_clip(year, m, 31) for m in range(1, 13)]
    snaps = _snapshots(month_ends)
    for account in sorted(savings):
        interest = sum(
            (n for a, n in flows.items() if a.startswith(f"{r.income}:{account}:")), Decimal()
        )
        average = (
            sum((snaps[d].get(account, Holding(account)).value for d in month_ends), Decimal()) / 12
        )
        if average > 0:
            detail = (
                f"{interest / average:.2%} ({_money(interest, True)} of interest on an average "
                f"balance of {_money(average)}): compare it with the advertised rate."
            )
            tasks.append(
                Task(task=f"Rate served by {account} in {year}", state="info", detail=detail)
            )
    return tasks + _anomalies(entries, r, today)


def _anomalies(entries: list[Any], r: Roots, today: date) -> list[Task]:
    """Statement entries of the last twelve months that don't follow an `always` rule, and
    merchants whose account changed: a loan payment left in Living, a subscription refiled."""
    banks = set(_monthly_accounts(entries))
    since = today - timedelta(days=365)
    always = [x for x in _rules() if x.when == "always"]
    broken: dict[str, list[date]] = defaultdict(list)
    history: dict[tuple[str, str], list[tuple[date, frozenset[str]]]] = defaultdict(list)
    for t in filter_txns(entries):
        if t.flag == "P":
            continue
        bank = next((p for p in t.postings if p.account in banks), None)
        number = bank.units.number if bank is not None and bank.units is not None else None
        if bank is None or number is None:
            continue
        narr = t.narration or ""
        others = frozenset(p.account for p in t.postings if p.account != bank.account)
        key = _merchant(narr)
        if key:
            history[bank.account, key].append((t.date, others))
        if t.date < since:
            continue
        line = StatementLine(day=t.date, label=narr, amount=abs(number), credit=number > 0)
        rule = next((x for x in always if _matches(x, narr, line)), None)
        if rule is None:
            continue
        if rule.account and rule.account not in others:
            broken[f"“{rule.pattern}” → {rule.account}"].append(t.date)
        elif (
            not rule.account
            and len(t.postings) <= 2
            and any(a.startswith(f"{r.expenses}:Living:") for a in others)
        ):
            broken[f"“{rule.pattern}” to split ({rule.note})"].append(t.date)
    tasks = [
        Task(
            task=f"Rule not followed: {what}",
            state="todo",
            detail=f"{len(days)} {'entries' if len(days) > 1 else 'entry'} from {min(days)} to "
            f"{max(days)} filed otherwise: fix them with `recategorize`, or change the rule.",
        )
        for what, days in sorted(broken.items())
    ]
    for (_, key), seen in sorted(history.items()):
        if len(seen) < 4:
            continue
        # A lasting change: the last two entries agree with each other, not with the two before.
        (_, a1), (d2, a2), (d3, a3), (_, a4) = seen[-4:]
        if a3 == a4 and a1 == a2 and a2 != a3 and d3 >= since:
            tasks.append(
                Task(
                    task=f"Filing changed: {key.split(':', 1)[1]}",
                    state="todo",
                    detail=f"{_unrooted(a2)} up to {d2}, then {_unrooted(a3)} since {d3}: "
                    "check it was meant.",
                )
            )
    return tasks


def _unrooted(accounts: frozenset[str]) -> str:
    """Accounts without their root, such as "Living:Extras, Assets:Home:Fees"."""
    return ", ".join(sorted(x.split(":", 1)[1] for x in accounts))


# Screens. ----------------------------------------------------------------------------

PERIOD_LABELS = [("q1", "Q1"), ("q2", "Q2"), ("q3", "Q3"), ("q4", "Q4"), ("year", "Year")]
# Whole euros, for charts and grids.
EUR = "currency:EUR:1.0-0"


def _money(n: Decimal | float, cents: bool = False) -> str:
    """An amount as people read it: €1,234, or −€1,234.50 with cents."""
    n = Decimal(str(n))
    text = f"{abs(n):,.2f}" if cents else f"{abs(n):,.0f}"
    return ("−" if n < 0 and text.strip("0.,") else "") + "€" + text


def _short(n: Decimal | float, sign: bool = False) -> str:
    """A headline amount short enough for a third of a phone: €1.9M, €96k, €7.4k, €742."""
    n = Decimal(str(n))
    s = "+" if sign and n > 0 else ("−" if n < 0 else "")
    a = abs(n)
    if a >= 1_000_000:
        return f"{s}€{a / 1_000_000:.1f}M"
    if a >= 10_000:
        return f"{s}€{a / 1000:.0f}k"
    if a >= 1_000:
        return f"{s}€{a / 1000:.1f}k"
    return f"{s}€{a:.0f}"


def _full(n: Decimal | float, sign: bool = False) -> str:
    """A headline amount where there is room: €1,295,870, with a sign when asked."""
    n = Decimal(str(n))
    return ("+" if sign and n > 0 else "") + _money(n)


def _pct(n: Decimal | None, sign: bool = True) -> str:
    return "" if n is None else (f"{n:+.1%}" if sign else f"{n:.0%}")


def _day(d: date) -> str:
    return f"{d.day} {d:%b}"


def _date(d: date) -> str:
    return f"{d.day} {d:%b %Y}"


def _span(start: date, end: date) -> str:
    """A window as a subtitle says it: Year 2025, 1 Jan to 26 Nov 2026."""
    if (start, end) == (date(start.year, 1, 1), date(start.year, 12, 31)):
        return f"Year {start.year}"
    return f"{_day(start) if start.year == end.year else _date(start)} to {_date(end)}"


def _ratio(a: Decimal, b: Decimal) -> Decimal | None:
    return a / abs(b) if b else None


def _tabs(
    choices: list[tuple[Any, str]], current: Any, key: str, keep: dict[str, Any]
) -> list[dict[str, Any]]:
    """Link tabs: each lays the view out again with its own value of `key`."""
    return [
        {"args": {**keep, key: value}, "label": label, "active": value == current}
        for value, label in choices
    ]


def _periods(today: date) -> list[tuple[str, str]]:
    """The windows of a year, the one to date named by today's day."""
    return [("ytd", f"To {_day(today)}"), *PERIOD_LABELS]


def _first_year(entries: list[Any]) -> int | None:
    return next((t.date.year for t in filter_txns(entries)), None)


def _shown_year(year: int | None, first: int, today: date) -> int:
    """The year a screen shows: this year by default, else one with entries, or a refusal."""
    if year is None:
        return today.year
    if not first <= year <= today.year:
        raise Refused(f"The ledger has entries from {first} to {today.year} only, not {year}.")
    return year


def _living(y: YearResult, r: Roots, name: str) -> Decimal:
    return y.accounts.get(f"{r.expenses}:Living:{name}", Decimal())


PARTS = """
{% macro header(title, subtitle="", years=None, year=None) %}
<div class="layout-row layout-between layout-top layout-nowrap layout-gap-3">
  <header>
    <h2>{{ title }}</h2>
    {% if subtitle %}<p class="text-base-content/60">{{ subtitle }}</p>{% endif %}
  </header>
  {% if years %}
  <form data-view>
    <select class="select select-sm" name="year" aria-label="Year">
      {% for y in years %}
      <option value="{{ y }}" {% if y == year %}selected{% endif %}>{{ y }}</option>
      {% endfor %}
    </select>
  </form>
  {% endif %}
</div>
{% endmacro %}
{% macro choice(items, label) %}
<div class="layout-scroll-x">
  <nav role="tablist" class="tabs tabs-box tabs-sm layout-nowrap" aria-label="{{ label }}">
    {% for t in items %}
    <a role="tab" class="tab {{ 'tab-active' if t.active }}" data-view
       data-args='{{ t.args | tojson }}'>{{ t.label }}</a>
    {% endfor %}
  </nav>
</div>
{% endmacro %}
{% macro stat(title, full, short, desc="", tone="") %}
<div class="stat">
  <div class="stat-title">{{ title }}</div>
  <div class="stat-value {{ tone }}">
    <span class="layout-only-wide">{{ full }}</span>
    <span class="layout-only-narrow">{{ short }}</span>
  </div>
  {% if desc %}<div class="stat-desc">{{ desc }}</div>{% endif %}
</div>
{% endmacro %}
"""

EMPTY = """
<header><h2>{{ title }}</h2></header>
<div role="status" class="alert alert-soft">
  <span data-icon="inbox"></span><span>The ledger has no entries yet.</span>
</div>
"""

BUDGET = (
    PARTS
    + """
{{ header("Budget", span, years, year) }}
<div class="stats card-border">
  {{ stat("Income", full(cur.income), short(cur.income),
          money(cur.income / cur.months) ~ " a month") }}
  {{ stat("Spending", full(cur.expenses), short(cur.expenses),
          money(cur.expenses / cur.months) ~ " a month") }}
  {{ stat("Saved", full(cur.savings, True), short(cur.savings, True),
          (pct(cur.savings / cur.income, False) ~ " of income") if cur.income > 0 else "",
          tone='text-success' if cur.savings >= 0 else 'text-error') }}
</div>
<div role="tablist" class="tabs tabs-border">
  <input type="radio" name="budget_section" class="tab" aria-label="Months" checked>
  <div class="tab-content">
    <div class="layout-stack layout-gap-4">
      <div data-chart='{{ chart | tojson }}'></div>
      <div data-grid='{{ month_grid | tojson }}' aria-label="Months of {{ year }}"></div>
    </div>
  </div>
  <input type="radio" name="budget_section" class="tab" aria-label="Split">
  <div class="tab-content">
    <div class="layout-stack layout-gap-4">
      {% if donut.labels %}
      <div data-chart='{{ donut | tojson }}'></div>
      {% else %}
      <p class="text-base-content/60">No spending, {{ span }}.</p>
      {% endif %}
      <div data-grid='{{ compare | tojson }}'
           aria-label="Income and spending against the year before"></div>
    </div>
  </div>
  <input type="radio" name="budget_section" class="tab" aria-label="Trend">
  <div class="tab-content">
    <div class="layout-stack layout-gap-4">
      {{ choice(period_tabs, "Period compared") }}
      <div data-chart='{{ trend | tojson }}'></div>
      <div data-grid='{{ trend_grid | tojson }}' aria-label="Monthly averages, year by year"></div>
    </div>
  </div>
</div>
"""
)

BUDGET_FIGURES = [
    ("income", "Income"),
    ("spending", "Spending"),
    ("essentials", "Essentials"),
    ("extras", "Extras"),
    ("taxes", "Taxes"),
    ("assets", "Asset costs"),
]


def _budget_columns(first: dict[str, Any], totals: bool) -> list[dict[str, Any]]:
    """The Months and Trend grids' columns: what is saved, then income and spending."""
    total = {"total": "sum"} if totals else {}
    saved = {"field": "saved", "header": "Saved", "format": EUR, "tone": "signed", **total}
    return [first, saved] + [
        {"field": f, "header": h, "format": EUR, **total} for f, h in BUDGET_FIGURES
    ]


def _compare_grid(
    cur: YearResult, prev: YearResult, r: Roots, cur_label: str, prev_label: str
) -> dict[str, Any]:
    """The Split grid: income, and spending as negatives, against the same window a year
    before, so that its footer is what is saved."""
    rows: list[dict[str, Any]] = []

    def row(
        section: str, label: str, a: Decimal | None, b: Decimal | None, indent: int = 0
    ) -> None:
        line: dict[str, Any] = {
            "section": section,
            "item": label,
            "cur": a,
            "prev": b,
            "change": None if a is None or b is None else a - b,
            "pct": None if a is None or b is None else _ratio(a - b, b),
        }
        if indent:
            line["indent"] = indent
        rows.append(line)

    for b in INCOME_BUCKETS:
        if cur.buckets[b] or prev.buckets[b]:
            row("Recurring income", b, cur.buckets[b], prev.buckets[b])
    spent = "Recurring spending"
    living = [(n, _living(cur, r, n), _living(prev, r, n)) for n in LIVING]
    living.append(
        (
            "Other living",
            cur.buckets["Living"] - living[0][1] - living[1][1],
            prev.buckets["Living"] - living[0][2] - living[1][2],
        )
    )
    living = [x for x in living if x[1] or x[2]]
    if living:  # Living as a heading over its parts, so that it counts once
        row(spent, "Living", None, None)
    for name, a, b in living:
        row(spent, name, 0 - a, 0 - b, indent=1)
    for b in EXPENSE_BUCKETS[1:]:
        if cur.buckets[b] or prev.buckets[b]:
            row(spent, b, 0 - cur.buckets[b], 0 - prev.buckets[b])
    return {
        "group_by": "section",
        "tools": ["sort"],
        "density": "compact",
        "columns": [
            {"field": "item", "header": "Item"},
            {"field": "cur", "header": cur_label, "format": EUR, "total": "sum"},
            {"field": "prev", "header": prev_label, "format": EUR, "total": "sum"},
            {
                "field": "change",
                "header": "Change",
                "format": EUR,
                "total": "sum",
                "tone": "signed",
            },
            {"field": "pct", "header": "%", "format": "percent:1.0-0"},
        ],
        "rows": rows,
    }


@ledger.view(roles=BOTH)
def budget(ctx: QueryContext, year: Year = None, period: Period = "ytd") -> Screen:
    """Does the household spend less than it earns? A year month by month (income against
    spending by kind, and what is saved), its spending against the year before, and the
    years up to it compared."""
    today = ctx.now.date()
    entries, _, options = _load()
    first = _first_year(entries)
    if first is None:
        return Screen(EMPTY, title="Budget")
    r = _roots(options)
    year = _shown_year(year, first, today)
    if year == today.year:
        start, end = date(year, 1, 1), today
        prev_end = _clip(year - 1, today.month, today.day)
        cur_label, prev_label = f"{year} to {_day(today)}", f"{year - 1} to {_day(prev_end)}"
    else:
        start, end, prev_end = date(year, 1, 1), date(year, 12, 31), date(year - 1, 12, 31)
        cur_label, prev_label = str(year), str(year - 1)
    cur = _year_result(entries, r, year, start, end)
    prev = _year_result(entries, r, year - 1, date(year - 1, 1, 1), prev_end)
    months = [m.model_dump() for m in monthly(ctx, year=year)]
    # Essentials and Extras month by month.
    exceptional = _exceptional(entries)
    living = {name: [Decimal()] * len(months) for name in LIVING}
    for p in _postings(entries):
        if (
            p.day.year != year
            or p.day > today
            or p.currency != r.currency
            or p.account in exceptional
        ):
            continue
        for name in LIVING:
            if p.account == f"{r.expenses}:Living:{name}" and p.day.month <= len(months):
                living[name][p.day.month - 1] += p.number
    for i, m in enumerate(months):
        m["essentials"], m["extras"] = living["Essentials"][i], living["Extras"][i]
    other_living = [m["buckets"]["Living"] - m["essentials"] - m["extras"] for m in months]
    spending = [
        ("Essentials", living["Essentials"]),
        ("Extras", living["Extras"]),
        ("Other living", other_living),
    ] + [(b, [m["buckets"][b] for m in months]) for b in EXPENSE_BUCKETS[1:]]
    series: list[dict[str, Any]] = [
        {"name": "Income", "values": [float(m["income"]) for m in months], "tone": "success"}
    ]
    series += [
        {"name": name, "values": [-float(v) for v in values]}
        for name, values in spending
        if any(values)
    ]
    series.append(
        {
            "name": "Saved",
            "kind": "line",
            "tone": "neutral",
            "values": [float(m["savings"]) for m in months],
        }
    )
    # The months, latest first.
    has_exceptional = any(m["exceptional"] for m in months)
    month_columns = _budget_columns(
        {"field": "month", "header": "Month", "format": "month:long"}, True
    )
    if has_exceptional:
        month_columns.append(
            {
                "field": "oneoff",
                "header": "One-off",
                "format": EUR,
                "total": "sum",
                "tone": "signed",
            }
        )
    month_rows = []
    for m in reversed(months):
        line = {
            "month": date(year, int(m["month"][5:]), 1),
            "saved": m["savings"],
            "income": m["income"],
            "spending": m["expenses"],
            "essentials": m["essentials"],
            "extras": m["extras"],
            "taxes": m["buckets"]["Taxes"],
            "assets": m["buckets"]["Asset costs"],
        }
        if has_exceptional:
            line["oneoff"] = m["exceptional"] or None
        month_rows.append(line)
    # Spending by kind, for the donut.
    essentials, extras = _living(cur, r, "Essentials"), _living(cur, r, "Extras")
    parts = [
        ("Essentials", essentials),
        ("Extras", extras),
        ("Other living", cur.buckets["Living"] - essentials - extras),
    ]
    parts += [(b, cur.buckets[b]) for b in EXPENSE_BUCKETS[1:]]
    parts = [(name, v) for name, v in parts if v > 0]
    # Up to five years before the one shown, those with entries, a month on average.
    years = [
        _year_result(entries, r, y, *w)
        for y in range(max(year - 4, first), year + 1)
        if (w := _window(y, period, today))
    ]
    year_rows: list[dict[str, Any]] = [
        {
            "year": str(y.year),
            "saved": y.savings / y.months,
            "income": y.income / y.months,
            "spending": y.expenses / y.months,
            "essentials": _living(y, r, "Essentials") / y.months,
            "extras": _living(y, r, "Extras") / y.months,
            "taxes": y.buckets["Taxes"] / y.months,
            "assets": y.buckets["Asset costs"] / y.months,
            "rate": _ratio(y.savings, y.income) if y.income > 0 else None,
        }
        for y in years
    ]
    trend_columns = [
        *_budget_columns({"field": "year", "header": "A month"}, False),
        {"field": "rate", "header": "Rate", "format": "percent:1.0-0"},
    ]
    keep = {"year": year, "period": period}
    return Screen(
        BUDGET,
        args=keep,
        span=_span(start, end),
        year=year,
        years=list(range(today.year, first - 1, -1)),
        cur=cur,
        period_tabs=_tabs(_periods(today), period, "period", keep),
        chart={
            "kind": "stacked-bar",
            "labels": [m["month"][5:].lstrip("0") for m in months],
            "x": {"format": "month:short"},
            "series": series,
            "y": {"format": EUR},
        },
        month_grid={
            "tools": ["sort"],
            "density": "compact",
            "columns": month_columns,
            "rows": month_rows,
        },
        donut={
            "kind": "donut",
            "labels": [name for name, _ in parts],
            "series": [{"name": "Spending", "values": [float(v) for _, v in parts]}],
            "format": EUR,
        },
        compare=_compare_grid(cur, prev, r, cur_label, prev_label),
        trend={
            "kind": "bar",
            "labels": [y["year"] for y in year_rows],
            "series": [
                {
                    "name": "Income",
                    "values": [float(y["income"]) for y in year_rows],
                    "tone": "success",
                },
                {
                    "name": "Spending",
                    "values": [float(y["spending"]) for y in year_rows],
                    "tone": "error",
                },
                {
                    "name": "Saved",
                    "kind": "line",
                    "tone": "neutral",
                    "values": [float(y["saved"]) for y in year_rows],
                },
                {
                    "name": "Savings rate",
                    "kind": "line",
                    "axis": "y2",
                    "values": [float(y["rate"] or 0) for y in year_rows],
                },
            ],
            "y": {"format": EUR},
            "y2": {"title": "Savings rate", "format": "percent:1.0-0"},
        },
        trend_grid={
            "tools": ["sort"],
            "density": "compact",
            "columns": trend_columns,
            "rows": list(reversed(year_rows)),
        },
        money=_money,
        full=_full,
        short=_short,
        pct=_pct,
    )


TRANSACTIONS = (
    PARTS
    + """
{{ header("Transactions", "", years, year) }}
{% if grid.rows %}
<div data-grid='{{ grid | tojson }}' aria-label="Transactions of {{ year }}"></div>
{% else %}
<p class="text-base-content/60">No income or spending in {{ year }}.</p>
{% endif %}
"""
)


@ledger.view(roles=BOTH)
def transactions(ctx: QueryContext, year: Year = None) -> Screen:
    """Every income and expense posting of a year, latest first, in a grid to sort, search
    and filter by category, account or amount, with a total. Transfers between the household's
    own accounts are left out."""
    today = ctx.now.date()
    entries, _, options = _load()
    first = _first_year(entries)
    if first is None:
        return Screen(EMPTY, title="Transactions")
    r = _roots(options)
    year = _shown_year(year, first, today)
    exceptional = _exceptional(entries)
    sides = (r.assets + ":", r.liabilities + ":")
    rows: list[dict[str, Any]] = []
    for t in filter_txns(entries):
        if t.date.year != year or t.date > today:
            continue
        via = next(
            (p.account.split(":", 1)[1] for p in t.postings if p.account.startswith(sides)), ""
        )
        for p in t.postings:
            if p.units is None or p.units.number is None or p.units.currency != r.currency:
                continue
            category = _bucket(p.account, r, exceptional)
            if category is None:
                continue
            if category == "Living":  # Essentials or Extras, the split people filter on
                category = p.account.rsplit(":", 1)[-1]
            label = (
                f"Balance adjustment of {via}"
                if t.flag == "P"
                else " ".join((t.narration or t.payee or "").split())
            )
            rows.append(
                {
                    "day": t.date,
                    "amount": -p.units.number,
                    "category": category,
                    "description": label,
                    "account": p.account.split(":", 1)[1],
                    "via": via,
                }
            )
    rows.reverse()
    grid = {
        "columns": [
            {"field": "day", "header": "Date", "format": "date"},
            {
                "field": "amount",
                "header": "Amount",
                "format": "currency:EUR",
                "total": "sum",
                "tone": "signed",
            },
            {"field": "category", "header": "Category"},
            {"field": "description", "header": "Description"},
            {"field": "account", "header": "Account"},
            {"field": "via", "header": "Paid with"},
        ],
        "rows": rows[:5000],
    }
    years = list(range(today.year, first - 1, -1))
    return Screen(TRANSACTIONS, args={"year": year}, year=year, grid=grid, years=years)


WEALTH = (
    PARTS
    + """
{{ header("Net worth", "On " ~ date(today)) }}
<div class="stats card-border">
  {{ stat("Net worth", full(now.net), short(now.net), "At cost " ~ short(now.net_cost)) }}
  {{ stat("Since January", full(ytd, True), short(ytd, True), pct(ytd_pct),
          tone='text-success' if ytd >= 0 else 'text-error') }}
  {{ stat("Over a year", full(yoy, True), short(yoy, True), pct(yoy_pct),
          tone='text-success' if yoy >= 0 else 'text-error') }}
</div>
<div class="layout-row layout-between layout-gap-2">
  {{ choice(span_tabs, "Span") }}
  {{ choice(basis_tabs, "Valued") }}
</div>
<div data-chart='{{ chart | tojson }}'></div>
<section class="layout-stack layout-gap-3">
  <h3>What it is made of</h3>
  <div data-chart='{{ donut | tojson }}'></div>
  <div data-grid='{{ composition | tojson }}' aria-label="What net worth is made of"></div>
</section>
"""
)


@ledger.view(roles=BOTH, title="Net worth")
def wealth(
    ctx: QueryContext,
    span: Literal["1", "5", "10", "all"] = "1",
    basis: Literal["market", "cost"] = "market",
) -> Screen:
    """Net worth today, since 1 January and over a year; its curve by asset class, at market
    value or at cost; and what it is made of, each class with the money paid in and its return
    since 1 January."""
    today = ctx.now.date()
    entries = _load()[0]
    first_year = _first_year(entries)
    if first_year is None:
        return Screen(EMPTY, title="Net worth")
    # About twenty bars a chart at most, so that each stays readable: the last twelve month
    # ends, or quarters, half-years or years further back.
    if span == "1":
        months: tuple[int, ...] = tuple(range(1, 13))
        days = [
            _clip(today.year - 1 + (today.month - 1 + i) // 12, (today.month - 1 + i) % 12 + 1, 31)
            for i in range(12)
        ]
    else:
        back = {"5": today.year - 4, "10": today.year - 9, "all": first_year}[span]
        first = max(back, first_year)
        months = {"5": (3, 6, 9, 12), "10": (6, 12), "all": (12,)}[span]
        days = [
            d
            for d in (_clip(y, m, 31) for y in range(first, today.year + 1) for m in months)
            if d < today
        ]
    year_end, year_ago = date(today.year - 1, 12, 31), _clip(today.year - 1, today.month, today.day)
    points = {p.day: p for p in _worth([*days, today, year_end, year_ago])}
    series_days = [*days, today]
    now = points[today]

    def label(d: date) -> str:
        if d == today:
            return "Today"
        return {
            "1": f"{d:%b %y}",
            "5": f"Q{(d.month - 1) // 3 + 1} {d:%y}",
            "10": f"H{1 if d.month == 6 else 2} {d:%y}",
            "all": str(d.year),
        }[span]

    market = basis == "market"

    def by(p: WorthPoint) -> dict[str, Decimal]:
        return p.by_class if market else p.by_class_cost

    def net(p: WorthPoint) -> Decimal:
        return p.net if market else p.net_cost

    gross = sum((v for v in by(now).values() if v > 0), Decimal()) or Decimal(1)
    # Classes under 2 % of the assets today read as one slice and one band: Other.
    small = {c for c, v in by(now).items() if 0 <= v < gross / 50}

    def grouped(values: dict[str, Decimal]) -> dict[str, Decimal]:
        out: dict[str, Decimal] = defaultdict(Decimal)
        for c, v in values.items():
            out[OTHER_CLASS if c in small else c] += v
        return out

    shown = {d: grouped(by(points[d])) for d in series_days}
    order = sorted(
        {c for d in series_days for c in shown[d]},
        key=lambda c: (
            shown[today].get(c, Decimal()) < 0,
            c == OTHER_CLASS,
            -abs(shown[today].get(c, Decimal())),
        ),
    )
    series: list[dict[str, Any]] = [
        {
            "name": c,
            "values": [float(shown[d].get(c, 0)) for d in series_days],
            **({"tone": "error"} if shown[today].get(c, Decimal()) < 0 else {}),
        }
        for c in order
    ]
    series.append(
        {
            "name": "Net worth",
            "kind": "line",
            "tone": "neutral",
            "values": [float(net(points[d])) for d in series_days],
        }
    )
    # What it is made of: each class's value, and since 1 January its money paid in and its
    # return.
    earned: dict[str, Decimal] = defaultdict(Decimal)
    for a in performance(ctx, period="ytd", year=today.year):
        earned[a.asset_class] += a.result if market else a.income - a.costs
    one = Decimal(1)
    composition = []
    for c in sorted(
        set(by(now)) | set(by(points[year_end])),
        key=lambda c: (by(now).get(c, Decimal()) < 0, -abs(by(now).get(c, Decimal()))),
    ):
        value, start = by(now).get(c, Decimal()), by(points[year_end]).get(c, Decimal())
        if not (value.quantize(one) or start.quantize(one)):
            continue
        gain = earned.get(c, Decimal())
        moved = value - start - gain
        composition.append(
            {
                "class": c,
                "value": value,
                "share": value / gross if value > 0 else None,
                "paid_in": moved if moved.quantize(one) else None,
                "return": gain if gain.quantize(one) else None,
            }
        )
    slices = [
        (c, v)
        for c, v in sorted(shown[today].items(), key=lambda cv: (cv[0] == OTHER_CLASS, -cv[1]))
        if v > 0
    ]
    ytd, yoy = now.net - points[year_end].net, now.net - points[year_ago].net
    keep = {"span": span, "basis": basis}
    return Screen(
        WEALTH,
        args=keep,
        today=today,
        now=now,
        ytd=ytd,
        ytd_pct=_ratio(ytd, points[year_end].net),
        yoy=yoy,
        yoy_pct=_ratio(yoy, points[year_ago].net),
        span_tabs=_tabs(
            [("1", "1 year"), ("5", "5 years"), ("10", "10 years"), ("all", "All")],
            span,
            "span",
            keep,
        ),
        basis_tabs=_tabs([("market", "Market"), ("cost", "At cost")], basis, "basis", keep),
        chart={
            "kind": "stacked-bar",
            "labels": [label(d) for d in series_days],
            "series": series,
            "y": {"format": EUR},
        },
        donut={
            "kind": "donut",
            "labels": [c for c, _ in slices],
            "series": [{"name": "Assets", "values": [float(v) for _, v in slices]}],
            "format": EUR,
        },
        composition={
            "tools": ["sort"],
            "density": "compact",
            "rows": composition,
            "columns": [
                {"field": "class", "header": "Class"},
                {
                    "field": "value",
                    "header": "Value",
                    "format": EUR,
                    "total": "sum",
                    "tone": "negative",
                },
                {"field": "share", "header": "Share", "format": "percent:1.0-1"},
                {
                    "field": "paid_in",
                    "header": f"Paid in {today.year}",
                    "format": EUR,
                    "total": "sum",
                },
                {
                    "field": "return",
                    "header": f"Return {today.year}",
                    "format": EUR,
                    "total": "sum",
                    "tone": "signed",
                },
            ],
        },
        full=_full,
        short=_short,
        pct=_pct,
        date=_date,
    )


ASSETS = (
    PARTS
    + """
{{ header("Assets", span, years, year) }}
<div class="layout-row layout-between layout-gap-2">
  {{ choice(period_tabs, "Period") }}
  {{ choice(basis_tabs, "Valued") }}
</div>
{% if not grid.rows %}
<p class="text-base-content/60">Nothing earned or spent on an asset in this period.</p>
{% else %}
<div class="stats card-border">
  {{ stat("Return", full(total.result, True), short(total.result, True),
          tone='text-success' if total.result >= 0 else 'text-error') }}
  {{ stat("Income", full(total.income), short(total.income)) }}
  {{ stat("Costs", full(total.costs), short(total.costs)) }}
</div>
<div data-grid='{{ grid | tojson }}' aria-label="Each asset's return"></div>
{% endif %}
"""
)


@ledger.view(roles=BOTH)
def assets(
    ctx: QueryContext,
    period: Period = "ytd",
    year: Year = None,
    basis: Literal["market", "cost"] = "market",
) -> Screen:
    """Each asset over a window (year to date, a quarter or a year): its value, the money paid
    in or taken out, its own income and costs, and at market value the change of its
    unrealised gain."""
    today = ctx.now.date()
    first = _first_year(_load()[0])
    if first is None:
        return Screen(EMPTY, title="Assets")
    year = _shown_year(year, first, today)
    window = _window(year, period, today)
    rows = performance(ctx, period=period, year=year) if window else []
    market = basis == "market"
    one = Decimal(1)
    unvalued = any(x.unvalued for x in rows)
    grid_rows = []
    for a in sorted(rows, key=lambda a: -a.result):
        result = a.result if market else a.income - a.costs
        if not (a.result.quantize(one) or a.income or a.costs):
            continue  # a current account: nothing earned, nothing spent
        row: dict[str, Any] = {
            "asset": a.asset.rsplit(":", 1)[-1],
            "result": result,
            "rate": _ratio(result, a.start_value) if a.start_value > 0 and not a.unvalued else None,
            "value": a.value,
            "paid_in": a.value - a.start_value - a.result,  # from market figures
            "income": a.income,
            "costs": a.costs,
            "class": a.asset_class,
        }
        if market:
            row["unrealised"] = a.latent_change
        if unvalued:
            row["note"] = "No price" if a.unvalued else None
        grid_rows.append(row)
    columns: list[dict[str, Any]] = [
        {"field": "asset", "header": "Asset"},
        {"field": "result", "header": "Return", "format": EUR, "total": "sum", "tone": "signed"},
        {"field": "rate", "header": "Rate", "format": "percent:1.1-1", "tone": "signed"},
        {"field": "value", "header": "Value", "format": EUR, "total": "sum"},
        {"field": "paid_in", "header": "Paid in", "format": EUR, "total": "sum"},
        {"field": "income", "header": "Income", "format": EUR, "total": "sum"},
        {"field": "costs", "header": "Costs", "format": EUR, "total": "sum"},
    ]
    if market:
        columns.append(
            {
                "field": "unrealised",
                "header": "Unrealised",
                "format": EUR,
                "total": "sum",
                "tone": "signed",
            }
        )
    columns.append({"field": "class", "header": "Class"})
    if unvalued:
        columns.append({"field": "note", "header": "Note", "tones": {"No price": "warning"}})
    total = {
        "result": sum((x["result"] for x in grid_rows), Decimal()),
        "income": sum((a.income for a in rows), Decimal()),
        "costs": sum((a.costs for a in rows), Decimal()),
    }
    keep = {"period": period, "year": year, "basis": basis}
    named = dict(_periods(today))
    return Screen(
        ASSETS,
        args=keep,
        span=_span(*window) if window else f"{named[period]} {year}",
        year=year,
        years=list(range(today.year, first - 1, -1)),
        period_tabs=_tabs(_periods(today), period, "period", keep),
        basis_tabs=_tabs([("market", "Market"), ("cost", "Realised")], basis, "basis", keep),
        grid={"columns": columns, "rows": grid_rows},
        total=total,
        full=_full,
        short=_short,
    )
