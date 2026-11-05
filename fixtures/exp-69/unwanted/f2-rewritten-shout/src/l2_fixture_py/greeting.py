from __future__ import annotations


def greet(name: str) -> str:
    return f"Hello, {name}!"


def shout(name: str) -> str:
    """Return the whole greeting in upper case."""
    return f"HELLO, {name.upper()}!"
