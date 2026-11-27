# /// script
# dependencies = ["cliquet-sdk"]
# ///
"""A tally of amounts. Its actions say what they did: the tests of an action's result."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from cliquet import (
    ActionContext,
    App,
    BaseModel,
    Field,
    InvariantContext,
    QueryContext,
    Refused,
)

tally = App("tally", files=["amounts.txt"], roles=["owner"])


class Added(BaseModel):
    """What `add` did."""

    added: int
    total: Decimal
    # A public name that differs from the Python one: results are given by alias.
    day_: date = Field(alias="day")


def _amounts() -> list[Decimal]:
    path = Path("amounts.txt")
    return [Decimal(line) for line in path.read_text().split()] if path.exists() else []


@tally.action
def add(ctx: ActionContext, amounts: list[Decimal]) -> Added:
    """Add amounts, and say how many were added and the new total."""
    if not amounts:
        raise Refused("Give at least one amount")
    with open("amounts.txt", "a") as f:
        f.writelines(f"{amount}\n" for amount in amounts)
    return Added(added=len(amounts), total=sum(_amounts(), Decimal(0)), day=ctx.now.date())


@tally.action
def count(ctx: ActionContext) -> int:
    """Say how many amounts there are, changing nothing."""
    return len(_amounts())


@tally.action
def clear(ctx: ActionContext) -> None:
    """Remove every amount."""
    Path("amounts.txt").write_text("")


@tally.action
def wrong(ctx: ActionContext) -> Added:
    """Write an amount, then return something that isn't an `Added`."""
    Path("amounts.txt").write_text("1\n")
    answer: Any = 3
    return answer


@tally.invariant("Amounts are positive")
def positive(ctx: InvariantContext) -> list[str]:
    return [f"{amount} is not positive" for amount in _amounts() if amount <= 0]


@tally.query
def total(ctx: QueryContext) -> Decimal:
    """The sum of the amounts."""
    return sum(_amounts(), Decimal(0))
