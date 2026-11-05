from __future__ import annotations


def greet(name: str) -> str:
    return f"Hello, {name.strip()}!"


def shout(name: str) -> str:
    return greet(name).upper()
