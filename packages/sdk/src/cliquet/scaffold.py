"""`cliquet new`: create an app folder with its own git repository."""

import builtins
import hashlib
import json
import keyword
import os
import re
import shutil
import subprocess
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from urllib.parse import unquote, urlparse

from cliquet.manifest import SDK_DISTRIBUTION, sdk_pin, uv_environment
from cliquet.store import OBJECT_FORMAT
from cliquet.tools import MAX_APP_NAME

IGNORE = "__pycache__/\n.pytest_cache/\n"

_APP = '''\
# /// script
# requires-python = ">=3.14"
# dependencies = ["{pin}"]
# ///
"""{title}: a list of items, kept by an agent."""

import csv
from typing import Annotated

from cliquet import ActionContext, App, BaseModel, Field, InvariantContext, QueryContext

ITEMS = "items.csv"

{var} = App("{name}", files=[ITEMS])


class Item(BaseModel):
    name: str
    note: str


def _items() -> list[Item]:
    try:
        with open(ITEMS, newline="") as f:
            return [Item(**row) for row in csv.DictReader(f)]
    except FileNotFoundError:
        return []


@{var}.action
def add_item(
    ctx: ActionContext,
    name: Annotated[str, Field(pattern=r"\\S", description="The item, unique on the list")],
    note: Annotated[str, Field(description="Anything worth remembering about it")] = "",
) -> None:
    """Add an item to the list."""
    rows = [*_items(), Item(name=name.strip(), note=note)]
    with open(ITEMS, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "note"])
        writer.writeheader()
        writer.writerows(row.model_dump() for row in rows)


@{var}.invariant("Item names are unique")
def unique_names(ctx: InvariantContext) -> list[str]:
    seen: set[str] = set()
    problems = []
    for item in _items():
        if item.name in seen:
            problems.append(f"{{item.name!r}} is listed twice")
        seen.add(item.name)
    return problems


@{var}.query
def items(ctx: QueryContext) -> list[Item]:
    """Every item on the list."""
    return _items()
'''

_TESTS = """\
from datetime import UTC, datetime

import pytest
from app import {var}, add_item, items, unique_names

from cliquet.testing import InvalidArguments, given

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_adds_an_item() -> None:
    person = given({var}, {{}}, now=NOW).connect()
    person.call(add_item, name="Milk").expect_ok()
    assert [item.name for item in person.query(items)] == ["Milk"]


def test_an_item_needs_a_name() -> None:
    person = given({var}, {{}}, now=NOW).connect()
    with pytest.raises(InvalidArguments):
        person.call(add_item, name=" ")


def test_refuses_an_item_listed_twice() -> None:
    person = given({var}, {{"items.csv": "name,note\\nMilk,\\n"}}, now=NOW).connect()
    person.call(add_item, name="Milk").expect_refused(by=unique_names)
"""

_INSTRUCTIONS = """\
# {title}

A list of items. Add one with `add_item`, and read the list with `items`.

Item names are unique. If `add_item` is refused because the item is already listed,
tell the person instead of trying again.
"""


class ScaffoldError(Exception):
    """The app folder can't be created as asked."""


def local_sdk() -> Path | None:
    """The folder of this SDK when it is installed from a local checkout, else None."""
    try:
        text = distribution(SDK_DISTRIBUTION).read_text("direct_url.json")
    except PackageNotFoundError:
        return None
    if not text:
        return None
    url = urlparse(json.loads(text).get("url", ""))
    return Path(unquote(url.path)) if url.scheme == "file" else None


def sdk_files(root: Path) -> dict[str, bytes]:
    """The files of the SDK in the folder `root` that installing it needs, by path."""
    root = Path(root)
    names = ("pyproject.toml", "README.md", "LICENSE")
    files = {name: (root / name).read_bytes() for name in names}
    for path in sorted((root / "src" / "cliquet").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def sdk_code_hash(root: Path) -> str:
    """A hash of the SDK's code in the folder `root`: its `pyproject.toml` and `.py` files.

    Its other files, the renderer's own and the vendored ones, change no sandbox's setup or
    environment, nor the manifest it reads of an app: a sandbox reads them as they are now.
    """
    digest = hashlib.sha256()
    for name, content in sdk_files(root).items():
        if name == "pyproject.toml" or name.endswith(".py"):
            digest.update(f"{name}\0{len(content)}\0".encode() + content)
    return digest.hexdigest()


def sdk_folder() -> Path | None:
    """The SDK's folder to build apps against: `CLIQUET_SDK_PATH`, or else `local_sdk()`."""
    given = os.environ.get("CLIQUET_SDK_PATH")
    # Absolute: uv reads a relative path from the copy of app.py, in another folder.
    return Path(given).absolute() if given else local_sdk()


def set_sdk_source(source: str, sdk: Path | None) -> str:
    """Point the PEP 723 block's SDK at the folder `sdk`, or remove the source if None."""
    lines = source.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.rstrip() == "# /// script"), None)
    if start is None:
        raise ScaffoldError("app.py has no `# /// script` block")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].rstrip() == "# ///")
    block, kept, skipping = lines[start + 1 : end], [], False
    for line in block:
        header = re.match(r"^#\s*\[(.+)\]\s*$", line)
        if header:
            skipping = header.group(1).strip() == "tool.uv.sources"
        if not skipping:
            kept.append(line)
    while kept and kept[-1].strip() == "#":
        kept.pop()
    if sdk is not None:
        path = json.dumps(str(sdk))
        kept += [
            "#\n",
            "# [tool.uv.sources]\n",
            f"# {SDK_DISTRIBUTION} = {{ path = {path}, editable = true }}\n",
        ]
    return "".join([*lines[: start + 1], *kept, *lines[end:]])


def new_app(
    target: Path,
    *,
    source: Path | None = None,
    name: str | None = None,
    lock: bool = True,
) -> Path:
    """Create an app folder with its own git repository and a first commit."""
    target = target.resolve()
    if target.exists() and any(target.iterdir()):
        raise ScaffoldError(f"{target} already exists and isn't empty")
    if shutil.which("git") is None or (lock and shutil.which("uv") is None):
        raise ScaffoldError("cliquet new needs git and uv on the PATH")
    if source is not None:
        _copy_app(source.resolve(), target)
    else:
        write_template(target, name or _name_from(target))
    app_py = target / "app.py"
    app_py.write_text(set_sdk_source(app_py.read_text(), local_sdk()))
    (target / ".gitignore").write_text(IGNORE)
    (target / "data").mkdir(exist_ok=True)
    _run(["git", "init", "-q", OBJECT_FORMAT], target)
    if lock:
        _run(["uv", "lock", "--quiet", "--script", "app.py"], target, uv_environment())
    _run(["git", "add", "-A"], target)
    message = f"Create the app from {source.name}" if source is not None else "Create the app"
    _commit(target, message)
    return target


def _copy_app(source: Path, target: Path) -> None:
    if not (source / "app.py").is_file():
        raise ScaffoldError(f"{source} has no app.py")
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "app.py", target / "app.py")
    if (source / "instructions.md").is_file():
        shutil.copy2(source / "instructions.md", target / "instructions.md")
    ignore = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")
    for folder in ("tests", "data"):
        if (source / folder).is_dir():
            shutil.copytree(source / folder, target / folder, ignore=ignore)


def write_template(target: Path, name: str) -> None:
    """Write the small app `cliquet new` makes into `target`."""
    if not re.match(r"^[a-z][a-z0-9_-]*$", name) or len(name) > MAX_APP_NAME:
        raise ScaffoldError(
            f"{name!r} can't be an app name: use at most {MAX_APP_NAME} lower case letters and "
            "digits, starting with a letter"
        )
    var = name.replace("-", "_")
    if var in dir(builtins) or keyword.iskeyword(var):
        var = "app"
    title = name.replace("-", " ").replace("_", " ").capitalize()
    (target / "tests").mkdir(parents=True)
    (target / "app.py").write_text(_APP.format(pin=sdk_pin(), title=title, name=name, var=var))
    (target / "tests" / "test_app.py").write_text(_TESTS.format(var=var))
    (target / "instructions.md").write_text(_INSTRUCTIONS.format(title=title))


def _name_from(target: Path) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", target.name.lower()).strip("-")
    name = name if re.match(r"^[a-z]", name) else f"app-{name}"
    # A long folder name is cut to what an app's name may be.
    return name[:MAX_APP_NAME].rstrip("-")


def _run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise ScaffoldError(f"{' '.join(command)} failed: {result.stderr.strip()}")


def _commit(target: Path, message: str) -> None:
    result = subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=target, capture_output=True, text=True
    )
    if result.returncode != 0:
        hint = ""
        if "user.email" in result.stderr or "user.name" in result.stderr:
            hint = ' Set your name first: git config --global user.name "..." and user.email.'
        raise ScaffoldError(f"the first commit failed: {result.stderr.strip()}{hint}")
