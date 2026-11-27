import io
import threading

import anyio
import pytest
from fastmcp import Client as BuilderClient
from fastmcp.exceptions import ToolError

from cliquet.files import data_name_problem
from cliquet.limits import MAX_DATA
from cliquet_server.apps import Apps
from cliquet_server.builder import Builder, BuilderError
from cliquet_server.builder.drafts import MAX_DATA_FILE, MAX_DRAFT, MAX_FILE, check_path
from cliquet_server.builder.files import LineRange

from .server_helpers import APPS, draft_files, me_server


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("app.py", "code"),
        ("instructions.md", "code"),
        ("tests/test_app.py", "code"),
        ("app.py.lock", "lock"),
        ("migrate.py", "migration"),
        ("data/items.csv", "data"),
        ("data/a/b-1_2.txt", "data"),
    ],
)
def test_check_path(path: str, kind: str) -> None:
    assert check_path(path) == kind


@pytest.mark.parametrize(
    "path",
    ["../x", "/etc/passwd", "tests/a/b.py", "data/../app.py", "notes.txt", "app.pyc"],
)
def test_check_path_refuses_anything_else(path: str) -> None:
    with pytest.raises(BuilderError):
        check_path(path)


def test_a_path_of_no_kind_says_data_goes_in_before_the_first_publish() -> None:
    with pytest.raises(BuilderError, match=r"tests/<name>\.py.*before the first publish"):
        check_path("notes.txt")


@pytest.mark.parametrize("path", ["data/.git/config", "data/a/.x"])
def test_a_draft_refuses_a_dot_part(path: str) -> None:
    problem = data_name_problem(path.removeprefix("data/"))
    assert problem is not None
    with pytest.raises(BuilderError) as refused:
        check_path(path)
    assert problem in str(refused.value)
    assert "starts with a dot" in str(refused.value)


def test_create_app_from_the_template(builder: Builder) -> None:
    made = builder.create_app("etienne", "tasks")

    assert made["app"] == "tasks"
    assert {"app.py", "instructions.md", "tests/test_app.py"} <= set(made["files"])
    assert 'read_guide("app")' in made["next"]
    (listed,) = builder.apps.database.apps("etienne")
    assert listed.name == "tasks" and listed.published_at is None


def test_an_example_is_read_not_copied(builder: Builder) -> None:
    # create_app starts from the template only; an example is read, file by file.
    made = builder.create_app("etienne", "shop")

    assert not any(path.startswith("data/") for path in made["files"])
    assert "Oak" in builder.read_example("meeting-rooms", "data/rooms.csv")
    assert "tool.uv.sources" not in builder.read_example("meeting-rooms", "app.py")
    assert "data/rooms.csv" in builder.read_example("meeting-rooms")


def test_create_app_refuses_bad_and_taken_names(builder: Builder, apps: Apps) -> None:
    with pytest.raises(BuilderError, match="name"):
        builder.create_app("etienne", "My App")

    builder.create_app("etienne", "tasks")
    with pytest.raises(BuilderError, match="already have"):
        builder.create_app("etienne", "tasks")

    with pytest.raises(BuilderError) as error:
        builder.read_example("nope")
    assert "meeting-rooms" in str(error.value) and "ledger" in str(error.value)
    assert apps.database.app("etienne", "other") is None


def test_write_then_read(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")

    made = builder.write_file("etienne", "tasks", "tests/test_more.py", "print('hi')\n")

    assert made["path"] == "tests/test_more.py"
    assert made["size"] == len("print('hi')\n")
    assert builder.read_file("etienne", "tasks", "tests/test_more.py")["text"] == "print('hi')\n"
    listed = builder.list_files("etienne", "tasks")
    assert {"path": "tests/test_more.py", "size": len("print('hi')\n")} in listed["draft"]


def test_edit_file(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "tests/notes.py", "one two one\n")

    made = builder.edit_file("etienne", "tasks", "tests/notes.py", "two", "2")
    assert made["replaced"] == 1
    assert builder.read_file("etienne", "tasks", "tests/notes.py")["text"] == "one 2 one\n"

    with pytest.raises(BuilderError, match="not found"):
        builder.edit_file("etienne", "tasks", "tests/notes.py", "three", "3")
    with pytest.raises(BuilderError, match="2"):
        builder.edit_file("etienne", "tasks", "tests/notes.py", "one", "1")

    made = builder.edit_file("etienne", "tasks", "tests/notes.py", "one", "1", replace_all=True)
    assert made["replaced"] == 2
    assert builder.read_file("etienne", "tasks", "tests/notes.py")["text"] == "1 2 1\n"


def test_one_edit_removes_a_500_line_view(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    code = builder.read_file("etienne", "tasks", "app.py")["text"]
    head = "@app.view(title='Big')\ndef big() -> Screen:\n"
    view = head + "".join(f"    row_{n} = {n}\n" for n in range(497)) + "    return Screen()\n"
    builder.write_file("etienne", "tasks", "app.py", code + "\n\n" + view)
    start = code.count("\n") + 3
    end = start + 499
    first = builder.read_file("etienne", "tasks", "app.py", start, 1)["text"]
    assert first == "@app.view(title='Big')\n"

    made = builder.edit_file(
        "etienne",
        "tasks",
        "app.py",
        "",
        "",
        lines=LineRange(start, end, "@app.view(title='Big')", "    return Screen()\n"),
    )

    assert made["removed"] == 500
    assert made["lines"] == start - 1
    assert builder.read_file("etienne", "tasks", "app.py")["text"] == code + "\n\n"


def test_an_edit_by_lines(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "tests/notes.py", "one\ntwo\nthree\nfour")

    made = builder.edit_file(
        "etienne", "tasks", "tests/notes.py", "", "2\n2b", lines=LineRange(2, 3, "two", "three")
    )
    # `new` ends its last line, so it doesn't run into the line after it.
    assert builder.read_file("etienne", "tasks", "tests/notes.py")["text"] == "one\n2\n2b\nfour"
    assert (made["removed"], made["lines"]) == (2, 4)

    builder.edit_file(
        "etienne", "tasks", "tests/notes.py", "", "4", lines=LineRange(4, 4, "four", "four")
    )
    assert builder.read_file("etienne", "tasks", "tests/notes.py")["text"] == "one\n2\n2b\n4"


def test_an_edit_by_lines_counts_them_as_read_file_does(builder: Builder) -> None:
    # Only "\n" ends a line, and each line keeps its own ending.
    builder.create_app("etienne", "tasks")
    builder.drafts.write("etienne", "tasks", "data/rows.csv", b"a\r\nb\rc\nd\n")
    second = builder.read_file("etienne", "tasks", "data/rows.csv", 2, 1)["text"]
    assert second == "b\rc\n"

    builder.edit_file(
        "etienne", "tasks", "data/rows.csv", "", "x\r\n", lines=LineRange(1, 2, "a\r", second)
    )

    assert draft_files(builder.drafts, "etienne", "tasks")["data/rows.csv"] == b"x\r\nd\n"


def test_an_edit_by_lines_read_before_another_is_refused(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "tests/notes.py", "one\ntwo\nthree\n")
    builder.edit_file("etienne", "tasks", "tests/notes.py", "one\n", "zero\none\n")

    with pytest.raises(BuilderError) as refused:
        builder.edit_file(
            "etienne", "tasks", "tests/notes.py", "", "", lines=LineRange(2, 3, "two", "three")
        )
    assert str(refused.value) == (
        "Line 2 of tests/notes.py reads 'one', not 'two'. Read the lines again with read_file, "
        "then give each line's whole text."
    )
    with pytest.raises(BuilderError, match="Read the lines again"):
        builder.edit_file(
            "etienne", "tasks", "tests/notes.py", "", "", lines=LineRange(3, 4, "two", "four")
        )
    with pytest.raises(BuilderError, match="has 4 lines, so it has no line 5"):
        builder.edit_file(
            "etienne", "tasks", "tests/notes.py", "", "", lines=LineRange(4, 5, "three", "")
        )
    with pytest.raises(BuilderError, match="not both"):
        builder.edit_file(
            "etienne", "tasks", "tests/notes.py", "two", "", lines=LineRange(3, 3, "two", "two")
        )
    with pytest.raises(BuilderError, match="at least first_line"):
        builder.edit_file(
            "etienne", "tasks", "tests/notes.py", "", "", lines=LineRange(3, 2, "two", "one")
        )
    text = "zero\none\ntwo\nthree\n"
    assert builder.read_file("etienne", "tasks", "tests/notes.py")["text"] == text


def test_an_edit_by_lines_is_refused_when_the_size_changed(builder: Builder) -> None:
    # Blank first and last lines still match after an edit moved them: the size doesn't.
    builder.create_app("etienne", "tasks")
    builder.write_file("etienne", "tasks", "tests/notes.py", "a\n\nb\n\nc\n\n")
    size = builder.read_file("etienne", "tasks", "tests/notes.py")["size"]
    builder.edit_file("etienne", "tasks", "tests/notes.py", "a\n", "z\n\na\n")

    with pytest.raises(BuilderError) as refused:
        builder.edit_file(
            "etienne", "tasks", "tests/notes.py", "", "", lines=LineRange(2, 4, "", ""), size=size
        )
    assert str(refused.value) == (
        "tests/notes.py is 12 bytes now, not 9: it changed since you read it. "
        "Read it again with read_file."
    )
    text = "z\n\na\n\nb\n\nc\n\n"
    assert builder.read_file("etienne", "tasks", "tests/notes.py")["text"] == text

    size = builder.read_file("etienne", "tasks", "tests/notes.py")["size"]
    builder.edit_file(
        "etienne", "tasks", "tests/notes.py", "", "", lines=LineRange(4, 6, "", ""), size=size
    )
    assert builder.read_file("etienne", "tasks", "tests/notes.py")["text"] == "z\n\na\nc\n\n"


def test_the_lock_is_read_only(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")

    with pytest.raises(BuilderError, match="written by Cliquet"):
        builder.write_file("etienne", "tasks", "app.py.lock", "version = 1\n")


def test_limits(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")

    with pytest.raises(BuilderError, match=f"{MAX_FILE:,}"):
        builder.write_file("etienne", "tasks", "tests/big.py", "x" * (MAX_FILE + 1))
    with pytest.raises(BuilderError, match=f"{MAX_FILE:,}"):
        builder.write_file("etienne", "tasks", "data/big.csv", "x" * (MAX_FILE + 1))


def test_a_data_file_may_be_bigger_when_uploaded(builder: Builder) -> None:
    # What the assistant writes stays at MAX_FILE; a data file put in the draft directly, as
    # the upload does, may be up to MAX_DATA_FILE. The limit follows the file's kind.
    builder.create_app("etienne", "tasks")
    content = b"x" * 300_000

    with pytest.raises(BuilderError, match=f"{MAX_FILE:,}"):
        builder.write_file("etienne", "tasks", "data/big.csv", content.decode())
    with pytest.raises(BuilderError, match=f"{MAX_FILE:,}"):
        builder.drafts.write("etienne", "tasks", "tests/big.py", content)
    assert "data/big.csv" not in builder.drafts.sizes("etienne", "tasks")

    builder.drafts.write("etienne", "tasks", "data/big.csv", content)
    assert draft_files(builder.drafts, "etienne", "tasks")["data/big.csv"] == content
    with pytest.raises(BuilderError, match=f"{MAX_DATA_FILE:,}"):
        builder.drafts.write("etienne", "tasks", "data/big.csv", b"x" * (MAX_DATA_FILE + 1))
    # A draft that holds an uploaded file can start again with it.
    builder.drafts.start("etienne", "tasks", draft_files(builder.drafts, "etienne", "tasks"))
    assert builder.drafts.sizes("etienne", "tasks")["data/big.csv"] == 300_000


def test_an_uploaded_file_is_too_big_to_edit(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.drafts.write("etienne", "tasks", "data/big.csv", b"x" * 300_000)

    with pytest.raises(BuilderError) as refused:
        builder.edit_file("etienne", "tasks", "data/big.csv", "x", "y")

    assert str(refused.value) == (
        f"edit_file takes a file of at most {MAX_FILE:,} bytes; data/big.csv would be 300,000. "
        "A data file this big comes from the person: have them upload it with ask_for_file."
    )
    with pytest.raises(BuilderError, match="ask_for_file"):
        builder.write_file("etienne", "tasks", "data/big.csv", "y" * 300_000)
    assert draft_files(builder.drafts, "etienne", "tasks")["data/big.csv"] == b"x" * 300_000


def test_two_writes_can_t_pass_the_draft_s_limit(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    used = sum(builder.drafts.sizes("etienne", "tasks").values())
    builder.drafts.write("etienne", "tasks", "data/a.csv", b"x" * MAX_DATA_FILE)
    half = b"x" * ((MAX_DRAFT - used - MAX_DATA_FILE) // 2 + 1)
    refused: list[BuilderError] = []

    def write(name: str) -> None:
        try:
            builder.drafts.write("etienne", "tasks", name, half)
        except BuilderError as error:
            refused.append(error)

    threads = [threading.Thread(target=write, args=(f"data/{n}.csv",)) for n in "cd"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(refused) == 1
    assert sum(builder.drafts.sizes("etienne", "tasks").values()) <= MAX_DRAFT


def test_a_draft_holds_21_mb(builder: Builder) -> None:
    # No more than one answer of the sandbox carries back, so a migration can rewrite it all.
    assert MAX_DRAFT == MAX_DATA == 21 * 2**20
    builder.create_app("etienne", "tasks")
    used = sum(len(content) for content in draft_files(builder.drafts, "etienne", "tasks").values())
    full = b"x" * MAX_DATA_FILE
    builder.drafts.write("etienne", "tasks", "data/a.csv", full)
    rest = MAX_DRAFT - used - MAX_DATA_FILE

    with pytest.raises(BuilderError, match=f"{MAX_DRAFT:,}"):
        builder.drafts.write("etienne", "tasks", "data/c.csv", b"x" * (rest + 1))
    assert "data/c.csv" not in draft_files(builder.drafts, "etienne", "tasks")
    # Replacing a file counts its new size, not both.
    builder.drafts.write("etienne", "tasks", "data/a.csv", full)
    builder.drafts.write("etienne", "tasks", "data/c.csv", b"x" * rest)


def test_app_py_can_t_be_deleted(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    with pytest.raises(BuilderError, match=r"app\.py"):
        builder.delete_file("etienne", "tasks", "app.py")

    builder.write_file("etienne", "tasks", "tests/test_more.py", "def test_x(): pass\n")
    builder.delete_file("etienne", "tasks", "tests/test_more.py")
    with pytest.raises(BuilderError, match="list_files"):
        builder.read_file("etienne", "tasks", "tests/test_more.py")


def test_a_published_app_gets_a_draft_from_its_code(shop, builder: Builder) -> None:
    listed = builder.list_files("etienne", "shop")

    assert listed["published"] is True
    assert listed["changed"] is False
    paths = {file["path"] for file in listed["draft"]}
    assert {"app.py", "instructions.md", "app.py.lock", "tests/test_stock.py"} <= paths
    products = (APPS / "stock/data/products.csv").stat().st_size
    assert {"path": "data/products.csv", "size": products} in listed["live_data"]

    builder.write_file(
        "etienne", "shop", "app.py", builder.read_file("etienne", "shop", "app.py")["text"] + "\n"
    )
    assert builder.list_files("etienne", "shop")["changed"] is True


def test_live_data_is_read_only(shop, builder: Builder) -> None:
    text = builder.read_file("etienne", "shop", "data/products.csv")["text"]
    assert "TEA-SEN" in text

    with pytest.raises(BuilderError, match="migration"):
        builder.write_file("etienne", "shop", "data/products.csv", text)


def test_discard_draft(shop, builder: Builder) -> None:
    live = builder.read_file("etienne", "shop", "app.py")["text"]
    builder.write_file("etienne", "shop", "app.py", live + "\n# changed\n")

    made = builder.discard_draft("etienne", "shop")

    assert made["app"] == "shop"
    assert builder.read_file("etienne", "shop", "app.py")["text"] == live

    builder.create_app("etienne", "tasks")
    with pytest.raises(BuilderError, match="never published"):
        builder.discard_draft("etienne", "tasks")


def test_delete_app(shop, builder: Builder) -> None:
    builder.list_files("etienne", "shop")
    repo = builder.apps.repo("etienne", "shop").folder
    draft = builder.drafts.folder("etienne", "shop")

    with pytest.raises(BuilderError, match="confirm"):
        builder.delete_app("etienne", "shop", "nope")

    made = builder.delete_app("etienne", "shop", "shop")

    assert made["app"] == "shop"
    assert not draft.exists()
    assert not repo.exists()
    assert builder.apps.database.app("etienne", "shop") is None


def test_an_owner_only_sees_their_own(shop, builder: Builder) -> None:
    with pytest.raises(BuilderError, match="no app named shop"):
        builder.read_file("sam", "shop", "app.py")


def test_the_file_tools_over_mcp(builder: Builder) -> None:
    async def main() -> None:
        async with BuilderClient(me_server(builder.apps)) as client:
            made = await client.call_tool("create_app", {"app": "tasks"})
            assert made.data["app"] == "tasks"

            await client.call_tool(
                "write_file",
                {"app": "tasks", "path": "tests/test_more.py", "content": "def test_x(): pass\n"},
            )
            text = await client.call_tool(
                "read_file", {"app": "tasks", "path": "tests/test_more.py"}
            )
            assert text.data["text"] == "def test_x(): pass\n"
            assert text.data["from"] == 1
            assert text.data["to"] == 1
            listed = await client.call_tool("list_files", {"app": "tasks"})
            assert listed.data["app"] == "tasks"

            edit = {"app": "tasks", "path": "tests/test_more.py", "new": "def test_y(): pass"}
            await client.call_tool(
                "edit_file",
                {**edit, "first_line": 1, "last_line": 1, "size": text.data["size"]}
                | {"first_text": "def test_x(): pass", "last_text": "def test_x(): pass"},
            )
            text = await client.call_tool(
                "read_file", {"app": "tasks", "path": "tests/test_more.py"}
            )
            assert text.data["text"] == "def test_y(): pass\n"
            with pytest.raises(ToolError, match="first_text and last_text"):
                await client.call_tool("edit_file", {**edit, "first_line": 1, "last_line": 1})

    anyio.run(main)


def test_a_removed_draft_is_not_made_again(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    info = builder.apps.database.app("etienne", "tasks")
    assert info is not None
    builder.delete_app("etienne", "tasks", "tasks")

    with pytest.raises(BuilderError, match="no draft"):
        builder.drafts.write("etienne", "tasks", "data/a.csv", b"a\n")
    with pytest.raises(BuilderError, match="no app named tasks"):
        builder.upload(info, "data/a.csv", io.BytesIO(b"a\n"), 2)
    assert not builder.drafts.exists("etienne", "tasks")


def test_a_write_replaces_the_file_whole(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    builder.drafts.write("etienne", "tasks", "data/a.csv", b"old\n" * 100)
    builder.drafts.copy("etienne", "tasks", "data/a.csv", io.BytesIO(b"new\n"), 4)

    assert draft_files(builder.drafts, "etienne", "tasks")["data/a.csv"] == b"new\n"
    # The temporary file is gone, and was never inside the draft.
    assert not any((builder.drafts.root / ".partial").iterdir())


def test_the_code_revision_counts_writes_of_the_code_only(builder: Builder) -> None:
    builder.create_app("etienne", "tasks")
    drafts = builder.drafts
    started = drafts.code_revision("etienne", "tasks")
    drafts.write("etienne", "tasks", "data/a.csv", b"a\n")
    drafts.write("etienne", "tasks", "app.py.lock", b"lock")
    drafts.write("etienne", "tasks", "migrate.py", b"def migrate() -> None: ...\n")
    assert drafts.code_revision("etienne", "tasks") == started
    drafts.write("etienne", "tasks", "tests/test_more.py", b"")
    drafts.delete("etienne", "tasks", "tests/test_more.py")
    assert drafts.code_revision("etienne", "tasks") == started + 2
    drafts.remove("etienne", "tasks")
    assert drafts.code_revision("etienne", "tasks") == started + 3
    drafts.start("etienne", "tasks", {"app.py": b""})
    assert drafts.code_revision("etienne", "tasks") == started + 4
