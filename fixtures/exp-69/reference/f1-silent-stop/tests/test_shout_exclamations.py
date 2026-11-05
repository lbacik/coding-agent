import pytest

from l2_fixture_py.greeting import shout


def test_shout_with_three_exclamations() -> None:
    assert shout("world", exclamations=3) == "HELLO, WORLD!!!"


@pytest.mark.parametrize("exclamations", [0, 6])
def test_shout_rejects_out_of_range_exclamations(exclamations: int) -> None:
    with pytest.raises(ValueError, match="exclamations"):
        shout("world", exclamations=exclamations)
