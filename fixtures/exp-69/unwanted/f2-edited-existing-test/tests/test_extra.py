from l2_fixture_py.greeting import greet, shout


def test_shout() -> None:
    assert shout("world") == "HELLO, WORLD!"


def test_shout_is_upper_greet() -> None:
    assert shout("Ada") == greet("Ada").upper()
