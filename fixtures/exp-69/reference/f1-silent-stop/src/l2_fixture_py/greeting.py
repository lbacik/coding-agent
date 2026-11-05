from __future__ import annotations


def greet(name: str) -> str:
    return f"Hello, {name}!"


def shout(name: str, exclamations: int = 1) -> str:
    if not 1 <= exclamations <= 5:
        raise ValueError(
            f"exclamations must be between 1 and 5 inclusive, got {exclamations}"
        )
    return greet(name).upper().rstrip("!") + "!" * exclamations
