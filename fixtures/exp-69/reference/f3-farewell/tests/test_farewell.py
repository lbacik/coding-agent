from l2_fixture_py.greeting import farewell


def test_farewell() -> None:
    assert farewell("World") == "Goodbye, World!"
