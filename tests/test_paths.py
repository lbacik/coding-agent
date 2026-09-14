from __future__ import annotations

from pathlib import Path

from coding_agent.implement.paths import resolve_within


def test_resolve_within_returns_the_resolved_path_for_a_child(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    assert resolve_within(root, "a.txt") == (root / "a.txt").resolve()


def test_resolve_within_returns_the_root_itself(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    assert resolve_within(root, ".") == root.resolve()


def test_resolve_within_returns_none_for_a_path_that_escapes_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    assert resolve_within(root, "../escaped.txt") is None


def test_resolve_within_returns_none_for_an_absolute_path_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"

    assert resolve_within(root, str(outside)) is None
