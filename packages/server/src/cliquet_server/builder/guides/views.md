# Views: an app's screens

Read this guide whole before writing a view, and follow its "Design rules". Use `find_classes` to look up a component's classes.

Set the app's `language` to the language its screens are written in, as `read_guide("app")` says. Without it, a French app's data grids, numbers and dates are in English.

## When to write one

Write a view only when the request asks for a screen. A view is a quick look at the data, such as a glance on a phone, and the few actions people do by hand. It is often shown inside the person's assistant. Most use goes through agents, which answer from the queries, so an app needs no view to be useful.

A view is drawn on the app's web page, and in a conversation where the client shows MCP Apps, such as Claude Desktop. In other clients the view's tool answers that the screen can't be shown there.

Every example has views. Read the closest before writing yours:

- `read_example("meeting-rooms", "app.py")`: one view per job. `week` is a table of rooms by day on a wide screen and a list of days on a phone. `day` puts a Book button on each room and Cancel on the caller's bookings. `find_room` is a search whose answer lists the free rooms with a Book button each, then why each other room can't. The admin also has `administration`, with tabs, whose Edit and Add a room buttons lay it out again as a form.
- `read_example("environments", "app.py")`: `catalog` is a data grid to sort, search and filter. `details` shows one record, chosen with a select: badges, a small table of facts, verdicts with their reasons, and lists.
- `read_example("ledger", "app.py")`: three numbers, then tabs of charts and grids: `budget` with a year select and link tabs for a period, `wealth` and `assets` with link tabs to switch between market value and cost, and `transactions`, a year's postings in a data grid.

## Design rules

A good screen is calm and obvious: a person sees what it is for, finds what they came for, and does it, on a phone or beside a conversation. The examples follow these rules: copy their patterns.

### Views and tabs

- **One job per view.** Sell at the till, check the shelves, receive a delivery: each is a view. A screen with forms for two jobs, or a form above a long report, does two jobs: split it into views, or into tabs when they are sections of one job.
- **Several views:** one per audience or workflow, each with its own `roles`. A screen only one role needs is its own view, such as the meeting rooms' `administration` for the admin. People pick a view from the app's tabs, and the first opens first, so write the most used one first: see "Rules" below for the tabs' order and labels.
- **Tabs** hold sections of one destination, read in any order, such as the ledger budget's Months, Split and Trend. Put the headline numbers above them.
- **Steps** for a short flow where only one step should show, such as a delivery's invoice, then its lines, then "Recorded": keep the step in an argument, and give each step a way back. Every view is a tab, so a screen only another one opens, such as a form to edit one record, is a step of that view, never a view of its own.
- **The main thing fits a phone's first screen:** the header, at most one row of numbers, then the form or the answer the person came for.

### Friendly

- **Say when.** Numbers say what period they cover: "Spent in November", "So far, to 9 November", "Entries up to 9 November 2026".
- **Show what an action did,** on the same screen: after a sale, "Sold today" lists it. The view is laid out again after each call, so this comes by itself.
- **Nothing to show:** `{% if rows %}…{% else %}<p class="text-base-content/60">Nothing sold yet today.</p>{% endif %}`. For an app with no data yet, show the header and an `alert`.
- **Words for the person:** month names, currency signs and plain labels, not `2026-06`, `TEA-SEN` or `in_stock`.

### Hierarchy

- **The top:** `<header><h2>Takings</h2><p class="text-base-content/60">Up to 9 November</p></header>`. The line under the title gives context, such as the period or the date of the values, or there is none. Never explain the screen there, and add no footnotes or explanatory text anywhere: the assistant explains the app from its code. An empty state or a status line isn't explanation: it says what the screen shows, or what happened.
- **Then the numbers that matter:** two to four `stat`s in one `stats`, in one row, on a phone too: stacked, three numbers would fill a phone's first screen. A `stat-desc` is one short line, such as "€4,644 a month": a third of a phone wraps a longer one onto three lines. Keep them short: `€7,846`, not `€7,845.50`. On a phone, `+€1.5M` fits three to a row. When an amount is longer than that, give it twice, the full one from 480 pixels wide and a short one below: `<div class="stat-value"><span class="layout-only-wide">€7,846</span><span class="layout-only-narrow">€7.8k</span></div>`.
- **One card per subject,** with a `card-title`, and a `text-base-content/60` line under it if it needs one.
- **Rows for things read one at a time,** such as what to reorder: a name on the left, its detail under it in `<small class="text-base-content/60">`, a number or a badge on the right.
- **A data grid for a list of records people explore,** such as every expense or every order, which they sort, search and filter.
- **A table for a statement with a fixed structure,** such as takings by product or a balance sheet: groups, subtotals, an order that means something, figures people compare down a column. Three columns fit a phone: a name and one or two short values.
- **A wider table when people also compare across,** such as months side by side. It scrolls sideways with each row's name in sight: put it in `<div class="layout-scroll-x">`, give it `table-pin-cols`, and start each row with a `<th>` that names it. Put the columns people look at most right after the name, as a phone shows them without scrolling. Keep figures short and on one line with `layout-nowrap`, and let text wrap: a title kept on one line can push the last column off a wide screen.

```html
<div class="layout-scroll-x">
  <table class="table table-pin-cols">
    <thead><tr><th>Category</th><th class="layout-end">Jan</th><th class="layout-end">Feb</th>…</tr></thead>
    <tbody><tr><th>Food</th><td class="layout-end layout-nowrap">€412</td>…</tr></tbody>
  </table>
</div>
```

A long one scrolls both ways in one box, to keep its header in sight too: `<div class="layout-scroll layout-scroll-x"><table class="table table-pin-rows table-pin-cols">`. The table's own `<div>` must hold `layout-scroll-x`, so never nest it in a second one.

- **Forms:** a label on every field, one `btn-primary`, and `required` on the fields the call needs.
- **Help people choose before they act.** When a form's action may be refused, such as a booking, let the screen ask first: a search lays the view out with what would pass, each with its own button, and why each other choice wouldn't, from the same query an agent calls.
- **An icon goes with words.** It helps a person find a button or a row, and the words say what it does. An icon stands alone only in a small button everyone knows, such as close (`x`) or delete (`trash`), with its `aria-label`. Use few: one per button or heading at most, and the same icon for the same thing across the app.

### The accent

Give the app a colour that fits its subject: `App("shop", ..., accent="#0f766e")`. It becomes daisyUI's `primary`: main buttons, active tabs and the app's web page. White text must read on it, at a contrast of at least 4.5:1, or `check_app` refuses it. So choose a darker shade, such as `#0f766e`, `#1d4ed8`, `#4338ca`, `#7c3aed`, `#be123c` or `#b45309`. Nothing else changes a screen's look: a screen has no `style` and no stylesheet of its own. The app's `icon` shows on its tile: see the app guide.

## A complete view

A view is a function that returns a `Screen`: an HTML template, written with [Jinja](https://jinja.palletsprojects.com/) against [daisyUI](https://daisyui.com/components/)'s components, and the values it shows. It needs nothing in the block at the top of `app.py`: Cliquet brings Jinja, daisyUI and the renderer.

This one belongs to an app, `board`, with an action `add` and a query `tasks`:

```python
from cliquet import QueryContext, Screen

OVERVIEW = """
<header>
  <h2>Board</h2>
</header>
<section class="card card-border">
  <div class="card-body">
    <h3 class="card-title">Tasks</h3>
    {% if tasks %}
    <ul class="list">
      {% for task in tasks %}<li class="list-row">{{ task }}</li>{% endfor %}
    </ul>
    {% else %}
    <p class="text-base-content/60">Nothing left to do.</p>
    {% endif %}
    {% if owner %}
    <form class="layout-stack layout-gap-3" data-call="add">
      <fieldset class="fieldset">
        <legend class="fieldset-legend">New task</legend>
        <input class="input" name="task" placeholder="Such as Water the plants" required>
      </fieldset>
      <button class="btn btn-primary">Add</button>
    </form>
    {% endif %}
  </div>
</section>
"""


@board.view(roles=["owner", "reader"])
def overview(ctx: QueryContext) -> Screen:
    """The tasks, with a form for the owner to add one."""
    return Screen(OVERVIEW, tasks=tasks(ctx), owner=ctx.caller.role == "owner")
```

Pressing "Add" calls the action `add` with the field `task`. If `add` refuses, its reason shows inside the form. If it succeeds, the form clears and the view is laid out again, so the new task shows. You write none of that: Cliquet's renderer does it the same way in every app.

## Rules

- **Signature.** `def name(ctx: QueryContext, ...) -> Screen`, with a docstring: it is the tool's description. A view may take arguments, typed like a query's, and each has a default: a view opens from the app's tabs with none.
- **The tab's label is the view's `title`,** or its function's name. Each view is a tab on the app's web page. Without a title, the tab reads as the function's name, with spaces for `_` and a capital first letter: `stock_levels` reads "Stock levels". To choose the wording, such as in the person's language, write `@compta.view(roles=["owner"], title="Résultats")` above `def resultats(...)`. A title is one line of at most 40 characters. The function's name stays the tool's name, so it follows the rule for names in `read_guide("app")`: lower-case ASCII letters, digits and `_`. Never put an accent in a function's name to word its tab.
- **The tabs follow the order the views are written in `app.py`,** and each role sees the ones it may open. The first of them opens first.
- **It runs like a query:** for each request, on the last commit, read-only. Call the app's queries as functions, such as `tasks(ctx)`.
- **`Screen(TEMPLATE, args={...}, **values)`.** Keep the template in a constant, such as `TILL`. The values are what it shows: lists, models, numbers, text, and functions such as `money=_money`. Format money and dates in Python or with a function you pass: `{{ money(row.total) }}`.
- **`args` are the screen's state.** They are the view's arguments that draw this screen again exactly as it is now, such as the lines of a receipt being built. After every click the view is laid out again with them. Leave out an argument that asks for something to happen, such as a line to add, or it happens again. Without `args`, the screen is drawn again with the view's defaults.
- **The layout can depend on the data and on `ctx.caller.role`,** such as a form only for the role that may use its action.
- **Templates stay in `app.py`.** For a part several views share, put a `{% macro %}` in a constant and add it to each template: `Screen(PARTS + TILL, ...)`.

## The template

Jinja, with Cliquet's settings:

- **Every value is escaped.** `{{ note }}` always shows as text, whatever it holds. There is no `safe` filter and no `{% autoescape %}`, and a value that is already markup is refused. So a member's note can never put a button on the owner's screen.
- **A value goes in text, or inside a quoted attribute value:** `<p>{{ name }}</p>`, `value="{{ month }}"`. Never in a tag outside quotes (`<p {{ x }}>`, `class={{ x }}`), and never as a tag's name.
- **A misspelled name is an error,** not an empty string. `None` shows as nothing.
- **`tojson`** writes a value as JSON, for `data-args`, `data-chart` and `data-grid`: `data-args='{{ {"lines": lines} | tojson }}'`. It writes models by their fields, a `Decimal` as a number and a date as `YYYY-MM-DD`.
- **Write only the screen's own HTML:** no `<html>`, `<head>`, `<body>`, `<script>`, `<style>`, `<img>` or `<svg>`: an icon is a `<span data-icon>`, below. Close every element: `<p>…</p>`, `<div></div>`. Void elements, such as `<input>` and `<br>`, have no end tag.

## How a screen calls the app

Forms make every call. Nothing happens until the person clicks or submits: no call when the screen opens, no message to the assistant.

| Write | It does |
| --- | --- |
| `<form data-call="add">` | Submitting it calls the action `add` with the form's fields, each by its `name`. |
| `data-args='{"limit": 5}'` | Fixed arguments, added to the fields. Write them with `tojson`. |
| `data-then='{"lines": []}'` | After the call succeeds, change the view's arguments so, then lay it out again. |
| `<form data-view>` | Submitting it lays this view out again, its current arguments changed by the fields and `data-args`. |
| `<form data-view="takings">` | Submitting it opens the view `takings` afresh, with only the fields and `data-args` as its arguments. Naming the view it is on starts it over. |
| `<a data-view="takings" data-args='{"month": "2026-10"}'>` | A link to a view of the app, opened afresh. With `data-view` alone, it lays this view out again, its arguments changed by `data-args`. |
| `<a href="https://…">` | A link to a web or mail address, which opens in a new tab. |

What the renderer does, in every app:

- **Names.** Each field's `name` is an argument of the tool it calls. Fields arrive as text and are converted by the tool's schema: a number field becomes a number. An empty optional field is left out, so its default applies.
- **While a call runs,** a slim bar slides along the top of the screen, and the button that sent it is disabled and shows that it works. Build no loading state of your own. Each submit gets its own `app_request_id`, so a call retried after a lost connection isn't made twice.
- **A refusal shows inside the form that caused it,** in the action's own words. Write refusals for a person: "Receipt R-0012 was already recorded on 13 November".
- **After a success,** that form clears and the view is laid out again with its arguments, changed by `data-then`.
- **Every layout keeps what the person changed,** after a call or a `data-view`: the open tab, ticked boxes, open `<details>`, a data grid's sort, search and filters, and what they typed in other forms, unless the new layout changed it. So radio tabs need no argument of their own.
- **Only actions.** A form calls an action. What a query answers, the view calls itself and lays out: to show another month or another search, lay the view out again with `data-view`. The built-in tools, such as `app_history`, are for agents. A screen never gets an action's result: after a call, the view is laid out again from the data.
- **No file.** A form can't send a file: an action whose argument is a `File` is for agents, who ask the person for it with `app_ask_for_file` (see "A file the person uploads" in `read_guide("app")`). The check refuses a form that calls one whose file is required, or that names its file in a field or in `data-args`. A form may call one whose file is optional, `File | None = None`, without it.
- **Roles.** Lay out a form only for the roles that may use its tool, with `{% if %}`. A screen that offers a role a tool it may not use is refused for that role.

**State lives in the view's arguments.** A screen that builds something before saving it, such as a receipt, keeps it in its arguments. Pressing "Add to receipt" lays the view out again with the product; the view adds the line and returns the lines as `args`:

```python
@shop.view(roles=["owner", "staff"])
def till(
    ctx: QueryContext, lines: tuple[SaleLine, ...] = (), sku: str = "", quantity: int = 1
) -> Screen:
    """The till: add each product of a receipt, then record it."""
    names = {p["sku"]: p["name"] for p in _read(PRODUCTS)}
    if sku in names:  # the person just pressed "Add to receipt"
        lines = (*lines, SaleLine(sku=sku, quantity=quantity))
    return Screen(TILL, args={"lines": lines}, lines=lines, names=names)
```

```html
<form class="layout-stack layout-gap-3" data-view>
  <!-- a select named sku, an input named quantity -->
  <button class="btn btn-outline">Add to receipt</button>
</form>
<form class="layout-stack layout-gap-3" data-call="record_receipt"
      data-args='{{ {"lines": lines} | tojson }}' data-then='{"lines": []}'>
  <!-- an input named receipt -->
  <button class="btn btn-primary" {% if not lines %}disabled{% endif %}>Record receipt</button>
</form>
```

The same holds for a chosen week (`week` in the meeting rooms), a year and a period (`year` and `period` in the ledger's `budget`), or a search (the day and times in `find_room`). An agent can open the screen in any of these states by calling the view's tool with the arguments.

**A list of records people sort, search or page through** is a data grid, below: the renderer does all of it in the browser. Never write sorting, filtering, searching or paging yourself for the rows a view sends. Only a view with too many rows to send, which pages on the server, keeps its sort in an argument, such as `sort: Literal["name", "quantity"] = "name"`. Each header is then a link that lays the view out again with its own `sort`, and the view sorts its rows in Python:

```html
<thead><tr>
  <th><a class="link" data-view data-args='{"sort": "name"}'>Product</a></th>
  <th class="layout-end"><a class="link" data-view data-args='{"sort": "quantity"}'>Units</a></th>
</tr></thead>
```

**A button on each row** is a small form per row, with the row's own value in `data-args`:

```html
{% for tool in tools %}
<li class="list-row">
  <span class="list-col-grow">{{ tool.name }}</span>
  {% if tool.out %}
  <form data-call="record_return" data-args='{{ {"tool": tool.name} | tojson }}'>
    <button class="btn btn-sm btn-outline">Bring back</button>
  </form>
  {% endif %}
</li>
{% endfor %}
```

## Layout and components

A screen uses daisyUI's classes and Cliquet's own layout classes, which start with `layout-`. Any other class is refused, Tailwind's included, with the class to use instead. `find_classes("stat")` lists the classes whose name holds `stat`, by component.

**Cliquet's layout classes.** daisyUI has components but no layout: these fill the gap. They all start with `layout-`, which no class of daisyUI's does.

| Class | What it does |
| --- | --- |
| `layout-stack` | A column, its parts 1rem apart. The screen itself is one. |
| `layout-row` | A row, centred, that wraps on a phone. |
| `layout-cols-2`, `layout-cols-3`, `layout-cols-4` | A grid of equal columns. Each is a grid by itself; `layout-grid` is a grid of one column. |
| `layout-fit` | A grid of columns at least 12rem wide, by itself: they stack on a phone. |
| `layout-span-2`, `layout-span-all` | A part of a grid two columns wide, or as wide as the grid. |
| `layout-gap-0` to `layout-gap-6` | The space between the parts: `layout-gap-1` .25rem, `layout-gap-3` .75rem, `layout-gap-4` 1rem. |
| `layout-between` | Spread a row's parts to both ends. |
| `layout-end`, `layout-center` | Gather a row's parts at the end or in the middle. On a table cell or a line of text, align its text: `<td class="layout-end">` for an amount. |
| `layout-top`, `layout-baseline`, `layout-bottom` | Align a row's parts at the top, on their text, or at the bottom, such as a button beside a labelled field. |
| `layout-grow`, `layout-fill`, `layout-nowrap` | Take the room left, take the whole width, keep on one line. |
| `layout-truncate` | Keep a line of text to one line, cut short with "…", such as a long name in a `list-row` or a `<td>`. Put the whole text in `title` if the person may need it. |
| `layout-scroll` | Keep a long part to 20rem, and scroll the rest: a history, a log, a `chat`. For a table, put it in a `<div class="layout-scroll">`, and add daisyUI's `table-pin-rows` to the table to keep its header in sight. |
| `layout-scroll-x` | Scroll a wide part sideways, such as many `steps` or a `layout-row layout-nowrap` of badges. |
| `layout-lines` | Keep the line breaks of a text a person typed, such as a note or an address: `<p class="layout-lines">{{ order.note }}</p>`. |
| `layout-only-wide`, `layout-only-narrow` | Show a part only from 480 pixels wide, or only below: `<span class="layout-only-wide">Quantity</span><span class="layout-only-narrow">Qty</span>`. They take no `sm:` or `md:`. |

A screen grows with its content, so it scrolls with the conversation. Use `layout-scroll` only for a part a person scrolls through, never for the main thing: a screenshot shows only its first 20rem.

The layout classes take `sm:` (from 480 pixels wide) and `md:` (from 640): `layout-stack sm:layout-row`. So do daisyUI's responsive ones, such as `card sm:card-side`. A screen is drawn 380 to 700 pixels wide, so `lg:`, `xl:` and `2xl:` would never apply: they are refused.

**Text** needs no class of Cliquet's to look right. Secondary text is daisyUI's `text-base-content/60`, and its colours `text-primary`, `text-success`, `text-warning` and `text-error` say what a number means. HTML has the rest: `<small>`, `<strong>`, `<h2>` to `<h4>`. A table's figures line up by themselves.

**daisyUI's components that suit a screen.** Their documentation is at [daisyui.com/components](https://daisyui.com/components/).

- **Card:** `<section class="card card-border"><div class="card-body"><h3 class="card-title">…</h3>…</div></section>`.
- **Numbers:** `<div class="stats card-border"><div class="stat"><div class="stat-title">Spent</div><div class="stat-value">€1,240</div><div class="stat-desc">So far, to 9 November</div></div>…</div>`. How many, in one row, and how short: see "Hierarchy" above.
- **List:** `<ul class="list"><li class="list-row"><div class="list-col-grow">…</div><span class="badge">…</span></li></ul>`. `list-col-grow` marks the part that takes the room. For things read one at a time.
- **Data grid:** `<div data-grid='{{ grid | tojson }}'></div>`, for a list of records people explore: see Data grids, below.
- **Table:** `<table class="table">` with `<thead>` and `<tbody>`. Numbers in `<td class="layout-end">`, the header too. For a statement with a fixed structure: groups, subtotals, an order that means something. A phone shows three columns: a wider table scrolls sideways, as "Hierarchy" above says.
- **Field:** `<fieldset class="fieldset"><legend class="fieldset-legend">Receipt</legend><input class="input" name="receipt"></fieldset>`. Also `select`, `textarea`, `checkbox`, `toggle`, `radio` and `range`. A field fills its column.
- **Filter bar:** a labelled field and its button in one row. The legend pushes the field down, so line them up at the bottom: `<form class="layout-row layout-bottom" data-view><fieldset class="fieldset"><legend class="fieldset-legend">Category</legend><select class="select" name="category">…</select></fieldset><button class="btn">Show</button></form>`. Without a legend, use `join`, as the meeting rooms' day picker does.
- **A select that lays the view out:** a named `<select>` alone in a `<form data-view>`, with no button, lays the view out again as soon as the person picks, with `data-args` added as for any `data-view` form: `<form data-view><select class="select" name="year" aria-label="Year"><option selected>2026</option><option>2025</option></select></form>`. It is as wide as its options, and one gesture makes the choice. For a view's argument, such as a year, use link tabs for a few options and this select for more: tabs past four or five overflow a phone.
- **Button:** `btn`, with `btn-primary` for the form's main button, `btn-outline` or `btn-ghost` for the others, and `btn-sm` in a row.
- **Icon:** an empty `<span data-icon="plus"></span>`, with the name of one of Lucide's icons, which `find_icons` finds by a word, such as `find_icons("plus")`. Cliquet draws it, 1em high in the colour of the text around it, so it fits a button, a list row, a link tab (`<a role="tab">`) or a heading: `<button class="btn btn-primary"><span data-icon="plus"></span> Add</button>`. A value may name it, `data-icon="{{ row.icon }}"`, and the name is checked when the view is laid out. A button that shows only an icon names itself for screen readers with `aria-label`: `<button class="btn btn-square btn-ghost btn-sm" aria-label="Close"><span data-icon="x"></span></button>`. In a `btn-square` or a `btn-circle`, the icon is a quarter larger, so it reads alone.
- **Badge, status and alert:** `badge badge-soft`, `badge-warning`, `badge-success`; `alert alert-soft`, `alert-warning` with `role="status"`. daisyUI's neutral is near-black in both themes, a colour for a background: a solid `badge-neutral` or `btn-neutral` reads, and so does `btn-soft btn-neutral`, but `text-neutral`, `link-neutral` and a soft, outline, dash or ghost neutral badge or button, or a `btn-link btn-neutral`, vanish in the dark theme. A grey badge is `badge badge-soft` or `badge badge-ghost`, and muted text is `text-base-content/70`. A `-content` colour, such as `text-primary-content`, is for text on its own colour only: inside `bg-primary`, or a solid `btn-primary` or `badge-primary`.
- **Tabs that keep their place:** radio inputs, which need no call. `<div role="tablist" class="tabs tabs-border"><input type="radio" name="section" class="tab" aria-label="Products" checked><div class="tab-content">…</div>…</div>`. Each content comes straight after its tab, in one `tabs` tablist whose radios share a name of their own and one `checked`. A `tab-content` takes colours only: a class that sets its display, such as `card` or `layout-stack`, shows it under every tab, so put that on a `<div>` inside it.
- **Tabs that change the view's arguments:** links, such as a month at a time. `<nav role="tablist" class="tabs tabs-border"><a role="tab" class="tab tab-active" data-view data-args='{"month": "2026-11"}'>November</a>…</nav>`. For a few options, put `tabs tabs-box` in a `layout-scroll-x`; for more, use a select that lays the view out, above.
- **Steps:** `<ul class="steps"><li class="step step-primary">Invoice</li>…</ul>`.
- **More:** `collapse` and `<details>`, `dropdown`, `join` (a field and its button side by side), `divider`, `progress`, `tooltip` with `data-tip`, `kbd`, `link`, `loading`, `skeleton`, `timeline`, `menu`, `breadcrumbs`.
- **`<details>` stays open.** Every layout keeps an open `<details>` open, so a menu in one stays open after a choice. Use a select or link tabs for a choice the screen acts on.
- **A `dropdown` or a `tooltip` is cut off** inside `stats` or a part that scrolls, such as `layout-scroll` or `layout-scroll-x`: put it outside. A `tooltip` opens only on hover, so never on a phone: never put in one what a person needs to read.

A modal, a drawer, a dock, a toast, a file input, a calendar and a theme controller can't work on a screen, and are refused with what to use instead.

## Charts

A chart is an empty element with Cliquet's chart format in `data-chart`. The renderer draws it with the app's colours, and a table of its numbers for screen readers. You never write a chart library's options.

```python
chart = {
    "kind": "stacked-bar",  # bar, stacked-bar, line, area, donut or scatter
    "labels": ["Jun", "Jul", "Aug"],
    "series": [
        {"name": "Food", "values": [412.5, 380.0, 455.2]},
        {"name": "Rent", "values": [950, 950, 950]},
    ],
    "y": {"format": "currency:EUR"},  # number, integer, percent (0.25 is 25%) or currency:XXX
    "title": "Spending by category and month",
}
```

A number format may fix its digits after a colon, as Angular's `digitsInfo`: `{minIntegerDigits}.{minFractionDigits}-{maxFractionDigits}`, all three written. `currency:EUR:1.0-0` writes whole euros, as €1,295,870 in English; `currency:EUR:1.2-2` always shows the cents; `number:1.0-2` shows up to two decimals; `percent:1.1-1` writes `25.5%`, the digits counting as the percent shows. The integer digits are 1 to 9, the decimals 0 to 6. A value with more decimals is rounded to the nearest, half away from zero: `-3.6` in `1.0-0` is `-4`. `integer` takes no digits: it is `number:1.0-0` already. Without digits, a number shows every decimal it has, and a currency at least its cents. The digits hold wherever the format is: an axis, a donut, a grid's column and its total, and their tooltips. Axis ticks stay short, and step by no less than the format's most decimals: whole euros have no tick at €1.50.

```html
<div data-chart='{{ chart | tojson }}'></div>
```

- One value per label in each series: give a month without spending a zero.
- A click on a legend item hides its series, and a second click shows it again. So a view needs no button to switch between two series: draw both.
- A series without a tone takes its colour from its name, so it keeps it in every chart of the app, a donut's slices included: name a category the same way in every chart. No such colour is close to a tone, such as a green or a red, or is the app's accent, so a category never reads as a tone, and keeps its colour beside any tone.
- When a series' colour means something, give it a `tone`: `success`, `error`, `warning`, `info` or `neutral`, the grey of a total or a baseline. Income is `{"name": "Income", "values": [...], "tone": "success"}` and spending `"tone": "error"`. A tone looks right in the light and dark themes. Two series of a chart can't share a tone.
- In a stacked chart of income and spending, give the income `success` and leave its parts, the categories of spending, without a tone: each keeps its colour by name, and none looks like income. A line of the balance over them takes `neutral`.

```python
chart = {
    "kind": "stacked-bar",
    "labels": ["Jun", "Jul", "Aug"],
    "series": [
        {"name": "Income", "values": [3200, 3100, 3250], "tone": "success", "kind": "line"},
        {"name": "Rent", "values": [950, 950, 950]},
        {"name": "Food", "values": [412.5, 380.0, 455.2]},
        {"name": "Extras", "values": [180, 240, 95]},
        {"name": "Balance", "values": [1657.5, 1530.0, 1749.8], "kind": "line", "tone": "neutral"},
    ],
    "y": {"format": "currency:EUR"},
}
```
- A donut has one series: its labels are the slices, and its series has no tone. It has no axes, so it takes its `format` at the top: `{"kind": "donut", "format": "currency:EUR", ...}`. Its total, in its middle, is written in that format, and a currency without digits shows at most its cents.
- Write the axis as people read it: give the labels as the values the chart shows, in text, and let the `x` axis's `format` write them. A month's number, a weekday's or a date, as `"1"` to `"12"`, `"1"` to `"7"` from Monday, or `"2026-11-03"`: `"x": {"format": "month:short"}` writes `1` to `12` as `Jan` to `Dec`, and `:long` writes a date whole, as `3 November 2026`. No format leaves the labels as the view wrote them.

```python
chart = {
    "kind": "bar",
    "labels": [str(month) for month in range(1, 13)],
    "series": [{"name": "Spent", "values": spending_by_month}],
    "x": {"title": "Month", "format": "month:short"},
    "y": {"format": "currency:EUR"},
}
```
- A line or area chart shows a point under the pointer. A value alone, with no value on either side, always shows its point.
- A bar, stacked-bar or area chart's axis starts at zero. A line chart's axis fits its values, so an uptime from 99% to 100% shows its trend. Ticks show as many decimals as they need to differ.
- To draw a line over bars, give its series `"kind": "line"` in a `bar` or `stacked-bar` chart. The line is drawn on top and never stacked. Only a bar chart's series take a kind.
- A line on another scale, such as a margin next to amounts, goes on the second axis, `y2`, on the right: give it `"axis": "y2"`. Only a line goes there: a line chart's series, or a line over bars. `y2` takes a `title`, a `format` and a span, as `y` does.

```python
chart = {
    "kind": "bar",
    "labels": ["Jun", "Jul", "Aug"],
    "series": [
        {"name": "Revenue", "values": [12400, 13100, 11800]},
        {"name": "Margin", "values": [0.31, 0.28, 0.34], "kind": "line", "axis": "y2"},
    ],
    "y": {"format": "currency:EUR"},
    "y2": {"title": "Margin", "format": "percent"},
}
```

- To show a target or a budget, add a series with the same value at every label, `"tone": "neutral"` and `"kind": "line"`, in a bar chart. It reads as a flat grey line across the bars.

```python
chart = {
    "kind": "stacked-bar",
    "labels": ["Jun", "Jul", "Aug"],
    "series": [
        {"name": "Food", "values": [412.5, 380.0, 455.2]},
        {"name": "Rent", "values": [950, 950, 950]},
        {"name": "Budget", "values": [1400, 1400, 1400], "kind": "line", "tone": "neutral"},
    ],
    "y": {"format": "currency:EUR"},
}
```

### Axes

Each axis describes itself. `y`, also spelled `y1`, takes a `title`, drawn along the axis, a `format`, `number` when it has none, and a `min` and `max`. The axis covers at least that span, and grows to show the values beyond it. So `"y": {"min": 0}` makes a line chart start at zero, and `"max": 1` keeps a percent axis up to 100%.

```python
chart = {
    "kind": "line",
    "labels": ["Jul", "Aug", "Sep"],
    "series": [{"name": "Uptime", "values": [0.991, 0.995, 0.999]}],
    "x": {"title": "Month"},
    "y": {"title": "Uptime", "format": "percent", "min": 0.9, "max": 1},
}
```

- Only a donut takes a `format` at the top, and no series takes one: a series is in the format of its axis.
- A bar, stacked-bar, line or area chart's `x` holds its labels, so it takes a `title` and the `format` its labels are written in: `date`, `month` or `weekday`, each with `:short` or `:long`, such as `month:short`. It takes no `min`, `max` or number format, and a label its format doesn't read is refused.
- A bar, stacked-bar or area chart's `y` can't have a `min` above zero: a bar cut short misrepresents its value. Draw a line chart instead.

### Scatter

A scatter shows how two numbers go together, one point per item. It has no `labels`: each series has `points`, each with an `x`, a `y` and an optional `label`, which its tooltip shows. Its `x` is a span of numbers, and takes a `format`, a `min` and a `max`, as `y` does. A series has a `name` and an optional `tone`, and no `kind` or `axis`. A scatter has at most 1,000 points, and no `y2`.

```python
chart = {
    "kind": "scatter",
    "x": {"title": "Units sold a month", "format": "integer"},
    "y": {"title": "Margin", "format": "percent", "min": 0},
    "series": [
        {"name": "Green tea", "points": [{"x": 140, "y": 0.42, "label": "Sencha"}]},
        {"name": "Black tea", "points": [{"x": 210, "y": 0.28, "label": "Assam"}]},
    ],
}
```

## Data grids

A list of records, such as expenses, orders or members, is a data grid: an empty `<div>` with Cliquet's grid format in `data-grid`. The renderer draws a table and its tools, the same in every app and on a phone too: a click on a header sorts its rows up, then down, then back; a search box matches any cell; a Filters button opens a filter for each column; 25 rows a page; and a footer of totals. You write none of it.

```python
grid = {
    "columns": [
        {"field": "day", "header": "Day", "format": "date"},
        {"field": "amount", "header": "Amount", "format": "currency:EUR", "total": "sum"},
        {"field": "paid_to", "header": "Paid to"},
        {"field": "category", "header": "Category"},
    ],
    "rows": [
        {"day": e.day, "amount": e.amount, "paid_to": e.payee, "category": e.category}
        for e in expenses
    ],
}
```

```html
<div data-grid='{{ grid | tojson }}' aria-label="Expenses"></div>
```

- Each column shows one `field` of the rows, a name of letters, digits and `_`, under its `header`. Without a header, it reads as its field: `paid_to` reads "Paid to". A grid has 1 to 30 columns.
- A column's `format` is `text`, the default; a chart's number format: `number`, `integer`, `percent` (0.25 is 25%) or `currency:XXX`; or a label's, as a chart's `x` takes it: `date`, `month` or `weekday`, each with `:short` or `:long`, such as `month:short` for `Nov`. A column in any date format, `date` included, holds days as `YYYY-MM-DD`, as `tojson` writes a `date`. The renderer writes each cell in it, numbers on the right. A number format with digits, such as `currency:EUR:1.0-0`, rounds to them, as in a chart. Without digits, a cell shows every decimal its value has, and a currency at least its cents: an uptime of `0.99998` reads 99.998%, never 100%. Give cells as values, never as formatted text: `12.5`, not `"€12.50"`, so that they sort and add up.
- A `month` or `weekday` column still holds dates: its cells are `YYYY-MM-DD`, and it sorts, filters and totals by its date, while it shows only what its format names.
- A cell is text, a number, `true`, `false`, or `null` for none. A number column holds numbers, and a date column dates.
- `total` adds a footer row: `sum`, `avg`, `min`, `max` or `count`. `sum` and `avg` are for numbers, `min` and `max` for numbers and dates. A total counts the column's values, of the rows that pass the search and the filters, on every page. `sum`, `avg`, `min` and `max` are written in the column's own format, so a `min` or `max` on a column that shows only a month or a weekday reads `Nov`, not the date it came from: give that column `date:short`, or leave the total out, when the footer must say which day it is. `count` is always a whole number. An `avg` is in the column's digits when it has some; without, an `avg` of whole numbers has decimals too.
- A column can colour its cells, each case with one look. An amount is coloured by its text: a number column takes `"tone": "signed"`, a positive in green and a negative in red, for a result or a transaction that is income or spending, or `"tone": "negative"`, only the negatives in red, for a balance that must not fall below zero. Zero stays plain, the number keeps its minus, and the total takes the same tone. A state is a soft badge: a text column takes `"tones"`, a map from a cell's value to a tone, `success`, `error`, `warning`, `info` or `neutral`, as a chart's series takes it: `{"field": "status", "tones": {"Paid": "success", "Late": "error"}}`. Other values stay plain text, and sorting, search and filters still read the value. A column takes one or the other, never both.
- A long grid scrolls in its own box, with its header row and its totals always in sight. The first column stays in sight as the table scrolls sideways: make it the one that names a row, such as a day or a name. Put the column people look at most, such as an amount, right after it: a phone shows two or three columns at once.
- Each column's filter follows its data: from and to for numbers and dates, values to tick for text with few of them, such as a category, and "contains" for other text.
- The bar above the table holds the search box, with a button that clears it, and the Filters button, with how many filters are on in a badge. Each filter on shows under the bar as a chip, such as "Category: Food, Rent", that removes it, so a filter shows while its panel is closed. On a phone, the Filters button shows only its icon and the badge.
- Send the rows in the order people read first, such as the latest first: the grid shows that order until a header is clicked.
- `tools` lists the tools a grid shows: any of `"sort"`, `"search"`, `"filters"` and `"pages"`, or `[]` for none. It shows exactly those, however many rows it has. Without `tools`, a grid of 10 rows or fewer only sorts, and a longer one has every tool. A grid without `"pages"` shows every row in its scrolling box, with no count under it.
- `"density": "compact"` gives a grid the short rows and small text of `table table-xs`; `"normal"` is the default. A small table of figures, such as 12 months or ten asset classes, wants `"tools": ["sort"]` and `"density": "compact"`: it reads at a glance, and sits beside the app's other compact tables.
- A grid holds at most 5,000 rows. For more, narrow them in the view, such as to a month, with an argument to choose another.
- Put nothing inside the `<div>`, and no `<table>` around it. Its `aria-label` names the table for screen readers.
- The grid's own words, such as Search, Filters and "1–25 of 42", and its numbers and dates are in the app's `language`: a `month:short` column reads `Jan` in English, `janv.` in French. Write the headers in the same language.

### Groups

A statement, such as income, then living costs, then savings, is a grid that groups its rows. `group_by` names the field of the rows that holds each one's group. Each group gets a title row, which stays pinned under the header while its rows scroll, and, when a column has a `total`, a subtotal row where it ends, such as "Total Income". The footer keeps the grand total. A row may give `"indent": 1` or `2` to sit in under the row above it, such as a line of spending under its heading.

```python
grid = {
    "group_by": "section",
    "tools": ["sort"],
    "columns": [
        {"field": "line", "header": "Line"},
        {"field": "amount", "header": "Amount", "format": "currency:EUR", "total": "sum"},
    ],
    "rows": [
        {"section": "Income", "line": "Salary", "amount": 3200},
        {"section": "Living costs", "line": "Essentials", "amount": None},
        {"section": "Living costs", "line": "Rent", "amount": 950, "indent": 1},
        {"section": "Living costs", "line": "Groceries", "amount": 420, "indent": 1},
        {"section": "Living costs", "line": "Extras", "amount": None},
        {"section": "Living costs", "line": "Restaurants", "amount": 120, "indent": 1},
        {"section": "Savings", "line": "Emergency fund", "amount": 300},
    ],
}
```

- The groups follow the order of their first row, and the rows of each the order you send them. A group's value is text, or `null` for none: the rows with none, or with an empty text `""`, form one group, titled "—".
- The group's field needs no column, and may not have one: its title shows it. A grouped grid keeps `indent` for how far a row sits in, so no column or group may use that field.
- An `indent` is 0, 1 or 2, and only a grouped grid takes one. A row sits under the nearest row above it, in its group, with a smaller indent.
- A sort never moves a group: it sorts the rows within each. Rows under the same row sort among themselves, and move with it.
- A heading row, such as Essentials, leaves its figures empty, with `null`: a figure there would count twice in the subtotal, once for the heading and once for its lines. A `count` subtotal counts the heading rows too, so count a column the headings leave empty.
- Search and filters hide rows, and the subtotals add up the rows left. A group with no row left is hidden. The search matches a group's title too.
- A page holds 25 rows, titles and subtotals aside. A group cut by a page shows its title again at the top of the next, and its subtotal, of all its rows, where it ends.

## What the check refuses

`check_app`, the tests and the server lay out every view and check it. Each refusal says what to write instead. The ones agents meet most:

- a Tailwind class (`flex`, `gap-4`, `mt-2`, `w-full`, `text-sm`): use Cliquet's `layout-` classes, daisyUI's text colours, or `<small>` and `<strong>`;
- `lg:`, `xl:` or `2xl:`: use `sm:` or `md:`;
- a mix that can't be read in the dark theme, such as `badge-soft badge-neutral` or `text-neutral`, and a `-content` text colour, such as `text-primary-content`, off its own colour;
- a field without a label: a placeholder isn't one, as it goes when the person types;
- two `btn-primary` in one form, a form without a submit button (but a `data-view` form whose one field is a named select), a button outside a form;
- a field whose `name` isn't an argument of the form's tool, or a required argument that neither a field nor `data-args` gives;
- a table of more than three columns that doesn't scroll with its rows' names pinned;
- a form calling an action the role may not use, a query or a built-in tool in `data-call`, a view in `data-call`, or an action in `data-view`;
- `{{ }}` inside a tag outside quotes, a tool named by a value, `style`, `on…` attributes, `<script>`, `<img>`, `<svg>`, and a link that isn't to a web or mail address;
- an icon whose name isn't one of Lucide's, a `<span data-icon>` with something inside, and a button or a link that shows only an icon without `aria-label`;
- a chart whose series don't have one value per label;
- a grid with a cell that doesn't fit its column, such as text in a number column or a date not written as `YYYY-MM-DD`, a row with a field no column shows, other than a grouped grid's group and `indent`, or a tool or a density it doesn't know;
- a column with a `tone` that isn't a number column, `tones` on one that isn't text, a tone it doesn't know, or both on one column;
- a grouped grid whose `group_by` field no row has, or a column shows, a group that isn't text or `null`, and an `indent` other than 0, 1 or 2, or in a grid without `group_by`.

## Testing

Test each view as "Testing views" in `read_guide("tests")` says. `check_app` lays out every view on the current data, once per role, and reports a view that fails or breaks a rule.

## Looking at it

Tests show that a view lays out. They don't show how it looks: a table can pass every test and be cut off on a phone. So look at every view before you publish.

Every tool below works on the preview, as step 7 of `read_guide("start")` says. For a new app, write made-up first data first, so that its screen isn't empty: see "First data" there.

**Take screenshots.** `take_screenshots(app, role)` returns each view of that role, and needs `role` when the app has several, as two images: one as wide as a conversation, 768 pixels, and one as wide as a phone or a side panel, 390 pixels, drawn as `try_view` draws them. Look at both. A picture shows at most the top 1,600 pixels of a screen, and the answer says when it cut one. So does `try_view`'s. Check that nothing is cut off, that numbers and labels read well, and that an empty list says so. Fix the view and take them again. Take them for each role whose screen differs. `take_screenshots(app, role, view)` takes one view only.

The pictures are in the light theme. A person whose assistant is in dark mode sees the dark theme, so before you publish, take them once with `theme="dark"` and check that every text still reads.

**Click through it.** `try_view(app, view, steps, role)` opens the view on a copy of the preview's data, runs your steps, and returns a picture, the calls the view made, and any error. Then it makes the view's actions again on the preview, in order, with the same arguments, and says for each whether the preview kept it. Each step is one of:

| Step | Does |
| --- | --- |
| `{"click": "Record receipt"}` | Clicks the button, link or tab that reads this |
| `{"fill": "Receipt", "with": "R-0200"}` | Types into the field with this label or placeholder |
| `{"choose": "Product", "option": "Glass teapot 0.8 l"}` | Picks the option of the select with this label |
| `{"wait": 500}` | Waits, in milliseconds, 5,000 at most |

For the board above, as its owner:

```json
[{"fill": "New task", "with": "Order tea"}, {"click": "Add"}]
```

The answer lists each call the screen made: the tool, its arguments, and its result or its refusal. The picture is the screen after the last step. A step that finds nothing stops there: the error says which step, and lists what is on the screen. An action the screen's copy refused changed nothing, so it isn't made again. An action made again runs at a new `ctx.now`, on the preview's data as it is then.

Try each form, including a case that must be refused: the refusal must show in its form. What the steps change is kept in the preview, so the next `try_view`, `try_call` or screenshot sees it. `width` is from 320 to 1280 pixels, 768 by default: `width=390` tries the view as wide as a phone. `theme="dark"` tries it in the dark theme.

**Show it to the person.** `show_preview(app, view, role)` draws the draft's view in the conversation, on the preview, and the person can click through it. Use it when they want to see the view before it is live. Give the view's `arguments` to open it in a given state. It needs a client that draws views, and says so when yours doesn't. After you edit app.py, show the view again: on a view shown before the edit, an action or a query is refused until the view is laid out again. The preview's link opens every view of the draft in the person's browser, as any role.

If you can't see images, call `show_preview` or give the person the preview's link, and ask them what they see. Don't say a view looks right unless someone, you included, looked.
