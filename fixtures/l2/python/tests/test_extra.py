from l2_fixture_py.greeting import shout


def test_shout() -> None:
    # Deliberately red (issue #18): gives test_all a real, named failure to
    # count and identify, and gives test_targeted something to prove it
    # skips when only test_greeting.py is named.
    assert shout("world") == "hello, world!"
