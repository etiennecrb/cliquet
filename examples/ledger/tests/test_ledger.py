from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from app import (
    AssetClass,
    ImportLine,
    Leg,
    PriceLine,
    Rule,
    StatementLine,
    add_entries,
    add_prices,
    asset_accounts,
    assets,
    balance_sheet,
    bql,
    budget,
    checkup,
    classes_valid,
    classify,
    complete,
    contained,
    delete_rule,
    exceptional_declared,
    find_entries,
    fix_entry,
    import_statement,
    ledger,
    monthly,
    net_worth,
    performance,
    recategorize,
    reconciled,
    record_balance,
    results,
    rules,
    rules_valid,
    save_rule,
    set_classes,
    status,
    transactions,
    valid,
    wealth,
)

from cliquet.testing import Connection, Denied, InvalidArguments, Refused, World, given

NOW = datetime(2026, 5, 20, 9, 0, tzinfo=ZoneInfo("Europe/Paris"))
CHECKING = "Assets:Bank:Checking"

BOOK = """\
option "operating_currency" "EUR"
plugin "beancount.plugins.implicit_prices"

2024-01-01 open Assets:Bank:Checking EUR
2024-01-01 open Assets:Bank:Savings EUR
2024-01-01 open Assets:Broker:Cash EUR
2024-01-01 open Assets:Broker:FUND FUND
2024-01-01 open Assets:Home EUR
2024-01-01 open Liabilities:Bank:Mortgage EUR
2024-01-01 open Expenses:Living:Essentials EUR
2024-01-01 open Expenses:Living:Extras EUR
2024-01-01 open Expenses:Taxes:Income EUR
2024-01-01 open Expenses:Assets:Home:Loan-Interest EUR
2024-01-01 open Income:Acme:Salary EUR
2024-01-01 open Income:Assets:Bank:Savings:Interest EUR
2024-01-01 open Equity:Opening-Balances EUR

2024-01-01 * "Opening balances"
  Assets:Bank:Checking  2000.00 EUR
  Assets:Bank:Savings  10000.00 EUR
  Assets:Home  200000.00 EUR
  Liabilities:Bank:Mortgage  -150000.00 EUR
  Equity:Opening-Balances

2024-02-28 * "TRANSFER ACME SALARY"
  Assets:Bank:Checking  3000.00 EUR
  Expenses:Taxes:Income  300.00 EUR
  Income:Acme:Salary

2024-03-04 * "CARD 03/03/24 CORNER SHOP *0000"
  Assets:Bank:Checking  -45.10 EUR
  Expenses:Living:Essentials

2024-03-10 * "Buy FUND"
  Assets:Broker:Cash  -1000.00 EUR
  Assets:Broker:FUND  100 FUND {10.00 EUR, 2024-03-10}
  Assets:Bank:Checking

2024-12-31 * "Interest of the year"
  Assets:Bank:Savings  300.00 EUR
  Income:Assets:Bank:Savings:Interest

2025-01-05 * "DIRECT DEBIT EXAMPLE BANK LOAN"
  Assets:Bank:Checking  -800.00 EUR
  Liabilities:Bank:Mortgage  600.00 EUR
  Expenses:Assets:Home:Loan-Interest

2025-12-31 price FUND 12.00 EUR

2026-02-28 * "TRANSFER ACME SALARY"
  Assets:Bank:Checking  3200.00 EUR
  Expenses:Taxes:Income  320.00 EUR
  Income:Acme:Salary

2026-03-04 * "CARD 03/03/26 CORNER SHOP *0000"
  Assets:Bank:Checking  -52.30 EUR
  Expenses:Living:Essentials

2026-04-01 balance Assets:Bank:Checking  7302.60 EUR
"""

RULES = """\
pattern,direction,min,max,account,when,note
^4471,debit,,,Expenses:Assets:Home:Loan-Interest,always,Bank charges
EXAMPLE BANK LOAN,debit,,,,always,Split capital and interest
BAKERY,debit,,,Expenses:Living:Essentials,fallback,Probably essentials
"""

CLASSES = """\
pattern,class,depth,upkeep
^Assets:Broker(:|$),Stocks,1,monthly-prices
^Assets:Bank:Savings,Savings,2,interest
^Assets:Home,Property,1,revalue
^Assets:,Current accounts,2,
^Liabilities:,Debts,2,
"""


def world(book: str = BOOK) -> World:
    files = {"ledger.beancount": book, "rules.csv": RULES, "classes.csv": CLASSES}
    return given(ledger, files, now=NOW)


def owner(w: World | None = None) -> Connection:
    return (w or world()).connect("owner")


def reader() -> Connection:
    return world().connect("reader")


def line(day: str, label: str, amount: str, credit: bool = False, **more: Any) -> ImportLine:
    """A statement line to import, as an agent reads it."""
    return ImportLine(
        day=date.fromisoformat(day), label=label, amount=Decimal(amount), credit=credit, **more
    )


def statement(day: str, label: str, amount: str, credit: bool = False, detail: str = ""):
    """A statement line to classify."""
    return StatementLine(
        day=date.fromisoformat(day),
        label=label,
        detail=detail,
        amount=Decimal(amount),
        credit=credit,
    )


def price(day: str, commodity: str, amount: str) -> PriceLine:
    return PriceLine(day=date.fromisoformat(day), commodity=commodity, price=Decimal(amount))


def import_april(o: Connection, lines: list[ImportLine], closing: str, end: int = 30):
    return o.call(
        import_statement,
        lines=lines,
        opening_balance=Decimal("7302.60"),
        closing_balance=Decimal(closing),
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, end),
        bank=CHECKING,
    )


SHOP = "CARD 02/04/26 CORNER SHOP *0000"


# Importing a statement.


def test_a_statement_is_imported_with_its_balances() -> None:
    o = owner()
    loan = [
        Leg(account="Liabilities:Bank:Mortgage", amount=Decimal("601.00")),
        Leg(account="Expenses:Assets:Home:Loan-Interest"),
    ]
    imported = import_april(
        o,
        [
            line("2026-04-03", SHOP, "10.00", account="Expenses:Living:Essentials"),
            line(
                "2026-04-05",
                "DIRECT DEBIT EXAMPLE BANK LOAN",
                "800.00",
                detail="REF  77",
                legs=loan,
            ),
        ],
        "6492.60",
    ).expect_ok()
    assert imported.added == 2
    assert imported.balances == ["2026-05-01 6492.60"]  # the opening one was already there
    found = o.query(find_entries, text="BANK LOAN", limit=1)[0].text
    assert found.startswith('2026-04-05 * "REF 77 DIRECT DEBIT EXAMPLE BANK LOAN"')
    assert "  Liabilities:Bank:Mortgage  601.00 EUR" in found
    # The same statement again adds nothing.
    again = import_april(
        o, [line("2026-04-03", SHOP, "10.00", account="Expenses:Living:Essentials")], "7292.60", 3
    ).expect_ok()
    assert again.added == 0 and len(again.skipped) == 1


def test_a_statement_that_doesnt_reconcile_is_refused() -> None:
    import_april(
        owner(), [line("2026-04-03", SHOP, "10.00", account="Expenses:Living:Extras")], "7300.00"
    ).expect_refused(
        message="The statement doesn't reconcile: 7302.60 + lines = 7292.60, not 7300.00 (off by "
        "-7.40). The parse is wrong: a line is missing, doubled or in the wrong column."
    )


def test_a_line_without_account_is_refused() -> None:
    messages = import_april(
        owner(), [line("2026-04-03", SHOP, "10.00")], "7292.60"
    ).expect_refused()
    assert "Run `classify`" in messages[0]


def test_lines_outside_the_period_and_unknown_accounts_are_refused() -> None:
    o = owner()
    outside = line("2026-05-03", "CARD X *0000", "10.00", account="Expenses:Living:Extras")
    import_april(o, [outside], "7292.60").expect_refused(
        message="Lines outside the period: 2026-05-03 CARD X *0000"
    )
    unknown = line("2026-04-03", "CARD X *0000", "10.00", account="Expenses:Leisure")
    import_april(o, [unknown], "7292.60").expect_refused(
        message="Not an open account: Expenses:Leisure. See `accounts`."
    )


def test_classify_uses_rules_then_history_then_fallbacks() -> None:
    got = owner().query(
        classify,
        bank=CHECKING,
        lines=[
            statement("2026-04-03", SHOP, "9.00"),
            statement("2026-04-04", "DIRECT DEBIT BANK CHARGES", "5.00", detail="4471 FEES"),
            statement("2026-04-05", "DIRECT DEBIT EXAMPLE BANK LOAN", "800.00"),
            statement("2026-04-06", "CARD 05/04/26 BAKERY ROSE *0000", "3.00"),
            statement("2026-04-07", "CARD 06/04/26 CINEMA STAR *0000", "12.00"),
            statement("2026-04-08", "TRANSFER UNKNOWN", "50.00", credit=True),
        ],
    )
    assert [(s.account, s.source, s.review) for s in got] == [
        ("Expenses:Living:Essentials", "history", False),
        ("Expenses:Assets:Home:Loan-Interest", "rule", False),
        (None, "manual", True),
        ("Expenses:Living:Essentials", "fallback", True),
        ("Expenses:Living:Extras", "fallback", True),
        (None, "unknown", True),
    ]
    assert got[1].narration == "4471 FEES DIRECT DEBIT BANK CHARGES"


# Other writes.


def test_prices_balances_entries_and_fixes() -> None:
    o = owner()
    added = o.call(
        add_prices,
        lines=[
            price("2026-04-30", "FUND", "13.10"),
            price("2025-12-31", "FUND", "12.00"),
        ],
    ).expect_ok()
    assert added.added == 1 and added.skipped == ["2025-12-31 FUND"]
    o.call(add_prices, lines=[price("2026-04-30", "XYZ", "1")]).expect_refused(
        message="No account holds XYZ. See `accounts` for commodities."
    )
    o.call(
        record_balance,
        account="Assets:Bank:Savings",
        day=date(2026, 5, 2),
        amount=Decimal("10300.00"),
    ).expect_ok()
    assert [s.last_balance for s in o.query(status, text="Savings")] == ["10300.00 EUR"]
    cinema = f'2026-05-10 * "CARD 09/05/26 CINEMA *0000"\n  {CHECKING}  -12.00 EUR\n'
    o.call(add_entries, entries=cinema + "  Expenses:Living:Extras").expect_ok()
    o.call(fix_entry, old='CINEMA *0000"\n', new='CINEMA STAR *0000"\n').expect_ok()
    assert o.query(find_entries, text="CINEMA STAR")
    o.call(fix_entry, old="nowhere", new="x").expect_refused(
        message="`old` isn't in the ledger. Copy it from `find_entries`, spaces included."
    )


def test_a_pad_and_a_balance_for_a_meal_card() -> None:
    o = owner()
    o.call(add_entries, entries="2026-05-01 open Assets:Meal-Card EUR").expect_ok()
    o.call(
        record_balance,
        account="Assets:Meal-Card",
        day=date(2026, 5, 2),
        amount=Decimal("120.00"),
        pad_from="Expenses:Living:Essentials",
    ).expect_ok()
    assert o.query(status, text="Meal")[0].balance == {"EUR": Decimal("120.00")}


def test_rules_are_saved_ordered_and_deleted() -> None:
    o = owner()
    rule = Rule(pattern="CINEMA", account="Expenses:Living:Extras")
    o.call(save_rule, rule=rule, before="BAKERY").expect_ok()
    assert [r.pattern for r in o.query(rules)][-2:] == ["CINEMA", "BAKERY"]
    o.call(delete_rule, pattern="CINEMA").expect_ok()
    o.call(delete_rule, pattern="CINEMA").expect_refused(
        message="No rule has the pattern 'CINEMA'. See `rules`."
    )


# The rules.


def test_bad_rules_and_classes_are_refused() -> None:
    o = owner()
    o.call(save_rule, rule=Rule(pattern="(unclosed")).expect_refused(by=rules_valid)
    nowhere = Rule(pattern="X", account="Expenses:Nowhere")
    o.call(save_rule, rule=nowhere).expect_refused(by=rules_valid)
    o.call(set_classes, classes=[AssetClass(pattern="[", name="Broken")]).expect_refused(
        by=classes_valid
    )
    world().change("classes.csv", append="^Assets:Car,Car,1,weekly\n").expect_refused(
        by=classes_valid
    )


def test_an_include_or_a_foreign_plugin_is_refused() -> None:
    owner().call(add_entries, entries='include "other.beancount"').expect_refused(by=contained)
    world().change("ledger.beancount", append='plugin "evil.module"\n').expect_refused(by=contained)


def test_an_unbalanced_entry_is_refused() -> None:
    entry = f'2026-05-10 * "X"\n  {CHECKING}  -12.00 EUR\n  Expenses:Living:Extras  10.00 EUR'
    owner().call(add_entries, entries=entry).expect_refused(by=valid)


def test_a_confirmed_month_cant_change() -> None:
    o = owner()
    forgotten = f'2026-03-20 * "Forgotten"\n  {CHECKING}  -5.00 EUR\n  Expenses:Living:Extras'
    o.call(add_entries, entries=forgotten).expect_refused(by=reconciled)
    o.call(fix_entry, old="-52.30 EUR", new="-52.40 EUR").expect_refused(by=reconciled)


def test_an_unsure_entry_is_refused() -> None:
    unsure = f'2026-05-10 ! "X"\n  {CHECKING}  -1.00 EUR\n  Expenses:Living:Extras'
    messages = owner().call(add_entries, entries=unsure).expect_refused(by=complete)
    assert "ask them instead" in messages[0]


def test_new_accounts_follow_the_household_s_granularity() -> None:
    o = owner()
    for name in (
        "Income:Broker:Dividends",
        "Expenses:Living:Restaurants",
        "Expenses:Assets:Boat:Fees",
        "Expenses:Leisure",
    ):
        o.call(add_entries, entries=f"2026-05-01 open {name} EUR").expect_refused(by=asset_accounts)
    dividends = "2026-05-01 open Income:Assets:Broker:FUND:Dividends EUR\n  exceptional: FALSE"
    o.call(add_entries, entries=dividends).expect_ok()
    tax = "2026-05-01 open Expenses:Taxes:Property EUR\n  exceptional: FALSE"
    o.call(add_entries, entries=tax).expect_ok()


# BQL and analysis.


def test_bql_reads_and_refuses_writes() -> None:
    o = owner()
    got = o.query(
        bql,
        query="SELECT year, sum(number(convert(position,'EUR'))) AS total "
        "WHERE account ~ '^Expenses' GROUP BY year ORDER BY year",
    )
    assert got.columns == ["year", "total"]
    assert got.rows == [["2024", "345.10"], ["2025", "200.00"], ["2026", "372.30"]]
    with pytest.raises(Refused, match="Only SELECT"):
        o.query(bql, query="CREATE TABLE t USING 'csv:/etc/passwd'")
    with pytest.raises(Refused, match="BQL syntax error"):
        o.query(bql, query="SELEC")


def test_the_balance_sheet_values_at_market() -> None:
    sheet = owner().query(balance_sheet, as_of=date(2026, 3, 31))
    fund = next(x for x in sheet.lines if x.account == "Assets:Broker:FUND")
    assert (fund.cost, fund.value, fund.asset_class) == (
        Decimal("1000.00"),
        Decimal("1200.00"),
        "Stocks",
    )
    assert sheet.liabilities == Decimal("-149400.00")
    assert sheet.net == sheet.assets + sheet.liabilities


def test_the_balance_sheet_splits_money_moved_from_return() -> None:
    o = owner()
    move = f'2026-04-10 * "TRANSFER TO SAVINGS"\n  Assets:Bank:Savings  500.00 EUR\n  {CHECKING}'
    o.call(add_entries, entries=move).expect_ok()
    sheet = o.query(balance_sheet, as_of=date(2026, 5, 20), since=date(2024, 1, 1))
    lines = {x.account: x for x in sheet.lines}
    savings, fund = lines["Assets:Bank:Savings"], lines["Assets:Broker:FUND"]
    # The transfer and the purchase moved money; the interest and the price rise are the return.
    assert savings.flows == Decimal("500.00")
    assert savings.value - savings.start_value - savings.flows == Decimal("300.00")
    assert fund.flows == Decimal("1000.00")
    assert fund.value - fund.start_value - fund.flows == Decimal("200.00")
    loan = lines["Liabilities:Bank:Mortgage"]
    assert loan.value - loan.start_value - loan.flows == 0  # repaying capital is a move
    screen = str(o.view(wealth))
    assert "What it is made of" in screen and "Return 2026" in screen and "Paid in 2026" in screen


def test_net_worth_results_and_performance() -> None:
    o = owner()
    points = o.query(net_worth, step="year", from_year=2024)
    assert [p.day for p in points] == [date(2024, 12, 31), date(2025, 12, 31), date(2026, 5, 20)]
    assert points[1].net - points[1].net_cost == Decimal("200.00")  # the fund's unrealised gain
    years = o.query(results, period="q1", from_year=2024)
    assert [y.year for y in years] == [2024, 2025, 2026]
    assert years[0].buckets["Work income"] == Decimal("3300.00")
    assert years[2].buckets["Taxes"] == Decimal("320.00")
    perf = o.query(performance, period="year", year=2024)
    savings = next(a for a in perf if a.asset == "Bank:Savings")
    assert savings.income == Decimal("300.00") and savings.result == Decimal("300.00")
    with pytest.raises(Refused):
        o.query(performance, period="q4", year=2026)


def test_checkup_asks_for_each_class_s_upkeep() -> None:
    tasks = {t.task: t for t in owner().query(checkup)}
    assert tasks["Price of FUND"].state == "todo"
    assert tasks["Revaluation of Home"].detail.startswith("Booked at cost: never revalued.")
    assert tasks["Rate served by Assets:Bank:Savings in 2025"].state == "info"


# Roles and screens.


def test_the_reader_reads_everything_but_cannot_write() -> None:
    r = reader()
    assert r.query(balance_sheet).net
    assert r.query(bql, query="SELECT count(*) AS n").rows
    assert r.query(find_entries, text="CORNER SHOP")
    assert r.query(checkup)
    with pytest.raises(Denied):
        r.call(add_entries, entries="2026-05-01 open Expenses:Taxes:X EUR")
    with pytest.raises(Denied):
        r.call(
            recategorize,
            from_account="Expenses:Living:Essentials",
            to_account="Expenses:Living:Extras",
        )
    with pytest.raises(Denied):
        r.query(classify, lines=[])


def test_recategorize_moves_postings_and_keeps_amounts() -> None:
    o = owner()
    essentials, extras = "Expenses:Living:Essentials", "Expenses:Living:Extras"
    o.call(
        recategorize, from_account=essentials, to_account=extras, text="CORNER", expected=3
    ).expect_refused(
        message='2 entries match, not 3: 2024-03-04 * "CARD 03/03/24 CORNER SHOP *0000"; '
        '2026-03-04 * "CARD 03/03/26 CORNER SHOP *0000"'
    )
    moved = o.call(
        recategorize,
        from_account=essentials,
        to_account=extras,
        text="CORNER",
        from_day=date(2026, 1, 1),
        expected=1,
    ).expect_ok()
    assert moved.changed == ['2026-03-04 * "CARD 03/03/26 CORNER SHOP *0000"']
    assert o.query(find_entries, text="03/03/26 CORNER")[0].text.endswith(f"  {extras}")
    assert o.query(find_entries, text="03/03/24 CORNER")[0].text.endswith(f"  {essentials}")
    # A posting with an amount keeps it, in its column.
    o.call(
        recategorize,
        from_account="Expenses:Taxes:Income",
        to_account=extras,
        text="ACME",
        expected=2,
    ).expect_ok()
    assert f"  {extras}  300.00 EUR" in o.query(find_entries, text="ACME")[1].text
    o.call(
        recategorize, from_account=essentials, to_account=extras, text="NOWHERE"
    ).expect_refused()


def test_sale_fees_tagged_exceptional_leave_recurring_spending() -> None:
    o = owner()
    fees = "2026-04-02 open Expenses:Assets:Broker:FUND:Sale-Fees EUR"
    o.call(add_entries, entries=fees).expect_refused(by=exceptional_declared)
    paid = (
        f'2026-04-20 * "Sale fees"\n  {CHECKING}  -30.00 EUR\n'
        "  Expenses:Assets:Broker:FUND:Sale-Fees"
    )
    o.call(add_entries, entries=f"{fees}\n  exceptional: TRUE\n{paid}").expect_ok()
    april = o.query(monthly, year=2026)[3]
    assert (april.expenses, april.exceptional) == (Decimal("0"), Decimal("-30.00"))
    # The asset still carries its fee.
    perf = o.query(performance, period="ytd", year=2026)
    assert next(a for a in perf if a.asset == "Broker").costs == Decimal("30.00")


MONTHLY_BALANCES = """
2026-02-01 balance Assets:Bank:Checking  4154.90 EUR
2026-03-01 balance Assets:Bank:Checking  7354.90 EUR
"""


def test_checkup_flags_entries_against_the_rules() -> None:
    o = owner(world(BOOK + MONTHLY_BALANCES))
    o.call(
        add_entries,
        entries=f'2026-04-05 * "DIRECT DEBIT EXAMPLE BANK LOAN"\n  {CHECKING}  -800.00 EUR\n'
        "  Expenses:Living:Essentials\n\n"
        f'2026-04-06 * "4471 FEES DIRECT DEBIT BANK CHARGES"\n  {CHECKING}  -5.00 EUR\n'
        "  Expenses:Living:Extras",
    ).expect_ok()
    tasks = {t.task for t in o.query(checkup)}
    assert "Rule not followed: “^4471” → Expenses:Assets:Home:Loan-Interest" in tasks
    assert "Rule not followed: “EXAMPLE BANK LOAN” to split (Split capital and interest)" in tasks


def test_months_show_income_against_spending() -> None:
    months = owner().query(monthly, year=2026)
    assert [m.month for m in months] == ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05"]
    feb = months[1]
    assert (feb.income, feb.expenses, feb.savings) == (
        Decimal("3520.00"),
        Decimal("320.00"),
        Decimal("3200.00"),
    )
    assert months[2].savings == Decimal("-52.30")


def test_a_share_sale_is_exceptional_income() -> None:
    o = owner()
    o.call(
        add_entries,
        entries="2026-04-02 open Income:Assets:Broker:FUND:Sales EUR\n  exceptional: TRUE\n"
        '2026-04-02 * "Sale of FUND"\n'
        "  Assets:Broker:FUND  -50 FUND {10.00 EUR} @ 14.00 EUR\n"
        "  Assets:Broker:Cash  700.00 EUR\n"
        "  Income:Assets:Broker:FUND:Sales",
    ).expect_ok()
    april = o.query(monthly, year=2026)[3]
    assert (april.income, april.exceptional) == (Decimal("0"), Decimal("200.00"))
    year = o.query(results, period="ytd", from_year=2026)[0]
    assert year.exceptional == Decimal("200.00") and year.savings == year.income - year.expenses


def test_income_outside_salaries_is_other_income() -> None:
    o = owner()
    o.call(
        add_entries,
        entries="2026-05-01 open Income:Family:Allowance EUR\n  exceptional: FALSE\n"
        f'2026-05-05 * "TRANSFER FROM FAMILY FUND"\n  {CHECKING}  150.00 EUR\n'
        "  Income:Family:Allowance",
    ).expect_ok()
    may = o.query(monthly, year=2026)[4]
    assert may.buckets["Other income"] == Decimal("150.00")
    assert may.buckets["Work income"] == Decimal("0")
    assert may.income == Decimal("150.00")  # still recurring income
    assert o.query(monthly, year=2026)[1].buckets["Work income"] == Decimal("3520.00")
    # Unemployment benefit is work income.
    o.call(
        add_entries,
        entries="2026-05-01 open Income:Benefits:Unemployment EUR\n  exceptional: FALSE\n"
        f'2026-05-06 * "TRANSFER FROM JOB AGENCY"\n  {CHECKING}  900.00 EUR\n'
        "  Income:Benefits:Unemployment",
    ).expect_ok()
    may = o.query(monthly, year=2026)[4]
    assert may.buckets["Work income"] == Decimal("900.00")
    assert may.buckets["Other income"] == Decimal("150.00")


def test_a_new_income_account_must_say_if_it_is_exceptional() -> None:
    o = owner()
    bonus = "2026-05-01 open Income:Acme:Bonus EUR"
    messages = o.call(add_entries, entries=bonus).expect_refused(by=exceptional_declared)
    assert "exceptional: TRUE" in messages[0]
    o.call(add_entries, entries=f'{bonus}\n  exceptional: "yes"').expect_refused(
        by=exceptional_declared
    )
    # A one-off bonus, tagged, stays off the monthly income.
    paid = f'2026-05-15 * "Bonus"\n  {CHECKING}  1000.00 EUR\n  Income:Acme:Bonus'
    o.call(add_entries, entries=f"{bonus}\n  exceptional: TRUE\n{paid}").expect_ok()
    may = o.query(monthly, year=2026)[4]
    assert (may.income, may.exceptional) == (Decimal("0"), Decimal("1000.00"))


def test_every_screen_lays_out_for_each_role() -> None:
    for conn in (owner(), reader()):
        screen = str(conn.view(wealth))
        assert "Net worth" in screen and "At cost" in screen and "Debts" in screen
        assert "Net worth" in str(conn.view(wealth, span="all", basis="cost"))
        assert "Net worth" in str(conn.view(wealth, span="10"))
        screen = str(conn.view(budget))
        assert "a month" in screen and "Recurring income" in screen and "Essentials" in screen
        assert "1 Jan to 20 May 2026" in screen and "Recurring spending" in screen
        assert "Change" in screen and "Split" in screen and "Trend" in screen
        assert "'name': 'year'" in screen and "2024" in screen  # the year select
        past = str(conn.view(budget, year=2025, period="q4"))
        assert "Year 2025" in past and "2023" not in past  # no year before the first entry
        assert "Unrealised" in str(conn.view(assets, period="year", year=2025))
        assert "Paid in" in str(conn.view(assets, period="year", year=2024))
        cost = str(conn.view(assets, period="year", year=2024, basis="cost"))
        assert "Realised" in cost and "Unrealised" not in cost


def test_transactions_list_income_and_spending_by_category() -> None:
    for conn in (owner(), reader()):
        screen = conn.view(transactions, year=2026)
        text = str(screen)
        assert "CORNER SHOP" in text and "Essentials" in text and "Work income" in text
        assert screen["arguments"] == {"year": 2026}
        assert "Broker:FUND" not in text  # a purchase moves money between own accounts
        assert "BANK LOAN" in str(conn.view(transactions, year=2025))


def test_a_screen_refuses_a_year_without_entries() -> None:
    o = owner()
    only = "entries from 2024 to 2026 only, not 2019"
    with pytest.raises(Refused, match=only):
        o.view(budget, year=2019)
    with pytest.raises(Refused, match=only):
        o.view(transactions, year=2019)
    with pytest.raises(Refused, match=only):
        o.view(assets, year=2019)


def test_the_budget_on_new_year_s_day() -> None:
    new_year = datetime(2027, 1, 1, 9, 0, tzinfo=ZoneInfo("Europe/Paris"))
    files: dict[str, str | bytes] = {
        "ledger.beancount": BOOK,
        "rules.csv": RULES,
        "classes.csv": CLASSES,
    }
    o = given(ledger, files, now=new_year).connect("owner")
    screen = str(o.view(budget))
    assert "a month" in screen and "No spending, 1 Jan to 1 Jan 2027." in screen
    assert {y.months for y in o.query(results)} == {Decimal("0.1")}


def test_screens_with_no_ledger() -> None:
    o = given(ledger, {}, now=NOW).connect("owner")
    for screen in (o.view(budget), o.view(transactions), o.view(wealth), o.view(assets)):
        assert "The ledger has no entries yet." in str(screen)


def test_a_bad_argument_is_invalid() -> None:
    with pytest.raises(InvalidArguments):
        # As an agent sends it, in JSON: a model couldn't hold a lower-case commodity.
        bad = [{"day": "2026-04-30", "commodity": "fund", "price": "1"}]
        owner().call(add_prices, lines=bad)  # ty: ignore[invalid-argument-type]
