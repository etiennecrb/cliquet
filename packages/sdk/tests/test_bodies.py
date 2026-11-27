"""A request's body is bounded before it is read: the preview's calls and the sandbox's."""

from contextlib import closing
from pathlib import Path

import anyio
import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.testclient import TestClient
from test_runtime import LOG_APP, OWNER, make_app

from cliquet import preview
from cliquet.limits import read_body
from cliquet.mcp_server import AppServer


def test_a_body_that_states_too_long_a_length_is_refused_unread() -> None:
    async def never() -> dict[str, object]:
        raise AssertionError("the body was read")

    scope = {"type": "http", "method": "POST", "headers": [(b"content-length", b"101")]}
    with pytest.raises(HTTPException) as refused:
        anyio.run(read_body, Request(scope, never), 100)
    assert refused.value.status_code == 413


def test_a_body_that_sends_more_than_it_may_is_refused() -> None:
    chunks = [b"x" * 60, b"x" * 60]

    async def receive() -> dict[str, object]:
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    scope = {"type": "http", "method": "POST", "headers": []}
    with pytest.raises(HTTPException) as refused:
        anyio.run(read_body, Request(scope, receive), 100)
    assert refused.value.status_code == 413


def test_the_preview_refuses_a_call_over_its_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preview, "MAX_BODY", 100)
    with closing(make_app(tmp_path / "log", LOG_APP, {"log.txt": "start\n"})) as runtime:
        app = preview.preview_app(AppServer(runtime, OWNER))
        with TestClient(app) as client:
            assert client.post("/rpc", content=b"{" + b" " * 200 + b"}").status_code == 413
            assert client.post("/rpc", json={"method": "tools/list"}).status_code == 200
