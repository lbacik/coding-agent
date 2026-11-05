from l2_fixture_py.greeting import greet


def shout_all(name: str) -> str:
    return greet(name).upper()
