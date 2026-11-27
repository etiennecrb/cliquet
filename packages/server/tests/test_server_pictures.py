import base64
import re

import anyio
import pytest
from fastmcp import Client as BuilderClient

from cliquet import screenshot
from cliquet_server.builder import Builder, BuilderError
from cliquet_server.builder.trying import acting_role
from cliquet_server.executor import CaptureFailed, Picture, SandboxClient, SandboxError, Scripted

from .server_helpers import draft_example, me_server

pytestmark = [pytest.mark.browser, pytest.mark.slow]

# A picture as the sandbox answers one, for a test that needs no browser.
PNG = b"\x89PNG\r\n\x1a\n" + bytes(16)

needs_browser = pytest.mark.skipif(
    screenshot.find_browser() is None, reason="needs a Chrome, Chromium or Edge"
)


def test_role() -> None:
    assert acting_role({"name": "x", "roles": []}, None) is None
    with pytest.raises(BuilderError, match="has no roles"):
        acting_role({"name": "x", "roles": []}, "owner")
    assert acting_role({"name": "x", "roles": ["owner"]}, None) == "owner"
    assert acting_role({"name": "x", "roles": ["owner"]}, "owner") == "owner"
    with pytest.raises(BuilderError, match="owner, staff"):
        acting_role({"name": "x", "roles": ["owner", "staff"]}, None)
    assert acting_role({"name": "x", "roles": ["owner", "staff"]}, "staff") == "staff"
    with pytest.raises(BuilderError, match="owner, staff"):
        acting_role({"name": "x", "roles": ["owner", "staff"]}, "nobody")
    # The one role helper's words, as a sentence: the names keep their case.
    with pytest.raises(BuilderError) as wrong:
        acting_role({"name": "x", "roles": ["Owner", "Staff"]}, "Boss")
    assert str(wrong.value) == "The app has no role Boss. Its roles are: Owner, Staff."


@needs_browser
def test_screenshots_of_a_draft(builder: Builder) -> None:
    draft_example(builder, "shop")
    builder.start_preview("etienne", "shop")

    pictures, notice = builder.take_screenshots("etienne", "shop", role="staff")

    assert notice is None
    assert all(isinstance(picture, Picture) for picture in pictures)
    assert [picture.view for picture in pictures] == [
        "till",
        "till",
        "shelves",
        "shelves",
        "delivery",
        "delivery",
    ]
    assert [picture.width for picture in pictures] == [768, 390] * 3
    assert all(picture.png[:8] == b"\x89PNG\r\n\x1a\n" for picture in pictures)


def test_screenshots_need_a_role_when_there_are_several(builder: Builder) -> None:
    draft_example(builder, "shop")
    builder.start_preview("etienne", "shop")

    with pytest.raises(BuilderError, match="owner, staff"):
        builder.take_screenshots("etienne", "shop")


@needs_browser
def test_screenshots_of_an_app_without_views(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.start_preview("etienne", "tasks")

    with pytest.raises(BuilderError, match=r"^The screenshots can\'t be taken"):
        builder.take_screenshots("etienne", "tasks")


@needs_browser
def test_screenshots_over_mcp(builder: Builder) -> None:
    draft_example(builder, "shop")

    async def main() -> None:
        async with BuilderClient(me_server(builder.apps)) as client:
            await client.call_tool("start_preview", {"app": "shop"})
            result = await client.call_tool("take_screenshots", {"app": "shop", "role": "staff"})
        # A line before each picture, two pictures for each of the three views, and a last line.
        assert [part.type for part in result.content] == ["text", "image"] * 6 + ["text"]
        assert all(part.mime_type == "image/png" for part in result.content if part.type == "image")

    anyio.run(main)


def test_screenshots_in_the_dark_theme_over_mcp(
    builder: Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    themes: list[str] = []

    def drawn(self, login, app, version, role, files, views=None, theme="light"):
        themes.append(theme)
        return [Picture(view="till", width=768, png=PNG), Picture(view="till", width=390, png=PNG)]

    monkeypatch.setattr(SandboxClient, "pictures", drawn)
    draft_example(builder, "shop")

    async def main() -> None:
        async with BuilderClient(me_server(builder.apps)) as client:
            await client.call_tool("start_preview", {"app": "shop"})
            arguments = {"app": "shop", "role": "staff", "view": "till"}
            light = await client.call_tool("take_screenshots", arguments)
            night = await client.call_tool("take_screenshots", {**arguments, "theme": "dark"})
            wrong = await client.call_tool(
                "take_screenshots", {**arguments, "theme": "blue"}, raise_on_error=False
            )
        assert themes == ["light", "dark"]
        assert light.content[0].text == "till, as wide as a conversation (768 pixels):"
        assert light.content[-1].text.endswith(
            'Before you publish, take them once with theme="dark" too.'
        )
        assert night.content[0].text == (
            "till, as wide as a conversation (768 pixels), in the dark theme:"
        )
        assert night.content[-1].text == (
            "Look at each screenshot. If something looks wrong, fix the view and take them again."
        )
        assert wrong.is_error
        assert wrong.content[0].text == (
            "theme should be 'light' or 'dark'.\ntake_screenshots takes app, role, view and theme."
        )

    anyio.run(main)


def test_try_view_in_the_dark_theme_over_mcp(
    builder: Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    themes: list[str] = []
    # The top 1,600 pixels of a screen 5,000 pixels tall.
    cut = PNG[:16] + (390).to_bytes(4) + (1600).to_bytes(4)

    def script(self, login, app, version, role, files, view, steps, width=768, theme="light"):
        themes.append(theme)
        return Scripted(png=cut, calls=[], error=None, height=5000)

    monkeypatch.setattr(SandboxClient, "script", script)
    draft_example(builder, "shop")

    async def main() -> None:
        async with BuilderClient(me_server(builder.apps)) as client:
            await client.call_tool("start_preview", {"app": "shop"})
            arguments = {"app": "shop", "role": "staff", "view": "till", "steps": [], "width": 390}
            tried = await client.call_tool("try_view", {**arguments, "theme": "dark"})
        assert themes == ["dark"]
        text = tried.content[0].text
        assert text.startswith("till, as staff, at 390 pixels, in the dark theme, after 0 steps.")
        # The call that shows the rest draws it in the same theme.
        assert 'steps=[...], role="staff", width=390, theme="dark")' in text

    anyio.run(main)


# Enough products that the shelves are about 10,000 pixels tall on a phone.
LONG_SHELVES = "sku,name,price,reorder_at\n" + "".join(
    f"P-{n:03},Product number {n},1.00,0\n" for n in range(110)
)


@needs_browser
def test_a_long_screen_s_picture_is_cut_and_the_answer_says_so(builder: Builder) -> None:
    draft_example(builder, "shop")
    builder.write_file("etienne", "shop", "data/products.csv", LONG_SHELVES)

    async def main() -> None:
        async with BuilderClient(me_server(builder.apps)) as client:
            await client.call_tool("start_preview", {"app": "shop"})
            arguments = {"app": "shop", "role": "staff", "view": "shelves"}
            result = await client.call_tool("take_screenshots", arguments)
            tried = await client.call_tool("try_view", {**arguments, "steps": [], "width": 390})
        _, _, line, narrow, _ = result.content
        assert line.type == "text" and narrow.type == "image"
        png = base64.b64decode(narrow.data)
        # As wide as try_view draws a phone, one pixel per CSS pixel, and no taller than the cap.
        assert int.from_bytes(png[16:20]) == 390
        assert int.from_bytes(png[20:24]) == screenshot.MAX_HEIGHT
        found = re.search(r"the screen's ([\d,]+) pixels", line.text)
        assert found is not None
        tall = int(found[1].replace(",", ""))
        assert 9000 < tall < 20000
        assert line.text.startswith(
            "shelves, as wide as a phone (390 pixels): The picture is cut: it shows the top 1,600"
        )
        assert 'try_view(app="shop", view="shelves", steps=[...], role="staff", width=390)' in (
            line.text
        )
        # try_view draws the same picture, and says the same of it.
        text, picture = tried.content
        assert int.from_bytes(base64.b64decode(picture.data)[20:24]) == screenshot.MAX_HEIGHT
        assert "The picture is cut: it shows the top 1,600" in text.text

    anyio.run(main)


def test_a_failed_picture_says_what_to_call_next(
    builder: Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fails(self, login, app, version, role, files, views=None, theme="light"):
        raise CaptureFailed(
            "shelves, 390 pixels wide, on a screen 9876 pixels tall, couldn't be captured: the "
            "browser refused Page.captureScreenshot: Unable to capture screenshot"
        )

    monkeypatch.setattr(SandboxClient, "pictures", fails)
    draft_example(builder, "shop")
    builder.start_preview("etienne", "shop")

    with pytest.raises(BuilderError) as every:
        builder.take_screenshots("etienne", "shop", role="staff")
    assert str(every.value).endswith(
        'Unable to capture screenshot. Take that view alone with take_screenshots(app="shop", '
        'role="staff", view="<view>"), or look at it as wide as a phone with '
        'try_view(app="shop", view="<view>", steps=[], role="staff", width=390).'
    )
    with pytest.raises(BuilderError) as one:
        builder.take_screenshots("etienne", "shop", role="staff", view="shelves")
    assert str(one.value).endswith(
        "Look at it as wide as a phone with "
        'try_view(app="shop", view="shelves", steps=[], role="staff", width=390).'
    )


def test_a_failed_dark_picture_says_to_look_at_it_dark(
    builder: Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fails(self, login, app, version, role, files, views=None, theme="light"):
        raise CaptureFailed("till couldn't be captured")

    monkeypatch.setattr(SandboxClient, "pictures", fails)
    draft_example(builder, "shop")
    builder.start_preview("etienne", "shop")

    with pytest.raises(BuilderError) as error:
        builder.take_screenshots("etienne", "shop", role="staff", theme="dark")
    assert str(error.value).endswith(
        'Take that view alone with take_screenshots(app="shop", role="staff", view="<view>", '
        'theme="dark"), or look at it as wide as a phone with try_view(app="shop", '
        'view="<view>", steps=[], role="staff", width=390, theme="dark").'
    )


def test_only_a_failed_capture_says_to_take_one_view(
    builder: Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fails(self, login, app, version, role, files, views=None, theme="light"):
        raise SandboxError("the sandbox answered 500: it broke")

    monkeypatch.setattr(SandboxClient, "pictures", fails)
    draft_example(builder, "shop")
    builder.start_preview("etienne", "shop")

    with pytest.raises(BuilderError) as error:
        builder.take_screenshots("etienne", "shop", role="staff")
    assert str(error.value) == (
        "The screenshots can't be taken: the sandbox answered 500: it broke"
    )


def test_screenshots_of_a_view_the_app_doesn_t_have(builder: Builder) -> None:
    draft_example(builder, "shop")
    builder.start_preview("etienne", "shop")

    with pytest.raises(BuilderError) as error:
        builder.take_screenshots("etienne", "shop", role="staff", view="shelf")
    assert "no view shelf. The views are: till, shelves, delivery." in str(error.value)
    assert "try_view" not in str(error.value)


SALE = {"sku": "TEA-SEN", "quantity": 2, "receipt": "R-0900"}
STEPS = [
    {"fill": "Receipt", "with": "R-0900"},
    {"choose": "Product", "option": "Sencha green tea 100 g"},
    {"click": "Add to receipt"},
    {"click": "Record receipt"},
]


def quantity(result: dict, sku: str) -> int:
    data = result["result"]
    if isinstance(data, dict) and "result" in data:
        data = data["result"]
    return next(row["quantity"] for row in data if row["sku"] == sku)


@needs_browser
def test_screenshots_show_the_preview(shop, builder: Builder, monkeypatch) -> None:
    sent: list[dict[str, bytes]] = []
    pictures = SandboxClient.pictures

    def spy(self, login, app, version, role, files, views=None, theme="light"):
        sent.append(dict(files))
        return pictures(self, login, app, version, role, files, views, theme)

    monkeypatch.setattr(SandboxClient, "pictures", spy)
    builder.start_preview("etienne", "shop")
    (before, _), _ = builder.take_screenshots("etienne", "shop", role="staff", view="shelves")

    builder.try_call("etienne", "shop", "record_sale", SALE, role="staff")
    (after, _), _ = builder.take_screenshots("etienne", "shop", role="staff", view="shelves")

    assert b"R-0900" not in sent[0]["movements.csv"]
    assert b"R-0900" in sent[1]["movements.csv"]
    assert after.png[:8] == b"\x89PNG\r\n\x1a\n"
    assert after.png != before.png


@needs_browser
def test_try_view_keeps_its_clicks(shop, builder: Builder) -> None:
    head = builder.apps.repo("etienne", "shop").head()
    builder.start_preview("etienne", "shop")

    tried = builder.try_view("etienne", "shop", "till", STEPS, role="staff")
    after = builder.try_call("etienne", "shop", "stock", role="staff")

    assert tried.error is None
    # The sale the view recorded, made again on the preview.
    assert [(one["tool"], one["ok"]) for one in tried.kept] == [("record_receipt", True)]
    assert quantity(after, "TEA-SEN") == 10
    assert builder.apps.repo("etienne", "shop").head() == head
