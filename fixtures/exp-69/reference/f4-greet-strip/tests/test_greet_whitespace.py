import pytest

from l2_fixture_py.greeting import greet, shout


def test_greet_strips_surrounding_whitespace() -> None:
    assert greet("  Ada Lovelace ") == "Hello, Ada Lovelace!"


@pytest.mark.parametrize("name", ["", "   "])
def test_greet_rejects_blank_name(name: str) -> None:
    with pytest.raises(ValueError, match="name"):
        greet(name)


def test_shout_strips_through_greet() -> None:
    assert shout("  ada ") == "HELLO, ADA!"
