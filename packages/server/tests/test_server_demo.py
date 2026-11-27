import anyio
import httpx

from .server_helpers import app_client, app_token


def levels(result) -> dict[str, int]:
    return {row["sku"]: row["quantity"] for row in result.structured_content["result"]}


def test_the_demo(server, address: str) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(base_url=address) as web:
            signed_in = await web.get("/login?next=/", follow_redirects=False)
            assert signed_in.status_code == 303
            home = await web.get("/")
            assert 'href="/manage/etienne/shop"' in home.text

            page = await web.get("/manage/etienne/shop")
            assert "/apps/etienne/shop/mcp" in page.text

            staff_token = app_token(server, "staff")
            async with app_client(address, "/apps/etienne/shop/mcp", staff_token) as staff:
                tools = {tool.name for tool in (await staff.list_tools()).tools}
                assert "record_sale" in tools
                sale = await staff.call_tool(
                    "record_sale",
                    {"sku": "TEA-SEN", "quantity": 2, "receipt": "R-0900"},
                )
                assert not sale.is_error

            owner_token = app_token(server, "owner")
            async with app_client(address, "/apps/etienne/shop/mcp", owner_token) as owner:
                stock = await owner.call_tool("stock")
                assert levels(stock)["TEA-SEN"] == 9

                history = (await owner.call_tool("app_history")).structured_content["changes"]
                latest = history[0]
                assert latest["action"] == "record_sale"
                assert latest["caller"] == "etienne"

                screen = await web.post(
                    "/apps/etienne/shop/staff/rpc",
                    json={
                        "method": "tools/call",
                        "params": {"name": "shelves", "arguments": {}},
                    },
                )
                assert "structuredContent" in screen.json()

                again = await web.get("/manage/etienne/shop/history")
                assert "record_sale" in again.text

                reverted = await owner.call_tool("app_revert", {"commit": latest["commit"]})
                assert not reverted.is_error
                back = await owner.call_tool("stock")
                assert levels(back)["TEA-SEN"] == 11

    anyio.run(scenario)
