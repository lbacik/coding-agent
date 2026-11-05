from __future__ import annotations


def greet(name: str) -> str:
    stripped = name.strip()
    if not stripped:
        raise ValueError("name must not be empty or only whitespace")
    return f"Hello, {stripped}!"


def shout(name: str) -> str:
    return greet(name).upper()
