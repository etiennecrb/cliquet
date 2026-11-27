import base64
import gzip
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from cliquet.errors import from_app, shown
from cliquet.limits import MAX_ANSWER
from cliquet.sandbox import version_id
from cliquet.wheels import Locked, Wheel, Wheels, WheelUnavailable, default_cache
from cliquet_server.db import Database
from cliquet_server.docker import DockerExecutor
from cliquet_server.executor import (
    BuildProblems,
    Built,
    CaptureFailed,
    LocalExecutor,
    SandboxClient,
    SandboxError,
    SandboxFault,
    UnknownVersion,
    make_executor,
    sandbox_environment,
    wait_until_healthy,
)
from cliquet_server.settings import Settings, SettingsError
from cliquet_server.sprites import SpritesExecutor

from .server_helpers import example_code, example_data, template_code

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC).isoformat()


def client_for(handler: Any, token: str | None = None, **kwargs: Any) -> SandboxClient:
    return SandboxClient("http://sandbox", token, transport=httpx.MockTransport(handler), **kwargs)


def test_the_token_and_the_headers_are_sent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = client_for(handler, "secret", headers={"Authorization": "Bearer x"})
    assert client.health() == {"ok": True}
    client.close()
    assert seen[0].headers["x-sandbox-token"] == "secret"
    assert seen[0].headers["authorization"] == "Bearer x"


def test_json_goes_gzipped_and_a_gzipped_answer_is_read() -> None:
    seen: list[httpx.Request] = []
    answer = gzip.compress(json.dumps({"ok": True, "padding": "x" * 1000}).encode())

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=answer, headers={"content-encoding": "gzip"})

    client = client_for(handler)
    request = {"op": "manifest", "arguments": {"note": "été"}}
    assert client.run("sam", "stock", "0" * 16, request)["ok"] is True
    client.close()
    assert seen[0].headers["content-encoding"] == "gzip"
    assert seen[0].headers["content-type"] == "application/json"
    assert "gzip" in seen[0].headers["accept-encoding"]
    assert json.loads(gzip.decompress(seen[0].read())) == request


def test_answers_become_errors() -> None:
    def answering(status: int, body: dict[str, Any]) -> Any:
        return lambda request: httpx.Response(status, json=body)

    with pytest.raises(UnknownVersion):
        client_for(answering(404, {"error": "unknown version"})).health()
    with pytest.raises(BuildProblems) as problems:
        client_for(answering(422, {"problems": ["one", "two"]})).health()
    assert problems.value.problems == ["one", "two"]
    with pytest.raises(BuildProblems) as error:
        client_for(answering(422, {"error": "uv can't build it"})).health()
    assert error.value.problems == ["uv can't build it"]
    # A view whose picture failed, after it was laid out, is its own kind of error.
    with pytest.raises(CaptureFailed, match="couldn't be captured"):
        client_for(
            answering(422, {"error": "till couldn't be captured", "capture_failed": True})
        ).health()
    with pytest.raises(SandboxError):
        client_for(answering(401, {"error": "wrong or missing token"})).health()
    with pytest.raises(SandboxError):
        client_for(answering(500, {"error": "boom"})).health()


def test_a_client_says_when_an_answer_is_not_the_sandbox_s() -> None:
    answers = [
        httpx.Response(404, text="no such Sprite"),
        httpx.Response(404, json={"error": "unknown version"}),
        httpx.Response(422, json={"problems": ["app.py has no App"]}),
    ]
    lost: list[str] = []
    client = client_for(lambda request: answers.pop(0), lost=lambda: lost.append("lost"))
    with pytest.raises(SandboxError, match="answered 404"):
        client.health()
    assert lost == ["lost"]
    # The sandbox's own answers say it is there.
    with pytest.raises(UnknownVersion):
        client.health()
    with pytest.raises(BuildProblems):
        client.health()
    assert lost == ["lost"]


def test_a_client_says_when_the_sandbox_can_t_be_reached() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    lost: list[str] = []
    client = client_for(handler, lost=lambda: lost.append("lost"))
    with pytest.raises(SandboxError, match="can't be reached"):
        client.health()
    # Once, when the call gives up, not at each try.
    assert lost == ["lost"]


def test_a_client_pointed_at_another_sandbox_speaks_to_it_with_its_token() -> None:
    seen: list[tuple[str, list[str], str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens = request.headers.get_list("x-sandbox-token")
        seen.append((request.url.host, tokens, request.headers.get("authorization")))
        return httpx.Response(200, json={"ok": True})

    client = SandboxClient(
        "https://old.example",
        "old-token",
        headers={"Authorization": "Bearer org"},
        transport=httpx.MockTransport(handler),
    )
    client.health()
    client.point_at("https://new.example", "new-token")
    client.health()
    assert seen == [
        ("old.example", ["old-token"], "Bearer org"),
        ("new.example", ["new-token"], "Bearer org"),
    ]
    client.close()


def test_a_dropped_connection_is_tried_again() -> None:
    calls: list[httpx.Request] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            raise httpx.ConnectError("no")
        return httpx.Response(200, json={"ok": True})

    client = client_for(flaky)
    assert client.health() == {"ok": True}
    assert len(calls) == 3
    calls.clear()

    def down(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("no")

    with pytest.raises(SandboxError):
        client_for(down).health()
    assert len(calls) == 3


def test_a_timeout_is_not_tried_again() -> None:
    calls: list[httpx.Request] = []

    def slow(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("slow")

    with pytest.raises(SandboxError) as error:
        client_for(slow).health()
    assert len(calls) == 1
    assert "more than 30 seconds" in str(error.value)


def test_the_client_stops_at_32_mb() -> None:
    assert MAX_ANSWER == 32 * 1024 * 1024
    sent: list[int] = []

    def chunks() -> Any:
        yield b'{"manifest": "'
        for _ in range(100):
            sent.append(1)
            yield b"x" * (1024 * 1024)
        yield b'"}'

    client = client_for(lambda request: httpx.Response(200, content=chunks()))
    with pytest.raises(SandboxError, match="the sandbox answered more than 32 MB"):
        client.health()
    assert len(sent) <= 34


def built(manifest: dict[str, Any], lock: bytes = b"lock") -> Any:
    body = {"manifest": manifest, "lock": base64.b64encode(lock).decode()}
    return lambda request: httpx.Response(200, json=body)


def test_a_lock_file_is_at_most_1_mb() -> None:
    manifest = {"name": "shop", "files": ["a.csv"]}
    assert client_for(built(manifest)).put_version("s", "shop", "0" * 16, {}).lock == b"lock"
    big = client_for(built(manifest, b"x" * (1024 * 1024 + 1)))
    with pytest.raises(BuildProblems, match="lock file"):
        big.put_version("s", "shop", "0" * 16, {})


def test_pictures_are_bounded() -> None:
    def answering(count: int, size: int) -> SandboxClient:
        png = base64.b64encode(b"x" * size).decode()
        pictures = [{"view": "v", "width": 768, "png": png}] * count
        return client_for(lambda request: httpx.Response(200, json={"pictures": pictures}))

    assert len(answering(50, 10).pictures("s", "shop", "0" * 16, None, {})) == 50
    with pytest.raises(SandboxError, match="more than 50 pictures"):
        answering(51, 10).pictures("s", "shop", "0" * 16, None, {})
    with pytest.raises(SandboxError, match="more than 8 MB"):
        answering(1, 8 * 1024 * 1024 + 1).pictures("s", "shop", "0" * 16, None, {})
    png = base64.b64encode(b"x" * (8 * 1024 * 1024 + 1)).decode()
    script = client_for(lambda request: httpx.Response(200, json={"png": png, "calls": []}))
    with pytest.raises(SandboxError, match="more than 8 MB"):
        script.script("s", "shop", "0" * 16, None, {}, "v", [])


def test_pictures_and_scripts_name_their_theme() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(gzip.decompress(request.content)))
        return httpx.Response(200, json={"pictures": [], "png": "", "calls": []})

    client = client_for(handler)
    client.pictures("s", "shop", "0" * 16, None, {})
    client.pictures("s", "shop", "0" * 16, None, {}, ["till"], "dark")
    client.script("s", "shop", "0" * 16, None, {}, "till", [])
    client.script("s", "shop", "0" * 16, None, {}, "till", [], 390, "dark")
    assert [body["theme"] for body in sent] == ["light", "dark", "light", "dark"]


def test_a_script_answer_is_checked() -> None:
    def answering(calls: Any) -> SandboxClient:
        body: dict[str, Any] = {"png": "", "calls": calls, "error": None}
        return client_for(lambda request: httpx.Response(200, json=body))

    def run(client: SandboxClient) -> Any:
        return client.script("s", "shop", "0" * 16, None, {}, "v", [])

    sale = {"tool": "sell", "arguments": {"sku": "A"}, "error": None, "result": {"commit": "c"}}
    refused = {"tool": "sell", "arguments": {}, "error": "x" * 10 + "y" * 2_000}
    assert run(answering([sale, refused])).calls == [
        sale,
        {"tool": "sell", "arguments": {}, "error": "y" * 2_000, "result": None},
    ]
    assert run(answering(None)).calls == []
    assert len(run(answering([sale] * 1000)).calls) == 1000
    with pytest.raises(SandboxError, match="more than 1000 calls"):
        run(answering([sale] * 1001))
    for wrong in (
        {"calls": "sell"},
        ["sell"],
        [{**sale, "tool": 3}],
        [{**sale, "arguments": ["A"]}],
        [{**sale, "error": 1}],
        [{"arguments": {}}],
    ):
        with pytest.raises(SandboxError, match=r"calls aren't a list|without a tool's name"):
            run(answering(wrong))


def test_test_output_and_errors_are_cut() -> None:
    answer = {"passed": True, "output": "a" * 10 + "b" * 20_000}
    run = client_for(lambda request: httpx.Response(200, json=answer)).test("s", "shop", "0" * 16)
    assert run.output == "b" * 20_000
    long = "a" * 10 + "b" * 2_000
    refused = client_for(lambda request: httpx.Response(422, json={"error": long}))
    with pytest.raises(BuildProblems) as error:
        refused.health()
    assert error.value.problems == ["b" * 2_000]
    script = client_for(
        lambda request: httpx.Response(200, json={"png": "", "error": long, "calls": []})
    )
    assert script.script("s", "shop", "0" * 16, None, {}, "v", []).error == "b" * 2_000


ENV_QUERY = """

@{var}.query
def environment(ctx: QueryContext) -> list[str]:
    \"\"\"The names of the variables the app's code can see.\"\"\"
    import os

    return sorted(os.environ)
"""


def test_the_local_sandbox_gets_no_server_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIQUET_OIDC_CLIENT_SECRET", "the-client-secret")
    monkeypatch.setenv("CLIQUET_SECRET_KEY", "the-secret-key-of-the-server-0123456789")
    code = template_code(tmp_path / "app", "secrets")
    code["app.py"] = code["app.py"] + ENV_QUERY.format(var="secrets").encode()
    code.pop("app.py.lock", None)
    local = LocalExecutor(tmp_path / "sandbox")
    try:
        client = local.sandbox("etienne")
        version = version_id(code)
        client.put_version("etienne", "secrets", version, code)
        request = {
            "op": "query",
            "caller": {"id": "t", "role": None},
            "now": NOW,
            "name": "environment",
            "arguments": {},
            "files": {},
        }
        names = client.run("etienne", "secrets", version, request)["result"]
    finally:
        local.close()
    assert "PATH" in names
    assert "CLIQUET_OIDC_CLIENT_SECRET" not in names
    assert "CLIQUET_SECRET_KEY" not in names
    assert "the-secret-key" not in json.dumps(names)


def test_the_local_sandbox_gets_the_browser_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    # Chromium needs it when the whole server runs in a container without its own sandbox.
    monkeypatch.setenv("CLIQUET_CHROME_NO_SANDBOX", "1")
    assert sandbox_environment("a-token")["CLIQUET_CHROME_NO_SANDBOX"] == "1"


def test_the_local_sandbox_starts_once(executor: LocalExecutor) -> None:
    first = executor.sandbox("etienne")
    assert first.health()["ok"] is True
    assert executor.sandbox("sam") is first


def test_a_version_is_built_and_run(executor: LocalExecutor) -> None:
    client = executor.sandbox("etienne")
    code = example_code()
    version = version_id(code)
    built = client.put_version("etienne", "shop", version, code)
    assert isinstance(built, Built)
    assert built.manifest["name"] == "shop"
    assert built.lock
    files = {name: base64.b64encode(content).decode() for name, content in example_data().items()}
    answer = client.run(
        "etienne",
        "shop",
        version,
        {
            "op": "query",
            "caller": {"id": "t", "role": "owner"},
            "now": NOW,
            "name": "stock",
            "arguments": {},
            "files": files,
        },
    )
    assert len(answer["result"]) == 5


def test_an_unknown_version_is_said(executor: LocalExecutor) -> None:
    with pytest.raises(UnknownVersion):
        executor.sandbox("etienne").run("etienne", "shop", "0" * 16, {"op": "manifest"})


def test_destroying_an_owner_s_sandbox_removes_their_apps_only(
    executor: LocalExecutor, tmp_path: Path
) -> None:
    client = executor.sandbox("etienne")
    code = template_code(tmp_path / "notes")
    version = version_id(code)
    client.put_version("leaving", "notes", version, code)
    client.put_version("staying", "notes", version, code)
    executor.destroy("leaving")
    apps = executor.root / "apps"
    assert not (apps / "leaving").exists()
    with pytest.raises(UnknownVersion):
        client.run("leaving", "notes", version, {"op": "manifest"})
    # The one sandbox serves every other owner as before.
    assert client.run("staying", "notes", version, {"op": "manifest"})
    assert (apps / "staying" / "notes" / version).is_dir()
    assert executor.sandbox("staying") is client


def test_destroying_starts_no_sandbox(tmp_path: Path) -> None:
    local = LocalExecutor(tmp_path / "sandbox")
    left = tmp_path / "sandbox" / "apps" / "leaving" / "notes"
    left.mkdir(parents=True)
    (left / "app.py").write_text("")
    local.destroy("leaving")
    local.destroy("never-here")
    assert not left.parent.exists()
    assert local._process is None
    with pytest.raises(ValueError, match="not a login"):
        local.destroy("..")
    local.close()


def test_an_app_with_problems_can_t_be_built(executor: LocalExecutor) -> None:
    code = example_code()
    without = (
        code["app.py"]
        .decode()
        .replace(
            '"""Stock for a small tea shop: products, deliveries and sales, kept by an agent."""\n',
            "",
        )
    )
    assert without != code["app.py"].decode()
    code = {**code, "app.py": without.encode()}
    version = version_id(code)
    with pytest.raises(BuildProblems) as error:
        executor.sandbox("etienne").put_version("sam", "shop", version, code)
    assert any("docstring" in problem for problem in error.value.problems)


def test_the_sandbox_is_started_again_if_it_stopped(tmp_path: Path) -> None:
    local = LocalExecutor(tmp_path / "sandbox")
    try:
        first = local.sandbox("t")
        assert first.health()["ok"] is True
        assert local._process is not None
        local._process.kill()
        local._process.wait(timeout=5)
        again = local.sandbox("t")
        assert again is not first
        assert again.health()["ok"] is True
    finally:
        local.close()


def test_make_executor(tmp_path: Path, database: Database) -> None:
    settings = Settings.from_env({"CLIQUET_DATA_DIR": str(tmp_path / "data")})
    made = make_executor(settings, database)
    assert isinstance(made, LocalExecutor)
    assert made.root == settings.data_dir / "sandbox"
    made.close()
    sprites = Settings.from_env(
        {
            "CLIQUET_DATA_DIR": str(tmp_path / "data"),
            "CLIQUET_EXECUTOR": "sprites",
            "CLIQUET_SPRITES_TOKEN": "a-token",
        }
    )
    made_sprites = make_executor(sprites, database)
    assert isinstance(made_sprites, SpritesExecutor)
    made_sprites.close()
    containers = Settings.from_env(
        {
            "CLIQUET_DATA_DIR": str(tmp_path / "data"),
            "CLIQUET_EXECUTOR": "docker",
            "CLIQUET_SANDBOX_IMAGE": "mine:1",
        }
    )
    made_docker = make_executor(containers, database)
    assert isinstance(made_docker, DockerExecutor)
    assert made_docker._sockets == containers.data_dir / "sockets"
    assert made_docker._image == "mine:1"
    made_docker.close()
    with pytest.raises(SettingsError):
        Settings.from_env(
            {"CLIQUET_DATA_DIR": str(tmp_path / "data"), "CLIQUET_EXECUTOR": "sprites"}
        )


def test_a_client_can_speak_over_a_unix_socket(tmp_path: Path) -> None:
    client = SandboxClient("http://sandbox", "secret", socket=str(tmp_path / "sandbox.sock"))
    assert str(client._client.base_url) == "http://sandbox"
    assert client._client.headers["x-sandbox-token"] == "secret"
    client.close()


def test_a_client_says_each_call_and_wakes_a_stopped_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cliquet_server.executor as executor

    monkeypatch.setattr(executor.time, "sleep", lambda seconds: None)
    answers: list[Any] = [
        httpx.ConnectError("it was stopped"),
        httpx.Response(200, json={"ok": True}),
    ]
    asked: list[httpx.Request] = []
    calls: list[int] = []
    woken: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    client = SandboxClient(
        "http://sandbox",
        "a-token",
        transport=httpx.MockTransport(handler),
        on_call=lambda: calls.append(1),
        wake=lambda: woken.append(1),
    )
    assert client.health() == {"ok": True}
    assert len(asked) == 2
    assert calls == [1]
    assert woken == [1]
    client.close()


class FakeHealth:
    """A sandbox's answers, without a sandbox: each one's SDK, or an error."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.asked = 0

    def ask(self, client: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Answer the client's health call: the next answer it has, and at least one."""
        self.asked += 1
        answer = self.answers[min(self.asked, len(self.answers)) - 1]
        if isinstance(answer, SandboxError):
            raise answer
        return answer


class FakeClock:
    """A clock that moves on its own, so a long wait ends at once."""

    def __init__(self) -> None:
        self._now = 0.0

    def monotonic(self) -> float:
        self._now += 1.0
        return self._now

    def sleep(self, seconds: float) -> None:
        """Nothing: the clock moves by itself."""


def answer_health(monkeypatch: pytest.MonkeyPatch, fake: FakeHealth) -> None:
    """Make every sandbox answer `fake`, as the sprites' own tests do."""

    def health(client: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return fake.ask(client, *args, **kwargs)

    monkeypatch.setattr(SandboxClient, "health", health)


def test_a_sandbox_is_healthy_only_as_this_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    import cliquet
    import cliquet_server.executor as executor

    # No test waits: the clock moves by itself, so the sandbox answers at once or never does.
    monkeypatch.setattr(executor, "time", FakeClock())
    healthy = FakeHealth({"sdk": cliquet.__version__})
    answer_health(monkeypatch, healthy)
    wait_until_healthy(client_for(lambda request: httpx.Response(200, json={})))
    assert healthy.asked == 1

    other = FakeHealth(SandboxError("the sandbox can't be reached"), {"sdk": cliquet.__version__})
    answer_health(monkeypatch, other)
    wait_until_healthy(client_for(lambda request: httpx.Response(200, json={})))
    assert other.asked == 2

    wrong = FakeHealth({"sdk": "0.0.0"})
    answer_health(monkeypatch, wrong)
    with pytest.raises(SandboxError, match=r"has SDK 0\.0\.0, not"):
        wait_until_healthy(client_for(lambda request: httpx.Response(200, json={})))


def test_a_sandbox_that_never_answers_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    import cliquet_server.executor as executor

    monkeypatch.setattr(executor, "time", FakeClock())
    answer_health(monkeypatch, FakeHealth(SandboxError("the sandbox took more than 30 seconds")))
    with pytest.raises(SandboxError, match="didn't start: the sandbox took more than 30 seconds"):
        wait_until_healthy(client_for(lambda request: httpx.Response(200, json={})))


def test_missing_wheels_are_sent_once(tmp_path: Path) -> None:
    class Counting(Wheels):
        sent = 0

        def path(self, wheel: Wheel) -> Path:
            self.sent += 1
            return super().path(wheel)

    wheels = Counting(default_cache())
    local = LocalExecutor(tmp_path / "sandbox", wheels=wheels)
    try:
        client = local.sandbox("etienne")
        code = example_code()
        built = client.put_version("etienne", "shop", version_id(code), code)
        first = wheels.sent
        assert first > 0
        assert any((tmp_path / "sandbox" / "wheels").glob("pytest-*.whl"))
        health = client.health()
        assert built.lock == wheels.lock(code, health["python"], health["platform"]).lock
        other = {**code, "instructions.md": code["instructions.md"] + b"\nMore.\n"}
        again = client.put_version("etienne", "shop", version_id(other), other)
        assert again.manifest["name"] == "shop"
        assert wheels.sent == first
    finally:
        local.close()


def test_a_wheel_that_can_t_be_had_is_not_the_app_s_fault() -> None:
    wheel = Wheel("gone-1.0-py3-none-any.whl", "https://files.pythonhosted.org/g.whl", "0" * 64, 1)

    class Unreachable(Wheels):
        def lock(self, files: Any, python: str, platform_: str) -> Locked:
            return Locked(b"lock", (wheel,))

        def path(self, wheel: Wheel) -> Path:
            raise WheelUnavailable("gone-1.0-py3-none-any.whl can't be downloaded: timed out")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True, "python": "3.14", "platform": "p"})
        return httpx.Response(409, json={"missing": [wheel.name]})

    client = client_for(handler, wheels=Unreachable(default_cache()))
    with pytest.raises(SandboxError, match="timed out") as error:
        client.put_version("s", "shop", "0" * 16, {"app.py": b"code"})
    assert not isinstance(error.value, BuildProblems)


def test_what_the_sandbox_printed_is_app_text() -> None:
    """Build problems stay build problems, and an error's text is the app's unless Cliquet
    marked it as its own fixed words: only the owner reads the rest."""
    problems = client_for(lambda request: httpx.Response(422, json={"error": "KeyError: 'x'"}))
    with pytest.raises(BuildProblems):
        problems.pictures("s", "shop", "0" * 16, None, {})
    with pytest.raises(BuildProblems):
        problems.script("s", "shop", "0" * 16, None, {}, "v", [])
    quoted = client_for(lambda request: httpx.Response(500, text="Traceback: Landlord"))
    with pytest.raises(SandboxError, match="Landlord") as error:
        quoted.pictures("s", "shop", "0" * 16, None, {})
    assert from_app(error.value) and not isinstance(error.value, SandboxFault)
    assert not from_app(SandboxFault("the sandbox can't be reached"))

    class NewTrouble(SandboxError):
        """A kind of SandboxError written later, with any text."""

    member = shown(NewTrouble("Landlord owes 1200"), "Landlord owes 1200", False, "It failed.")
    assert "Landlord" not in member and member.startswith("It failed. Error e-")
    assert shown(NewTrouble("Landlord"), "Landlord", True, "It failed.") == "Landlord"
    reached = SandboxFault("the sandbox can't be reached")
    assert shown(reached, str(reached), False, "It failed.") == str(reached)


def test_an_unreachable_sandbox_logs_its_detail(caplog: pytest.LogCaptureFixture) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused by 10.0.0.7 for Landlord")

    with pytest.raises(SandboxFault) as error:
        client_for(unreachable).health()
    assert re.fullmatch(
        r"the sandbox can't be reached\. Error e-[0-9a-f]{12} has the details\.",
        str(error.value),
    )
    assert "refused by 10.0.0.7 for Landlord" in caplog.text
    # Met while a sandbox starts, too: below ERROR, so a normal boot raises no alarm.
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
