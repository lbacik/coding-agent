from l2_fixture_py.greeting import greet


def test_greet() -> None:
    assert greet("World") == "Hello, World!"
