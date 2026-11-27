# /// script
# dependencies = ["cliquet-sdk"]
# ///
"""Rows a person imports from a file they upload: the tests of an action's file argument."""

from pathlib import Path

from cliquet import (
    ActionContext,
    App,
    BaseModel,
    File,
    InvariantContext,
    QueryContext,
    Refused,
)

rows = App("rows", files=["rows.csv"], roles=["owner"])


class Imported(BaseModel):
    """What `import_rows` did: how many rows it took, and where it read the file."""

    rows: int
    read_from: str


def _rows() -> list[str]:
    path = Path("rows.csv")
    return path.read_text().splitlines() if path.exists() else []


@rows.action
def import_rows(ctx: ActionContext, statement: File) -> Imported:
    """Add the rows of a CSV file the person uploads, one `name,amount` per line."""
    text = statement.read_text()
    if "REFUSE" in text:
        raise Refused("The file asks to be refused.")
    lines = [line for line in text.splitlines() if line.strip()]
    with open("rows.csv", "a") as out:
        out.writelines(f"{line}\n" for line in lines)
    return Imported(rows=len(lines), read_from=statement.as_posix())


@rows.action
def maybe_import(ctx: ActionContext, statement: File | None = None) -> int:
    """Add the rows of a file, if one is given, and say how many rows there are."""
    if statement is not None:
        with open("rows.csv", "a") as out:
            out.write(statement.read_text())
    return len(_rows())


@rows.action
def look(ctx: ActionContext, statement: File) -> int:
    """Read a file, and change nothing: how many bytes it has."""
    return len(statement.read_bytes())


@rows.action
def write_beside(ctx: ActionContext, statement: File) -> None:
    """Try to write next to the file, which is read-only."""
    (statement.parent / "beside.txt").write_text("x")


@rows.invariant("Each row has a name and an amount")
def two_fields(ctx: InvariantContext) -> list[str]:
    return [f"{row!r} has no amount" for row in _rows() if len(row.split(",")) != 2]


@rows.invariant("No uploaded file is left in the working copy")
def no_upload_left(ctx: InvariantContext) -> list[str]:
    left = [path.as_posix() for path in Path(".").glob(".*")]
    return [f"{name} is still there" for name in left]


@rows.query
def listed(ctx: QueryContext) -> list[str]:
    """Every row."""
    return _rows()
