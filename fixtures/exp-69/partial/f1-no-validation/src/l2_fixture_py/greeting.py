from __future__ import annotations


def greet(name: str) -> str:
    return f"Hello, {name}!"


def shout(name: str, exclamations: int = 1) -> str:
    return greet(name).upper().rstrip("!") + "!" * exclamations
