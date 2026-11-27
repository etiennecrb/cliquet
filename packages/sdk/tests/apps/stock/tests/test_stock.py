from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from app import (
    DeliveryLine,
    LastDelivery,
    ProductSales,
    Reorder,
    SaleLine,
    StockLevel,
    add_product,
    catalogue,
    delivery,
    known_products,
    last_delivery,
    never_negative,
    products,
    receive_delivery,
    record_receipt,
    record_sale,
    recorded_once,
    sales,
    set_price,
    shelves,
    shop,
    sold_today,
    stock,
    takings,
    till,
    to_reorder,
)

from cliquet.testing import Connection, Denied, InvalidArguments, World, given

NOW = datetime(2026, 11, 10, 18, 30, tzinfo=ZoneInfo("Europe/Paris"))

PRODUCTS = """\
sku,name,price,reorder_at
TEA-SEN,Sencha green tea 100 g,8.50,10
CUP-CER,Ceramic cup,7.20,6
"""

MOVEMENTS = """\
date,sku,quantity,kind,ref,unit_price
2026-10-01,TEA-SEN,20,delivery,LI-118/TEA-SEN,
2026-10-01,CUP-CER,8,delivery,LI-118/CUP-CER,
2026-10-03,TEA-SEN,-2,sale,R-0001/TEA-SEN,8.50
2026-11-04,CUP-CER,-3,sale,R-0006/CUP-CER,7.20
"""


def world() -> World:
    return given(shop, {"products.csv": PRODUCTS, "movements.csv": MOVEMENTS}, now=NOW)


def owner() -> Connection:
    return world().connect("owner")


def staff() -> Connection:
    return world().connect("staff")


def level(conn: Connection, sku: str) -> int:
    return next(s.quantity for s in conn.query(stock) if s.sku == sku)


def test_a_sale_lowers_stock() -> None:
    s = staff()
    s.call(record_sale, sku="TEA-SEN", quantity=3, receipt="R-0010").expect_ok()
    assert StockLevel(sku="TEA-SEN", name="Sencha green tea 100 g", quantity=15) in s.query(stock)


def test_a_delivery_raises_stock() -> None:
    s = staff()
    lines = [DeliveryLine(sku="CUP-CER", quantity=12)]
    s.call(receive_delivery, invoice="LI-131", lines=lines).expect_ok()
    assert level(s, "CUP-CER") == 17


def test_refuses_selling_more_than_in_stock() -> None:
    outcome = staff().call(record_sale, sku="CUP-CER", quantity=6, receipt="R-0010")
    # A refusal names the sale as the person knows it, not a line in a file.
    assert outcome.expect_refused(by=never_negative) == [
        "the sale of CUP-CER on receipt R-0010 (2026-11-10) would leave -1 in stock"
    ]


def test_a_sale_slipped_before_its_delivery_is_refused() -> None:
    # The total stays positive, but the cup was sold before it arrived.
    sale = "2026-09-30,CUP-CER,-1,sale,R-0000/CUP-CER,7.20\n"
    world().change(
        "movements.csv", replace=("2026-10-01,CUP-CER", sale + "2026-10-01,CUP-CER")
    ).expect_refused(by=never_negative)


def test_refuses_a_delivery_of_an_unknown_product() -> None:
    lines = [DeliveryLine(sku="TEA-OOL", quantity=10)]
    staff().call(receive_delivery, invoice="LI-131", lines=lines).expect_refused(by=known_products)


def test_refuses_the_same_receipt_twice() -> None:
    staff().call(record_sale, sku="TEA-SEN", quantity=2, receipt="R-0001").expect_refused(
        by=recorded_once
    )


def test_an_invoice_sent_again_is_skipped() -> None:
    s = staff()
    first = [DeliveryLine(sku="CUP-CER", quantity=5)]
    s.call(receive_delivery, invoice="LI-131", lines=first).expect_ok()
    again = [DeliveryLine(sku="CUP-CER", quantity=5), DeliveryLine(sku="TEA-SEN", quantity=4)]
    s.call(receive_delivery, invoice="LI-131", lines=again).expect_ok()
    assert (level(s, "CUP-CER"), level(s, "TEA-SEN")) == (10, 22)
    assert s.call(receive_delivery, invoice="LI-131", lines=again).commit is None


def test_last_delivery_says_where_to_resume() -> None:
    s = staff()
    assert s.query(last_delivery) == LastDelivery(invoice="LI-118", date="2026-10-01")
    lines = [DeliveryLine(sku="CUP-CER", quantity=5)]
    s.call(receive_delivery, invoice="LI-131", lines=lines).expect_ok()
    assert s.query(last_delivery) == LastDelivery(invoice="LI-131", date="2026-11-10")


def test_refuses_an_unknown_product_at_the_till() -> None:
    staff().call(record_sale, sku="TEA-OOL", quantity=1, receipt="R-0010").expect_refused(
        message="There is no product TEA-OOL. See `stock` for the skus."
    )


def test_a_sale_of_nothing_is_an_invalid_argument() -> None:
    with pytest.raises(InvalidArguments, match="greater than 0"):
        staff().call(record_sale, sku="TEA-SEN", quantity=0, receipt="R-0010")


def test_an_empty_delivery_is_an_invalid_argument() -> None:
    with pytest.raises(InvalidArguments, match="at least 1 item"):
        staff().call(receive_delivery, invoice="LI-131", lines=[])


def test_past_sales_keep_their_price() -> None:
    o = owner()
    o.call(set_price, sku="CUP-CER", price=Decimal("7.90")).expect_ok()
    o.call(record_sale, sku="CUP-CER", quantity=1, receipt="R-0011").expect_ok()
    assert o.query(sales, month="2026-11") == [
        ProductSales(sku="CUP-CER", name="Ceramic cup", quantity=4, revenue=Decimal("29.50"))
    ]


def test_a_price_of_zero_is_an_invalid_argument() -> None:
    with pytest.raises(InvalidArguments, match="greater than 0"):
        owner().call(set_price, sku="CUP-CER", price=Decimal("0"))


def test_staff_cannot_change_prices_or_see_revenue() -> None:
    s = staff()
    with pytest.raises(Denied):
        s.call(set_price, sku="CUP-CER", price=Decimal("1.00"))
    with pytest.raises(Denied):
        s.query(sales)


def test_a_new_product_starts_with_no_stock() -> None:
    o = owner()
    o.call(
        add_product, sku="TEA-ROO", name="Rooibos 100 g", price=Decimal("5.40"), reorder_at=8
    ).expect_ok()
    assert level(o, "TEA-ROO") == 0
    assert Reorder(sku="TEA-ROO", name="Rooibos 100 g", in_stock=0, sold_last_30_days=0) in o.query(
        to_reorder
    )


def test_refuses_an_existing_product() -> None:
    owner().call(
        add_product, sku="TEA-SEN", name="Sencha", price=Decimal("8.50"), reorder_at=10
    ).expect_refused(message="TEA-SEN already exists")


def test_sales_default_to_this_month() -> None:
    assert owner().query(sales) == [
        ProductSales(sku="CUP-CER", name="Ceramic cup", quantity=3, revenue=Decimal("21.60"))
    ]


def test_to_reorder_lists_products_at_their_reorder_point() -> None:
    assert staff().query(to_reorder) == [
        Reorder(sku="CUP-CER", name="Ceramic cup", in_stock=5, sold_last_30_days=3)
    ]


def test_a_new_shop_starts_with_no_files() -> None:
    owner = given(shop, {}, now=NOW).connect("owner")
    owner.call(
        add_product,
        sku="TEA-SEN",
        name="Sencha green tea 100 g",
        price=Decimal("8.50"),
        reorder_at=10,
    ).expect_ok()
    lines = [DeliveryLine(sku="TEA-SEN", quantity=20)]
    owner.call(receive_delivery, invoice="LI-001", lines=lines).expect_ok()
    owner.call(record_sale, sku="TEA-SEN", quantity=2, receipt="R-0001").expect_ok()
    assert level(owner, "TEA-SEN") == 18


def test_the_shelves_show_the_stock_and_what_to_reorder() -> None:
    # A screen's text is in its body: `str(screen)` holds it all.
    screen = str(staff().view(shelves))
    assert "['td', {'class': 'layout-end'}, '18']" in screen and "5 left" in screen
    assert "Ceramic cup" in screen and "Receive a delivery" in screen
    found = staff().view(shelves, search="cup")
    assert found["arguments"] == {"search": "cup"}
    assert "['td', {}, 'Sencha green tea 100 g']" not in str(found)


def test_the_shelves_open_on_an_empty_shop() -> None:
    screen = str(given(shop, {}, now=NOW).connect("staff").view(shelves))
    assert "Every product is above its reorder point." in screen


def test_a_delivery_goes_from_its_invoice_to_its_lines_to_done() -> None:
    conn = staff()
    first = conn.view(delivery)
    assert first["arguments"] == {} and "Invoice number" in str(first)
    # "Next" without an invoice stays on the first step.
    assert conn.view(delivery, step="lines")["arguments"] == {}
    lines = conn.view(delivery, step="lines", invoice="LI-140", sku="TEA-SEN", quantity=4)
    assert lines["arguments"] == {
        "step": "lines",
        "invoice": "LI-140",
        "lines": [{"sku": "TEA-SEN", "quantity": 4}],
    }
    assert "Sencha green tea 100 g" in str(lines) and "receive_delivery" in str(lines)
    done = conn.view(delivery, step="done", invoice="LI-140", lines=lines["arguments"]["lines"])
    assert "LI-140: 1 product received." in str(done)


def test_the_till_builds_a_receipt_line_by_line() -> None:
    conn = staff()
    one = conn.view(till, sku="TEA-SEN", quantity=2)
    # The arguments draw the screen again with the line, never with the line to add.
    assert one["arguments"] == {"lines": [{"sku": "TEA-SEN", "quantity": 2}]}
    two = conn.view(till, lines=one["arguments"]["lines"], sku="TEA-SEN", quantity=1)
    assert two["arguments"] == {"lines": [{"sku": "TEA-SEN", "quantity": 3}]}
    assert "3 units" in str(two)
    # An unknown product adds nothing.
    assert conn.view(till, sku="NOPE")["arguments"] == {}
    with pytest.raises(InvalidArguments):
        conn.view(till, quantity=0)


def test_the_till_shows_what_sold_today() -> None:
    conn = staff()
    assert "Nothing sold yet today." in str(conn.view(till))
    conn.call(record_sale, sku="TEA-SEN", quantity=2, receipt="R-0100").expect_ok()
    assert [(s.receipt, s.name, s.quantity) for s in conn.query(sold_today)] == [
        ("R-0100", "Sencha green tea 100 g", 2)
    ]
    screen = str(conn.view(till))
    assert "Receipt R-0100" in screen and "2 sold" in screen
    assert "record_receipt" in screen


def test_a_receipt_is_recorded_at_once() -> None:
    conn = staff()
    lines = [SaleLine(sku="TEA-SEN", quantity=2), SaleLine(sku="CUP-CER", quantity=1)]
    conn.call(record_receipt, receipt="R-0101", lines=lines).expect_ok()
    assert level(conn, "TEA-SEN") == 16 and level(conn, "CUP-CER") == 4
    # The same receipt again is refused as a whole: nothing of it is recorded twice.
    conn.call(record_receipt, receipt="R-0101", lines=lines).expect_refused(
        message="Receipt R-0101 was already recorded on 10 November"
    )
    assert level(conn, "TEA-SEN") == 16


def test_a_receipt_needs_a_line() -> None:
    with pytest.raises(InvalidArguments):
        staff().call(record_receipt, receipt="R-0102", lines=[])


def test_the_catalogue_and_the_takings_are_for_the_owner() -> None:
    o, s = owner(), staff()
    assert [p.name for p in o.query(products)] == ["Sencha green tea 100 g", "Ceramic cup"]
    assert "set_price" in str(o.view(catalogue)) and "add_product" in str(o.view(catalogue))
    assert "Ceramic cup" in str(o.view(takings))
    october = o.view(takings, month="2026-10")
    assert october["arguments"] == {"month": "2026-10"} and "€17.00" in str(october)
    for view in (catalogue, takings):
        with pytest.raises(Denied):
            s.view(view)
