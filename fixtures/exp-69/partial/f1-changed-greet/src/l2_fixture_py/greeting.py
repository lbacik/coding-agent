from __future__ import annotations


def greet(name: str, exclamations: int = 1) -> str:
    if not 1 <= exclamations <= 5:
        raise ValueError(f"exclamations must be between 1 and 5, got {exclamations}")
    return f"Hello, {name}" + "!" * exclamations


def shout(name: str, exclamations: int = 1) -> str:
    return greet(name, exclamations).upper()
