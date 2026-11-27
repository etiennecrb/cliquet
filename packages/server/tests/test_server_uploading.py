"""A person's own data, uploaded from the web, then read through the app: the whole path."""

import pytest

from cliquet import screenshot

from .server_helpers import draft_example, draft_files, signed_in

pytestmark = [pytest.mark.browser, pytest.mark.slow, pytest.mark.sandbox]

STEPS = [
    {"fill": "Receipt", "with": "R-0900"},
    {"choose": "Product", "option": "Sencha green tea 100 g"},
    {"click": "Add to receipt"},
    {"click": "Record receipt"},
]


def movements(count: int) -> bytes:
    """A made-up stock history: one delivery of each tea per line."""
    lines = ["date,sku,quantity,kind,ref,unit_price"]
    lines += [f"2026-10-01,TEA-SEN,1,delivery,LI-{number:06d}/TEA-SEN," for number in range(count)]
    return ("\n".join(lines) + "\n").encode()


def stock_of(result: dict, sku: str) -> int:
    rows = result["result"]
    if isinstance(rows, dict) and "result" in rows:
        rows = rows["result"]
    return next(row["quantity"] for row in rows if row["sku"] == sku)


@pytest.mark.skipif(screenshot.find_browser() is None, reason="needs a Chrome or Chromium")
def test_an_uploaded_file_goes_all_the_way_to_publish(server) -> None:
    builder = server.state.builder
    draft_example(builder, "store")
    content = movements(13_000)
    assert len(content) > 600_000

    with signed_in(server) as client:
        token = server.state.web_auth.csrf_for_session(client.cookies.get("session"))
        sent = client.post(
            "/build/store/upload",
            data={"csrf": token, "path": "data/movements.csv"},
            files={"file": ("movements.csv", content, "text/csv")},
            follow_redirects=False,
        )
        assert sent.status_code == 303, sent.text
        assert draft_files(builder.drafts, "etienne", "store")["data/movements.csv"] == content

        checked = builder.check_app("etienne", "store")
        assert checked["ok"] is True, checked["problems"]
        builder.start_preview("etienne", "store")
        called = builder.try_call("etienne", "store", "stock", role="owner")
        assert stock_of(called, "TEA-SEN") == 13_000
        tried = builder.try_view("etienne", "store", "till", STEPS, role="staff")
        assert tried.error is None
        published = builder.publish_app("etienne", "store", "First data from an upload")

    assert published["published"] is True, published
    live = server.state.apps.repo("etienne", "store").data(["movements.csv"])["movements.csv"]
    assert live == content
    page = builder.read_file("etienne", "store", "data/movements.csv", offset=13_001)
    assert page["lines"] == 13_001
    assert "next" not in page
