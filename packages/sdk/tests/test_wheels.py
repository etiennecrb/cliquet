"""The server's side of sending the packages: it locks an app and keeps the wheels it needs."""

import os
import time
from pathlib import Path

import pytest
from conftest import APPS, HEADER

from cliquet import wheels as wheels_module
from cliquet.sandbox import bundle_of
from cliquet.wheels import Wheel, WheelError, Wheels, WheelUnavailable

pytestmark = [pytest.mark.slow]

SPRITE = ("3.14", "x86_64-manylinux_2_28")
# Two small wheels, by their real URLs and hashes on PyPI.
ANNOTATED_TYPES = Wheel(
    "annotated_types-0.8.0-py3-none-any.whl",
    "https://files.pythonhosted.org/packages/99/91/8acff4f5e50511b911bbccb72b8628a49c68ce14148cd9f6431094859a90/annotated_types-0.8.0-py3-none-any.whl",
    "f072f4d804ea359e4eaf198b1af7a8b0943881a87f31bb764f8bf219bb9419e0",
    13427,
)


def test_the_server_locks_without_building(tmp_path: Path) -> None:
    files = bundle_of(APPS / "stock")
    files.pop("app.py.lock")
    locked = Wheels(tmp_path / "cache").lock(files, *SPRITE)
    assert b'name = "cliquet-sdk"' in locked.lock
    names = [wheel.name for wheel in locked.wheels]
    assert any(name.startswith("pytest-") for name in names)
    assert any(name.startswith("uv_build-") for name in names)
    assert any(name.startswith("jinja2-") for name in names)
    assert not any(name.startswith("pywin32") for name in names)
    assert not any(name.startswith("cliquet") for name in names)
    for wheel in locked.wheels:
        assert wheel.name.endswith(".whl")
        assert wheel.url.startswith("https://files.pythonhosted.org/")
        assert len(wheel.sha256) == 64
    # The same code locks the same way, without running uv again.
    assert Wheels(tmp_path / "cache").lock(files, *SPRITE).lock == locked.lock


def test_a_relock_takes_the_sdk_s_new_dependencies(tmp_path: Path) -> None:
    # The SDK changes in place on a deploy, and an app's stored lock still has its old
    # dependencies: uv 0.9 kept them, and the app's environment lacked the new ones.
    sdk = tmp_path / "sdk"
    (sdk / "src" / "cliquet").mkdir(parents=True)
    (sdk / "src" / "cliquet" / "__init__.py").write_text("")
    pyproject = sdk / "pyproject.toml"
    shipped = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    build = shipped[shipped.index("[build-system]") :]

    def depend_on(*names: str) -> None:
        listed = ", ".join(f'"{name}"' for name in names)
        pyproject.write_text(
            f'[project]\nname = "cliquet-sdk"\nversion = "0.0.1"\n'
            f'requires-python = ">=3.14"\ndependencies = [{listed}]\n\n{build}'
        )

    depend_on("annotated-types")
    wheels = Wheels(tmp_path / "cache", sdk=sdk)
    app = {"app.py": (HEADER + '"""An app."""\n').encode()}
    before = wheels.lock(app, *SPRITE)
    assert not any(wheel.name.startswith("idna-") for wheel in before.wheels)
    modified = pyproject.stat().st_mtime_ns
    depend_on("annotated-types", "idna")
    os.utime(pyproject, ns=(modified, modified))  # as a copy that keeps times would
    after = wheels.lock({**app, "app.py.lock": before.lock}, *SPRITE)
    assert any(wheel.name.startswith("idna-") for wheel in after.wheels)
    assert b'name = "idna"' in after.lock


def test_a_dependency_without_a_wheel_is_refused(tmp_path: Path) -> None:
    header = HEADER.replace('<0.1"]', '<0.1", "docopt"]')
    assert header != HEADER
    app = header + '"""An app that needs a library with no wheel."""\n'
    with pytest.raises(WheelError) as error:
        Wheels(tmp_path / "cache").lock({"app.py": app.encode()}, *SPRITE)
    assert "docopt" in str(error.value)


def test_a_wheel_is_checked_against_its_hash(tmp_path: Path) -> None:
    wheels = Wheels(tmp_path / "cache")
    wrong = Wheel(ANNOTATED_TYPES.name, ANNOTATED_TYPES.url, "0" * 64, ANNOTATED_TYPES.size)
    with pytest.raises(WheelError, match="hash"):
        wheels.path(wrong)
    assert not any(path.is_file() for path in (tmp_path / "cache").rglob("*"))
    found = wheels.path(ANNOTATED_TYPES)
    assert found.name == ANNOTATED_TYPES.name
    assert found.stat().st_size == ANNOTATED_TYPES.size


def test_a_wheel_from_elsewhere_is_refused(tmp_path: Path) -> None:
    wheels = Wheels(tmp_path / "cache")
    for url in ("https://example.com/x-1-py3-none-any.whl", "http://files.pythonhosted.org/x.whl"):
        with pytest.raises(WheelError, match=r"files\.pythonhosted\.org"):
            wheels.path(Wheel("x-1-py3-none-any.whl", url, "0" * 64, 10))
    with pytest.raises(WheelError, match="name"):
        wheels.path(Wheel("../x.whl", ANNOTATED_TYPES.url, ANNOTATED_TYPES.sha256, 10))


def test_the_cache_keeps_under_its_limit(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    old = cache / ("a" * 64) / "old-1-py3-none-any.whl"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"x" * 10_000)
    long_ago = time.time() - 3600
    os.utime(old, (long_ago, long_ago))
    wheels = Wheels(cache, limit=20_000)
    wheels.path(ANNOTATED_TYPES)
    assert not old.exists()
    assert wheels.path(ANNOTATED_TYPES).is_file()


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('<0.1"]', '<0.1", "evil @ http://127.0.0.1:9/evil-1-py3-none-any.whl"]'),
        ('<0.1"]', '<0.1", "evil @ file:///etc/passwd"]'),
        ('<0.1"]', '<0.1", "evil @ git+https://127.0.0.1:9/evil"]'),
        ("# ///\n", '# [tool.uv]\n# index-url = "http://127.0.0.1:9/simple"\n# ///\n'),
        ("# ///\n", '# [tool.uv]\n# find-links = ["/data/apps"]\n# ///\n'),
        ("# ///\n", '# [[tool.uv.index]]\n# url = "http://127.0.0.1:9/simple"\n# ///\n'),
    ],
)
def test_an_app_can_t_point_the_server_s_uv_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before: str, after: str
) -> None:
    header = HEADER.replace(before, after, 1)
    assert header != HEADER
    app = header + '"""An app that asks the server to fetch something else."""\n'

    def no_uv(*args: object, **kwargs: object) -> None:
        raise AssertionError("uv ran on an app that points it elsewhere")

    monkeypatch.setattr(wheels_module.subprocess, "run", no_uv)
    with pytest.raises(WheelError, match="PyPI"):
        Wheels(tmp_path / "cache").lock({"app.py": app.encode()}, *SPRITE)


def test_the_server_remembers_only_its_last_locks(tmp_path: Path) -> None:
    wheels = Wheels(tmp_path / "cache", remembered=2)
    files = bundle_of(APPS / "stock")
    files.pop("app.py.lock")
    for n in range(3):
        wheels.lock({**files, "app.py": files["app.py"] + f"\n# {n}\n".encode()}, *SPRITE)
    assert len(wheels._locked) == 2


def test_a_wheel_that_can_t_be_downloaded_is_said_so(tmp_path: Path) -> None:
    gone = Wheel(
        "gone-1.0-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/00/00/gone-1.0-py3-none-any.whl",
        "0" * 64,
        10,
    )
    with pytest.raises(WheelUnavailable, match="can't be downloaded"):
        Wheels(tmp_path / "cache").path(gone)
