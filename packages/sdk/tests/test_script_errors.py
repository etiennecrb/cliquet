"""What a scripted step says when it finds nothing, without a browser."""

from cliquet.script import _nothing


def test_a_step_that_finds_nothing_suggests_a_text_a_few_letters_off() -> None:
    seen = ["record receipt", "add to receipt", "record receipt", "tea"]
    said = _nothing(2, "Recrod receipt", seen)
    assert said == (
        'Step 2: nothing on the screen reads "Recrod receipt". Did you mean "record receipt"? '
        'It also has: "add to receipt", "tea".'
    )
    assert (
        _nothing(1, "Sell", ["tea"]) == 'Step 1: nothing on the screen reads "Sell". It has: "tea".'
    )
