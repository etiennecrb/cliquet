# /// script
# requires-python = ">=3.14"
# dependencies = ["cliquet-sdk>=0.0.1,<0.1"]
#
# [tool.uv.sources]
# cliquet-sdk = { path = "../../..", editable = true }
# ///
"""Stock for a small tea shop: products, deliveries and sales, kept by an agent."""

import csv
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

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

PRODUCTS = "products.csv"
MOVEMENTS = "movements.csv"
COLUMNS = {
    PRODUCTS: ["sku", "name", "price", "reorder_at"],
    MOVEMENTS: ["date", "sku", "quantity", "kind", "ref", "unit_price"],
}

shop = App(
    "shop",
    files=[PRODUCTS, MOVEMENTS],
    roles=["owner", "staff"],
    accent="#0f766e",
    timezone="Europe/Paris",
)

# Argument types: what the schema tells agents, and what is checked before any action runs.
Sku = Annotated[str, Field(description="A product's sku, such as TEA-SEN, as listed by `stock`")]
Units = Annotated[int, Field(gt=0, description="A number of units, more than zero")]
Price = Annotated[
    Decimal, Field(gt=0, decimal_places=2, description="A price in euros, such as 8.50")
]
Month = Annotated[str, Field(pattern=r"^\d{4}-\d{2}$", description="A month, as YYYY-MM")]


class DeliveryLine(BaseModel):
    sku: Sku
    quantity: Units


class StockLevel(BaseModel):
    sku: str
    name: str
    quantity: int


class SaleLine(BaseModel):
    sku: Sku
    quantity: Units


class Product(BaseModel):
    sku: str
    name: str
    price: Annotated[Decimal, Field(description="Euros")]
    reorder_at: int


class ProductSales(BaseModel):
    sku: str
    name: str
    quantity: int
    revenue: Annotated[Decimal, Field(description="Euros, at the price of each sale")]


class LastDelivery(BaseModel):
    invoice: str
    date: Annotated[str, Field(description="YYYY-MM-DD")]


class Sale(BaseModel):
    receipt: str
    sku: str
    name: str
    quantity: int


class Reorder(BaseModel):
    sku: str
    name: str
    in_stock: int
    sold_last_30_days: int


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


def _prices() -> dict[str, Decimal]:
    return {p["sku"]: Decimal(p["price"]) for p in _read(PRODUCTS)}


def _move(ctx: ActionContext, sku: str, units: int, kind: str, ref: str, price: str = "") -> None:
    """Append a movement, dated now, so the file stays in time order."""
    _append(MOVEMENTS, [ctx.now.date().isoformat(), sku, units, kind, f"{ref}/{sku}", price])


def _describe(m: dict[str, str]) -> str:
    """How a refusal names a movement: by its document, as the person knows it."""
    document = m["ref"].split("/")[0]
    if m["kind"] == "sale":
        return f"the sale of {m['sku']} on receipt {document} ({m['date']})"
    return f"the delivery of {m['sku']} on invoice {document} ({m['date']})"


Receipt = Annotated[str, Field(pattern=r"\S", description="The till receipt's number")]


def _sell(ctx: ActionContext, receipt: str, sku: str, quantity: int) -> None:
    """Record one product of a receipt, at the product's current price."""
    prices = _prices()
    if sku not in prices:
        raise Refused(f"There is no product {sku}. See `stock` for the skus.")
    _move(ctx, sku, -quantity, "sale", receipt, f"{prices[sku]:.2f}")


@shop.action(roles=["owner", "staff"])
def record_receipt(
    ctx: ActionContext,
    receipt: Receipt,
    lines: Annotated[list[SaleLine], Field(min_length=1, description="One line per product")],
) -> None:
    """Record a till receipt: every product on it, at once."""
    # Refuse early, in the person's words. The invariant still guards every other way in.
    done = [m["date"] for m in _read(MOVEMENTS) if m["ref"].split("/")[0] == receipt]
    if done:
        day = date.fromisoformat(done[0])
        raise Refused(f"Receipt {receipt} was already recorded on {day.day} {day:%B}")
    for line in lines:
        _sell(ctx, receipt, line.sku, line.quantity)


@shop.action(roles=["owner", "staff"])
def record_sale(ctx: ActionContext, sku: Sku, quantity: Units, receipt: Receipt) -> None:
    """Record one product of a receipt. For a whole receipt, use `record_receipt`."""
    _sell(ctx, receipt, sku, quantity)


@shop.action(roles=["owner", "staff"])
def receive_delivery(
    ctx: ActionContext,
    invoice: Annotated[str, Field(pattern=r"\S", description="The supplier invoice's number")],
    lines: Annotated[list[DeliveryLine], Field(min_length=1, description="One line per product")],
) -> None:
    """Record a supplier delivery, one line per product on the invoice.

    Lines already recorded are skipped, so sending an invoice again is harmless.
    """
    recorded = {m["ref"] for m in _read(MOVEMENTS)}
    for line in lines:
        if f"{invoice}/{line.sku}" not in recorded:
            _move(ctx, line.sku, line.quantity, "delivery", invoice)


@shop.action(roles=["owner"])
def add_product(
    ctx: ActionContext,
    sku: Annotated[
        str, Field(pattern=r"^[A-Z]+-[A-Z]+$", description="A new sku, such as TEA-OOL")
    ],
    name: Annotated[str, Field(pattern=r"\S", description="The name shown to customers")],
    price: Price,
    reorder_at: Annotated[int, Field(ge=0, description="Reorder when stock falls to this or less")],
) -> None:
    """Add a product to the catalogue. It starts with no stock."""
    if sku in _prices():
        raise Refused(f"{sku} already exists")
    _append(PRODUCTS, [sku, name, f"{price:.2f}", reorder_at])


@shop.action(roles=["owner"])
def set_price(ctx: ActionContext, sku: Sku, price: Price) -> None:
    """Change a product's price. Past sales keep the price they were sold at."""
    products = _read(PRODUCTS)
    if sku not in {p["sku"] for p in products}:
        raise Refused(f"There is no product {sku}. See `stock` for the skus.")
    with open(PRODUCTS, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS[PRODUCTS])
        writer.writeheader()
        writer.writerows({**p, "price": f"{price:.2f}"} if p["sku"] == sku else p for p in products)


@shop.invariant("Stock never goes below zero")
def never_negative(ctx: InvariantContext) -> list[str]:
    # Movements are dated when recorded, so the file is in time order.
    level: dict[str, int] = defaultdict(int)
    problems = []
    for m in _read(MOVEMENTS):
        level[m["sku"]] += int(m["quantity"])
        if level[m["sku"]] < 0:
            problems.append(f"{_describe(m)} would leave {level[m['sku']]} in stock")
    return problems


@shop.invariant("Every movement is for a known product")
def known_products(ctx: InvariantContext) -> list[str]:
    known = {p["sku"] for p in _read(PRODUCTS)}
    return [
        f"{_describe(m)}: {m['sku']} isn't in the catalogue"
        for m in _read(MOVEMENTS)
        if m["sku"] not in known
    ]


@shop.invariant("No receipt or invoice line is recorded twice")
def recorded_once(ctx: InvariantContext) -> list[str]:
    first: dict[str, str] = {}
    problems = []
    for m in _read(MOVEMENTS):
        if m["ref"] in first:
            problems.append(f"{_describe(m)} was already recorded on {first[m['ref']]}")
        first.setdefault(m["ref"], m["date"])
    return problems


@shop.query(roles=["owner", "staff"])
def stock(ctx: QueryContext) -> list[StockLevel]:
    """Units in stock, per product."""
    level: dict[str, int] = defaultdict(int)
    for m in _read(MOVEMENTS):
        level[m["sku"]] += int(m["quantity"])
    return [
        StockLevel(sku=p["sku"], name=p["name"], quantity=level[p["sku"]]) for p in _read(PRODUCTS)
    ]


@shop.query(roles=["owner", "staff"])
def last_delivery(ctx: QueryContext) -> LastDelivery | None:
    """The latest delivery recorded: where to resume when importing invoices."""
    deliveries = [m for m in _read(MOVEMENTS) if m["kind"] == "delivery"]
    if not deliveries:
        return None
    last = deliveries[-1]
    return LastDelivery(invoice=last["ref"].split("/")[0], date=last["date"])


@shop.query(roles=["owner"])
def sales(ctx: QueryContext, month: Month | None = None) -> list[ProductSales]:
    """Units sold and revenue per product for a month, this month by default."""
    month = month or ctx.now.strftime("%Y-%m")
    names = {p["sku"]: p["name"] for p in _read(PRODUCTS)}
    units: dict[str, int] = defaultdict(int)
    revenue: dict[str, Decimal] = defaultdict(Decimal)
    for m in _read(MOVEMENTS):
        if m["kind"] == "sale" and m["date"].startswith(month):
            units[m["sku"]] -= int(m["quantity"])
            revenue[m["sku"]] -= int(m["quantity"]) * Decimal(m["unit_price"])
    return [
        ProductSales(sku=s, name=names.get(s, s), quantity=units[s], revenue=revenue[s])
        for s in sorted(units)
    ]


@shop.query(roles=["owner"])
def products(ctx: QueryContext) -> list[Product]:
    """The catalogue: each product's sku, name, price and reorder point."""
    return [
        Product(
            sku=p["sku"], name=p["name"], price=Decimal(p["price"]), reorder_at=int(p["reorder_at"])
        )
        for p in _read(PRODUCTS)
    ]


@shop.query(roles=["owner", "staff"])
def sold_today(ctx: QueryContext) -> list[Sale]:
    """Each product sold today, the latest first, with its receipt."""
    today = ctx.now.date().isoformat()
    names = {p["sku"]: p["name"] for p in _read(PRODUCTS)}
    return [
        Sale(
            receipt=m["ref"].split("/")[0],
            sku=m["sku"],
            name=names.get(m["sku"], m["sku"]),
            quantity=-int(m["quantity"]),
        )
        for m in reversed(_read(MOVEMENTS))
        if m["kind"] == "sale" and m["date"] == today
    ]


@shop.query(roles=["owner", "staff"])
def to_reorder(ctx: QueryContext) -> list[Reorder]:
    """Products at or below their reorder point, with how many sold in the last 30 days."""
    since = (ctx.now - timedelta(days=30)).date().isoformat()
    in_stock = {s.sku: s.quantity for s in stock(ctx)}
    sold: dict[str, int] = defaultdict(int)
    for m in _read(MOVEMENTS):
        if m["kind"] == "sale" and m["date"] > since:
            sold[m["sku"]] -= int(m["quantity"])
    return [
        Reorder(
            sku=p["sku"],
            name=p["name"],
            in_stock=in_stock[p["sku"]],
            sold_last_30_days=sold[p["sku"]],
        )
        for p in _read(PRODUCTS)
        if in_stock[p["sku"]] <= int(p["reorder_at"])
    ]


# The screens. Each is a Jinja template of HTML, written with daisyUI's components and
# Cliquet's layout- classes. Every value is escaped: data never becomes markup.


def _euros(amount: Decimal) -> str:
    return f"€{amount:,.2f}"


def _month(month: str) -> str:
    """A month as people write it: 2026-11 is November 2026."""
    return date.fromisoformat(f"{month}-01").strftime("%B %Y")


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


# One view per job: the till, the shelves and a delivery for everyone, then the takings and
# the catalogue for the owner.

TILL = """
<header>
  <h2>Till</h2>
</header>
<section class="card card-border">
  <div class="card-body">
    <h3 class="card-title">This receipt</h3>
    <form class="layout-stack layout-gap-3" data-view>
      <div class="layout-grid layout-cols-3 layout-gap-3">
        <fieldset class="fieldset layout-span-2">
          <legend class="fieldset-legend">Product</legend>
          <select class="select" name="sku" required>
            <option value="" disabled selected>Choose a product</option>
            {% for sku, name in names.items() %}
            <option value="{{ sku }}">{{ name }}</option>
            {% endfor %}
          </select>
        </fieldset>
        <fieldset class="fieldset">
          <legend class="fieldset-legend">Units</legend>
          <input class="input" type="number" name="quantity" value="1" min="1" required>
        </fieldset>
      </div>
      <button class="btn btn-outline">Add to receipt</button>
    </form>
    {% if lines %}
    <ul class="list">
      {% for line in lines %}
      <li class="list-row">
        <span class="list-col-grow">{{ names[line.sku] }}</span>
        <span class="badge badge-soft">{{ plural(line.quantity, "unit") }}</span>
      </li>
      {% endfor %}
    </ul>
    {% else %}
    <p class="text-base-content/60">No product on this receipt yet.</p>
    {% endif %}
    <form class="layout-stack layout-gap-3" data-call="record_receipt"
          data-args='{{ {"lines": lines} | tojson }}' data-then='{"lines": []}'>
      <fieldset class="fieldset">
        <legend class="fieldset-legend">Receipt</legend>
        <input class="input" name="receipt" placeholder="Such as R-0013" required>
      </fieldset>
      <button class="btn btn-primary" {% if not lines %}disabled{% endif %}>Record receipt</button>
    </form>
  </div>
</section>
<section class="card card-border">
  <div class="card-body">
    <h3 class="card-title">Sold today</h3>
    {% if sold %}
    <ul class="list">
      {% for sale in sold %}
      <li class="list-row">
        <div class="list-col-grow">
          <div>{{ sale.name }}</div>
          <small class="text-base-content/60">Receipt {{ sale.receipt }}</small>
        </div>
        <span class="badge badge-soft">{{ sale.quantity }} sold</span>
      </li>
      {% endfor %}
    </ul>
    {% else %}
    <p class="text-base-content/60">Nothing sold yet today.</p>
    {% endif %}
  </div>
</section>
"""


@shop.view(roles=["owner", "staff"])
def till(
    ctx: QueryContext,
    lines: tuple[SaleLine, ...] = (),
    sku: Annotated[str, Field(description="A product to add to the receipt")] = "",
    quantity: Annotated[int, Field(ge=1)] = 1,
) -> Screen:
    """The till: add each product of a receipt, record it, and see what sold today."""
    names = {p["sku"]: p["name"] for p in _read(PRODUCTS)}
    if sku in names:  # "Add to receipt" was pressed: one more line, or more of a line
        added = {line.sku: line.quantity for line in lines}
        added[sku] = added.get(sku, 0) + quantity
        lines = tuple(SaleLine(sku=s, quantity=q) for s, q in added.items())
    # `args` draw the screen again as it is: the lines, never the line just added.
    return Screen(
        TILL, args={"lines": lines}, lines=lines, names=names, sold=sold_today(ctx), plural=_plural
    )


SHELVES = """
<header>
  <h2>Shelves</h2>
</header>
<div class="stats card-border">
  <div class="stat">
    <div class="stat-title">Products</div>
    <div class="stat-value">{{ levels | length }}</div>
  </div>
  <div class="stat">
    <div class="stat-title">To reorder</div>
    <div class="stat-value">{{ reorder | length }}</div>
  </div>
  <div class="stat">
    <div class="stat-title">Sold out</div>
    <div class="stat-value">{{ sold_out }}</div>
  </div>
</div>
<section class="card card-border">
  <div class="card-body">
    <h3 class="card-title">To reorder</h3>
    <p class="text-base-content/60">At or below their reorder point</p>
    {% if reorder %}
    <ul class="list">
      {% for row in reorder %}
      <li class="list-row">
        <div class="list-col-grow">
          <div>{{ row.name }}</div>
          <small class="text-base-content/60">{{ row.sold_last_30_days }} sold in 30 days</small>
        </div>
        <span class="badge badge-warning badge-soft">{{ row.in_stock }} left</span>
      </li>
      {% endfor %}
    </ul>
    <a class="link link-primary" data-view="delivery">Receive a delivery</a>
    {% else %}
    <p class="text-base-content/60">Every product is above its reorder point.</p>
    {% endif %}
  </div>
</section>
<section class="card card-border">
  <div class="card-body">
    <h3 class="card-title">In stock</h3>
    <form class="join" data-view>
      <input class="input join-item" type="search" name="search" value="{{ search }}"
             placeholder="Product name" aria-label="Find a product">
      <button class="btn join-item">Find</button>
    </form>
    {% if shown %}
    <table class="table">
      <thead><tr><th>Product</th><th class="layout-end">Units</th></tr></thead>
      <tbody>
        {% for row in shown %}
        <tr><td>{{ row.name }}</td><td class="layout-end">{{ row.quantity }}</td></tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <p class="text-base-content/60">No product matches “{{ search }}”.</p>
    {% endif %}
  </div>
</section>
"""


@shop.view(roles=["owner", "staff"])
def shelves(
    ctx: QueryContext,
    search: Annotated[str, Field(description="Part of a product's name")] = "",
) -> Screen:
    """The shelves: what to reorder, and what is in stock."""
    levels = stock(ctx)
    shown = [s for s in levels if search.casefold() in s.name.casefold()]
    return Screen(
        SHELVES,
        args={"search": search},
        levels=levels,
        shown=shown,
        search=search,
        reorder=to_reorder(ctx),
        sold_out=sum(1 for s in levels if not s.quantity),
    )


DELIVERY = """
<header>
  <h2>Receive a delivery</h2>
</header>
<ul class="steps">
  <li class="step step-primary">Invoice</li>
  <li class="step {{ 'step-primary' if step != 'invoice' }}">Products</li>
  <li class="step {{ 'step-primary' if step == 'done' }}">Recorded</li>
</ul>
{% if step == "invoice" %}
<section class="card card-border">
  <form class="card-body layout-stack layout-gap-3" data-view data-args='{"step": "lines"}'>
    <h3 class="card-title">The invoice</h3>
    <fieldset class="fieldset">
      <legend class="fieldset-legend">Invoice number</legend>
      <input class="input" name="invoice" value="{{ invoice }}" placeholder="Such as LI-2026-140"
             required>
    </fieldset>
    <button class="btn btn-primary">Next</button>
  </form>
</section>
{% elif step == "lines" %}
<section class="card card-border">
  <div class="card-body">
    <h3 class="card-title">The products on {{ invoice }}</h3>
    {% if lines %}
    <ul class="list">
      {% for line in lines %}
      <li class="list-row">
        <span class="list-col-grow">{{ names[line.sku] }}</span>
        <span class="badge badge-soft">{{ plural(line.quantity, "unit") }}</span>
      </li>
      {% endfor %}
    </ul>
    {% else %}
    <p class="text-base-content/60">No product yet. Add each line of the invoice.</p>
    {% endif %}
    <form class="layout-stack layout-gap-3" data-view>
      <div class="layout-grid layout-cols-3 layout-gap-3">
        <fieldset class="fieldset layout-span-2">
          <legend class="fieldset-legend">Product</legend>
          <select class="select" name="sku" required>
            <option value="" disabled selected>Choose a product</option>
            {% for sku, name in names.items() %}
            <option value="{{ sku }}">{{ name }}</option>
            {% endfor %}
          </select>
        </fieldset>
        <fieldset class="fieldset">
          <legend class="fieldset-legend">Units</legend>
          <input class="input" type="number" name="quantity" value="1" min="1" required>
        </fieldset>
      </div>
      <button class="btn btn-outline">Add line</button>
    </form>
    <form class="layout-row layout-between" data-call="receive_delivery"
          data-args='{{ {"invoice": invoice, "lines": lines} | tojson }}'
          data-then='{"step": "done"}'>
      <a class="btn btn-ghost" data-view data-args='{"step": "invoice"}'>Back</a>
      <button class="btn btn-primary" {% if not lines %}disabled{% endif %}>Record delivery</button>
    </form>
  </div>
</section>
{% else %}
<section class="card card-border">
  <div class="card-body">
    <h3 class="card-title">Recorded</h3>
    <p>{{ invoice }}: {{ plural(lines | length, "product") }} received. The stock is up to date.</p>
    <a class="btn btn-outline" data-view="delivery">Receive another</a>
  </div>
</section>
{% endif %}
"""


@shop.view(roles=["owner", "staff"])
def delivery(
    ctx: QueryContext,
    step: Literal["invoice", "lines", "done"] = "invoice",
    invoice: str = "",
    lines: tuple[DeliveryLine, ...] = (),
    sku: Annotated[str, Field(description="A product to add to the delivery")] = "",
    quantity: Annotated[int, Field(ge=1)] = 1,
) -> Screen:
    """Receive a supplier delivery, in three steps: the invoice, its lines, and done."""
    names = {p["sku"]: p["name"] for p in _read(PRODUCTS)}
    if sku in names:  # "Add line" was pressed
        added = {line.sku: line.quantity for line in lines}
        added[sku] = added.get(sku, 0) + quantity
        lines = tuple(DeliveryLine(sku=s, quantity=q) for s, q in added.items())
    if step == "lines" and not invoice.strip():
        step = "invoice"
    return Screen(
        DELIVERY,
        args={"step": step, "invoice": invoice, "lines": lines},
        step=step,
        invoice=invoice,
        lines=lines,
        names=names,
        plural=_plural,
    )


TAKINGS = """
<header>
  <h2>Takings</h2>
  <p class="text-base-content/60">Up to {{ today }}</p>
</header>
<nav role="tablist" class="tabs tabs-border">
  {% for one in months %}
  <a role="tab" class="tab {{ 'tab-active' if one == month }}" data-view
     data-args='{{ {"month": one} | tojson }}'>{{ month_name(one) }}</a>
  {% endfor %}
</nav>
<div class="stats card-border">
  <div class="stat">
    <div class="stat-title">Takings</div>
    <div class="stat-value">{{ euros(total) }}</div>
    <div class="stat-desc">{{ period }}</div>
  </div>
  <div class="stat">
    <div class="stat-title">Units sold</div>
    <div class="stat-value">{{ units }}</div>
    <div class="stat-desc">On {{ plural(receipts, "receipt") }}</div>
  </div>
</div>
<section class="card card-border">
  <div class="card-body">
    <h3 class="card-title">By product</h3>
    <p class="text-base-content/60">The best sellers first</p>
    {% if rows %}
    <table class="table">
      <thead>
        <tr><th>Product</th><th class="layout-end">Sold</th><th class="layout-end">Takings</th></tr>
      </thead>
      <tbody>
        {% for row in rows %}
        <tr>
          <td>{{ row.name }}</td>
          <td class="layout-end">{{ row.quantity }}</td>
          <td class="layout-end">{{ euros(row.revenue) }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <p class="text-base-content/60">Nothing sold in {{ month_name(month) }}.</p>
    {% endif %}
  </div>
</section>
"""


@shop.view(roles=["owner"])
def takings(ctx: QueryContext, month: Month | None = None) -> Screen:
    """The takings: what sold and for how much, this month or last."""
    this = ctx.now.strftime("%Y-%m")
    last = (ctx.now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    month = month or this
    rows = sorted(sales(ctx, month), key=lambda r: r.revenue, reverse=True)
    receipts = {
        m["ref"].split("/")[0]
        for m in _read(MOVEMENTS)
        if m["kind"] == "sale" and m["date"].startswith(month)
    }
    period = f"So far, to {ctx.now.day} {ctx.now:%B}" if month == this else "The whole month"
    return Screen(
        TAKINGS,
        args={"month": month},
        month=month,
        months=[this, last],
        today=f"{ctx.now.day} {ctx.now:%B %Y}",
        rows=rows,
        total=sum((r.revenue for r in rows), Decimal()),
        units=sum(r.quantity for r in rows),
        receipts=len(receipts),
        period=period,
        euros=_euros,
        plural=_plural,
        month_name=_month,
    )


CATALOGUE = """
<header>
  <h2>Catalogue</h2>
</header>
<div role="tablist" class="tabs tabs-border">
  <input type="radio" name="section" class="tab" aria-label="Products" checked>
  <div class="tab-content">
    <ul class="list">
      {% for product in products %}
      <li class="list-row">
        <div class="list-col-grow">
          <div>{{ product.name }}</div>
          <small class="text-base-content/60">Reorder at {{ product.reorder_at }} left</small>
        </div>
        <span class="badge badge-soft">{{ euros(product.price) }}</span>
      </li>
      {% endfor %}
    </ul>
  </div>
  <input type="radio" name="section" class="tab" aria-label="Add a product">
  <div class="tab-content">
    <form class="layout-stack layout-gap-3" data-call="add_product">
      <p class="text-base-content/60">It starts with no stock.</p>
      <div class="layout-grid layout-cols-3 layout-gap-3">
        <fieldset class="fieldset">
          <legend class="fieldset-legend">Sku</legend>
          <input class="input" name="sku" placeholder="TEA-OOL" required>
        </fieldset>
        <fieldset class="fieldset layout-span-2">
          <legend class="fieldset-legend">Name</legend>
          <input class="input" name="name" placeholder="Such as Oolong tea 100 g" required>
        </fieldset>
      </div>
      <div class="layout-grid layout-cols-2 layout-gap-3">
        <fieldset class="fieldset">
          <legend class="fieldset-legend">Price in euros</legend>
          <input class="input" name="price" inputmode="decimal" placeholder="9.20" required>
        </fieldset>
        <fieldset class="fieldset">
          <legend class="fieldset-legend">Reorder at</legend>
          <input class="input" type="number" name="reorder_at" min="0" placeholder="10" required>
        </fieldset>
      </div>
      <button class="btn btn-primary">Add product</button>
    </form>
  </div>
  <input type="radio" name="section" class="tab" aria-label="Change a price">
  <div class="tab-content">
    <form class="layout-stack layout-gap-3" data-call="set_price">
      <p class="text-base-content/60">Past sales keep the price they were sold at.</p>
      <fieldset class="fieldset">
        <legend class="fieldset-legend">Product</legend>
        <select class="select" name="sku" required>
          <option value="" disabled selected>Choose a product</option>
          {% for product in products %}
          <option value="{{ product.sku }}">{{ product.name }} ({{ euros(product.price) }})</option>
          {% endfor %}
        </select>
      </fieldset>
      <fieldset class="fieldset">
        <legend class="fieldset-legend">New price in euros</legend>
        <input class="input" name="price" inputmode="decimal" placeholder="8.90" required>
      </fieldset>
      <button class="btn btn-primary">Change price</button>
    </form>
  </div>
</div>
"""


@shop.view(roles=["owner"])
def catalogue(ctx: QueryContext) -> Screen:
    """The catalogue: the products and their prices, a new product, and a new price."""
    return Screen(CATALOGUE, products=products(ctx), euros=_euros)
