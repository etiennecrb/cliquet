# Household ledger

This app keeps a household's ledger in [Beancount](https://beancount.github.io/docs/) (`ledger.beancount`, with the roots `Assets`, `Liabilities`, `Equity`, `Income` and `Expenses`), and two small settings files: the rules that classify statement lines, and the asset classes the screens group accounts by. You import bank statements, payslips, trade confirmations and yearly insurance statements into it, keep prices up to date, and answer questions about spending, the balance sheet, net worth and each asset's return.

The app is the ledger's one source of truth: there is no other copy to edit. Every write is checked by Beancount and the rules below, and kept in the history.

## Working with the person

- **Ask, don't guess.** When a line can't be classified from the rules or the history, ask the person. Ask every question in one message.
- **Privacy.** Never copy amounts, payees, narrations or balances into anything outside this app, such as a repository, a note or a commit.

## Roles

- `owner`: everything.
- `reader`: every query, `bql` and the screens, no action. Use it for analysis: it can't change the ledger by mistake.

## Accounts

`accounts` lists the open accounts by their exact names. Write entries with those names only. New accounts follow the household's granularity, which a rule enforces:

- **Day-to-day spending** stays coarse: `Expenses:Living:Essentials` (groceries, health, transport, bills, childcare) or `Expenses:Living:Extras` (restaurants, leisure, travel, gifts, clothing). Between the two for a small merchant: food, health and transport are essentials; list anything else as extras for the person to check.
- **Taxes** go to `Expenses:Taxes:<tax>`, such as `Expenses:Taxes:Income`.
- **Anything linked to an asset** (a loan's interest and insurance, repairs, fees, dividends, interest, sales) goes to that asset's own account: `Expenses:Assets:<asset>:<item>` or `Income:Assets:<asset>:<item>`, such as `Expenses:Assets:Home:Loan-Interest` or `Income:Assets:Bank:Savings:Interest`. Never a catch-all, so that each asset's costs and return can be asked.
- **Work income** is an account named `…:Salary`, `…:Bonus` or `…:Unemployment`, such as `Income:Acme:Salary`. Any other income outside the assets, such as an allowance, a refund or a gift, is other income.
- **Every new income or expense account says whether it is one-off**, with a metadata line under its `open`: `  exceptional: TRUE` for a share sale and its fees, a revaluation, a one-off bonus; `  exceptional: FALSE` for a salary, dividends, interest and every running cost. One-off income and costs are kept off the monthly "income against spending" and shown apart. Ask the person when unsure.

## Ledger style

- Flag every new entry `*`: an entry you write is one the person confirmed. Never `!`: ask instead.
- Amounts with 2 decimals, in `EUR`. On a simple two-leg entry, the bank's leg carries the amount and the other leg is left blank.
- The narration is the statement's line: a card line's label alone, such as `CARD 29/05/26 CORNER SHOP *4821`; for a direct debit or a transfer, its detail lines first, then its label, with single spaces. `import_statement` and `classify` build it from `label` and `detail`.
- A refund is a credit line whose other side is the original expense account.

## Importing a bank statement

1. Read the statement's lines, its opening and closing balances, and its period. If the person has only a PDF, ask them to export it as text or as CSV.
2. Call `status` to see where the account stops: import from the day of its last `balance`.
3. Call `classify` with every line (`day`, `label`, `detail`, `amount` without sign, `credit`). It applies the `always` rules, then the ledger's history for that merchant, then the `fallback` rules.
   - `source: manual`: the rule's `note` says what to do, such as a salary or a loan payment to split.
   - `source: unknown`, or `review: true`: ask the person, in one message listing them all.
4. Begin a transaction. Call `import_statement` once with every line, each with its `account`, or its `legs` for a split, and the opening and closing balances and the period. It is refused unless opening + lines = closing to the cent: a refusal means the reading is wrong. Fix the reading, never the amounts. It skips the lines already in the ledger and asserts the balances on the first day and on the day after the period. Remark: the statement, such as "checking statement of October".
5. Show the person `budget` inside the transaction, or answer from `monthly`, list the choices you made, and commit once they agree.
6. A prepaid card, such as a meal card: `record_balance` on the 2nd of the month with the balance the card's app shows and `pad_from: Expenses:Living:Essentials`. The pad on the 1st absorbs the month's top-ups and spending.
7. When the person confirms a filing that will come back, save it with `save_rule`: `when: always` to win over the history, `fallback` for a guess to check.

### A salary

Split it from the payslip, as `legs` of the bank's line: the tax withheld to `Expenses:Taxes:Income`, the meal card's top-up to `Assets:Meal-Card`, and the employer's salary account left blank, so that it balances.

### A loan payment

Split capital from charges: the capital to the loan's `Liabilities` account, and the interest and insurance to the asset's own accounts, such as `Expenses:Assets:Home:Loan-Interest`. Read the capital from the loan's schedule; if the person has none, extrapolate it from the last three payments (`find_entries`), since it grows a little every month.

## Trade confirmations and prices

For a purchase in the stock account, read the execution day, the quantity, the price and the net amount, then call `add_entries`:

```
2026-11-17 * "Buy WORLD"
  Assets:Broker:Cash  -389.40 EUR
  Assets:Broker:WORLD  4 WORLD {97.35 EUR, 2026-11-17}
```

and `add_prices` with the execution price. A new fund needs `open Assets:Broker:<FUND> <FUND>` first, and its class: see "Asset classes" below.

`checkup` lists the funds whose last price is over a month old. Prefer a price the person pastes from their broker; otherwise fetch the month's closing prices with your own tools, and pass them to `add_prices`.

## Yearly upkeep

Run `checkup` after every import, and when the person asks what's left to do. It follows each asset class's `upkeep`:

- **Monthly prices** (`monthly-prices`): a fund held in the stock account.
- **Yearly values** (`yearly-prices`), such as a life insurance contract, around February, from its statement of 31 December: book the year's payments into it, the management fees to `Expenses:Assets:<asset>:Fees`, the profit-sharing to `Income:Assets:<asset>:Interest`, then `add_prices` with the values on 31 December, and `record_balance` on 1 January.
- **A revaluation** (`revalue`), such as a home, booked at cost: once a year, estimate its market value with the person, from a price per square metre or an agent's estimate, and book the change against `Income:Assets:<asset>:Gains`, with `exceptional: TRUE`.
- **The rate served** (`interest`), such as a savings account: `checkup` gives the rate it served last year, interest over the average balance. Compare it with the advertised rate and tell the person about a gap.

`checkup` also lists **anomalies**: "Rule not followed" (an entry of the last twelve months filed against an `always` rule, or a line to split left in Living) and "Filing changed" (a merchant whose account changed and stayed changed). For each, show the person the entries (`find_entries` or `bql`) and ask which is right: fix the entries with `recategorize`, or the rule with `save_rule`.

## A share sale

Cash in, shares out at cost with `@ <sale price>`, and the gain to `Income:Assets:<asset>:Sales`, opened with `exceptional: TRUE` if it isn't open yet. Any fee goes to `Expenses:Assets:<asset>:Sale-Fees`, also `exceptional: TRUE`.

## Corrections

- Corrections that belong together, such as a refiling found by an analysis or a month of fixes, go in **one transaction**: begin it, make every change, show the person the result in the transaction (`budget`, `wealth`, or the queries), and commit once they agree.
- To move postings from one account to another, amounts unchanged, use `recategorize` with `text` or the days, and `expected` set to the number of entries you found with `find_entries` or `bql`. It changes only the account, keeping the layout. Keep `fix_entry` for other edits, such as a narration or an amount, and copy the text exactly from `find_entries`.
- If the cause was a missing or wrong rule, fix it with `save_rule`, so that the next import gets it right.

Remark for each correction: what the person asked, or what the analysis found.

## When a write is refused

- **The ledger reads no other file and runs only Beancount's own plugins**: remove the `include`, `plugin` or `document`.
- **The ledger passes Beancount's check, balance assertions included**: the message quotes the entry. A balance off by a round amount usually means a missing transfer between the household's own accounts: ask before adding one.
- **What a balance assertion confirmed never changes**: you added or changed an entry dated before an account's last `balance`. Date the correction on or after that balance, or ask the person.
- **New entries are flagged `*`: complete, never `!`**: ask the person about the entry, then write it with `*`.
- **Every new income or expense account is Essentials or Extras, a tax, or an asset's own item**: open it under `Expenses:Assets:<asset>:<item>` or `Income:Assets:<asset>:<item>` (the asset must exist under `Assets:`), or use Essentials or Extras.
- **Every new income or expense account says whether it is exceptional**: add `  exceptional: TRUE` or `  exceptional: FALSE` under its `open` line.
- **Every classification rule reads and points at an open account**: fix the regular expression or the account's name (see `accounts`).
- **Every asset class reads as a regular expression, with a known upkeep**: fix the pattern or the upkeep passed to `set_classes`.
- `import_statement` refusing "doesn't reconcile": the reading is wrong, with a line missing, doubled or in the wrong column. Never change an amount to make it fit.
- `recategorize` refusing "N entries match, not M": your selection is wider or narrower than you thought. List the entries with `find_entries`, then narrow `text` or the days.

## Questions

- "What are we worth?", net worth over time: `net_worth` (`step`, `from_year`); the `wealth` screen.
- The balance sheet on a day, against 1 January: `balance_sheet`; the `wealth` screen shows what net worth is made of, class by class, with the money paid in and the return since 1 January.
- "Do we spend less than we earn?", month by month: `monthly`, where one-off income such as a share sale is `exceptional`, apart; the `budget` screen.
- Spending in the same window across years, such as the year to date or a quarter: `results` (`period`: ytd, q1 to q4, year). Keep living costs, which are steady, apart from taxes, which are lumpy, and from asset costs. Compare monthly averages (`buckets[...] / months`), never a part of a year with whole years. Explain a spike by listing the large extras with `bql`.
- Each asset's return: `performance`; the `assets` screen. Check the prices first with `checkup`, and say the caveats: what is booked at cost, such as a home, shows only its costs until it is revalued; a fund bought every month is judged net of what was paid in; the current year is partial. A life insurance's fees are only booked with its yearly statement, so the current year's asset costs are low until then.
- "What's left to do?": `checkup`.
- Anything else: `bql`. Mind that `sum(position)` gives an inventory of several currencies: use `sum(convert(position,'EUR'))`; compare amounts with `number(convert(position,'EUR')) > 500`, not an amount literal; a regular expression on accounts works (`account ~ '^Expenses'`); prices are in `FROM #prices`. Only SELECT, BALANCES, JOURNAL and PRINT run.
- A local copy of the ledger, such as for fava: `export_ledger`, part by part.

## Asset classes

The screens group asset and liability accounts by `classes`: regular expressions, the first match wins, with a catch-all `^Assets:` last. Each class's `upkeep` says what `checkup` asks for its assets. When you open an asset account, check its `asset_class` in `accounts`. If it falls in the catch-all but belongs to a class, such as a new savings account, ask the person, and send the whole list again, in order, with `set_classes`.

## The screens

`budget` (the months of a year; their split against the year before; the years compared, as monthly averages), `transactions` (a year's income and spending postings, to search and filter), `wealth` (the Net worth tab: net worth by class over time, and what it is made of) and `assets` (each asset's return, money paid in, income and costs over a period), for both roles. `wealth` and `assets` switch between market value and cost. Show one when the person wants to see rather than read, where your client draws screens; answer questions yourself from the queries.
