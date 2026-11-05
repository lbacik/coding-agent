from l2_fixture_py.greeting import shout


def test_shout() -> None:
    assert shout("world") == "HELLO, WORLD!"
