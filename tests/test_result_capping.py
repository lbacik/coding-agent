from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.implement.result_capping import (
    ArtifactNotFound,
    FilesystemArtifactStore,
    InMemoryArtifactStore,
    build_read_result_slice_tool,
    cap_tool_result,
)


# --- cap_tool_result: L3-IMP-5 ---------------------------------------------


def test_cap_tool_result_returns_short_content_unchanged() -> None:
    store = InMemoryArtifactStore()

    result = cap_tool_result("short", tool_call_id="call-1", limit=100, store=store)

    assert result == "short"
    with pytest.raises(ArtifactNotFound):
        store.read("call-1")


def test_cap_tool_result_at_exactly_the_limit_is_unchanged() -> None:
    store = InMemoryArtifactStore()
    content = "x" * 10

    result = cap_tool_result(content, tool_call_id="call-1", limit=10, store=store)

    assert result == content


def test_cap_tool_result_over_the_limit_stores_the_full_content_and_returns_a_pointer() -> None:
    store = InMemoryArtifactStore()
    content = "0123456789" * 100  # 1000 characters

    result = cap_tool_result(content, tool_call_id="call-7", limit=50, store=store)

    assert store.read("call-7") == content
    assert "call-7" in result
    assert "1000" in result  # the full size is named
    assert len(result) < len(content)
    # Both a head and a tail slice of the real content survive in the cap.
    assert content[:20] in result
    assert content[-20:] in result


def test_cap_tool_result_pointer_names_the_artifact_and_its_full_size() -> None:
    """`L3-IMP-5`'s own wording: the pointer names the stored artifact and
    its full size, so the model can ask for a slice of it."""
    store = InMemoryArtifactStore()
    content = "y" * 5000

    result = cap_tool_result(content, tool_call_id="call-9", limit=200, store=store)

    assert "artifact_id='call-9'" in result
    assert "full_size=5000" in result
    assert "read_result_slice" in result


# --- InMemoryArtifactStore --------------------------------------------------


def test_in_memory_artifact_store_reads_back_what_it_stored() -> None:
    store = InMemoryArtifactStore()
    store.store("a", "hello")

    assert store.read("a") == "hello"


def test_in_memory_artifact_store_raises_on_an_unknown_id() -> None:
    store = InMemoryArtifactStore()

    with pytest.raises(ArtifactNotFound):
        store.read("missing")


# --- FilesystemArtifactStore -------------------------------------------------


def test_filesystem_artifact_store_reads_back_what_it_stored(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(root=tmp_path / "artifacts")
    store.store("a", "hello")

    assert store.read("a") == "hello"
    assert (tmp_path / "artifacts" / "a").read_text(encoding="utf-8") == "hello"


def test_filesystem_artifact_store_raises_on_an_unknown_id(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(root=tmp_path / "artifacts")

    with pytest.raises(ArtifactNotFound):
        store.read("missing")


def test_filesystem_artifact_store_refuses_an_artifact_id_that_escapes_root(tmp_path: Path) -> None:
    """`read_result_slice`'s `artifact_id` is a model-supplied argument,
    untrusted the same way a tool's `path` argument is: a traversal
    attempt must not read a file outside `root`."""
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me", encoding="utf-8")
    store = FilesystemArtifactStore(root=tmp_path / "artifacts")

    with pytest.raises(ArtifactNotFound):
        store.read("../secret.txt")


def test_filesystem_artifact_store_refuses_to_store_under_an_escaping_id(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(root=tmp_path / "artifacts")

    with pytest.raises(ArtifactNotFound):
        store.store("../escaped.txt", "content")

    assert not (tmp_path / "escaped.txt").exists()


# --- read_result_slice tool -------------------------------------------------


def test_read_result_slice_returns_the_requested_character_range() -> None:
    store = InMemoryArtifactStore()
    store.store("call-1", "abcdefghij")
    tool = build_read_result_slice_tool(store)

    result = tool.invoke({"artifact_id": "call-1", "start": 2, "end": 5})

    assert result == "cde"


def test_read_result_slice_reports_an_unknown_artifact_without_raising() -> None:
    store = InMemoryArtifactStore()
    tool = build_read_result_slice_tool(store)

    result = tool.invoke({"artifact_id": "no-such-call", "start": 0, "end": 10})

    assert "Error" in str(result)
    assert "no-such-call" in str(result)


@pytest.mark.parametrize(
    "start,end",
    [(-1, 2), (4, 3), (1.5, 2)],
)
def test_read_result_slice_rejects_invalid_character_ranges(start: object, end: object) -> None:
    store = InMemoryArtifactStore()
    store.store("call-1", "abcdefghij")
    tool = build_read_result_slice_tool(store)

    result = tool.invoke({"artifact_id": "call-1", "start": start, "end": end})

    assert result.startswith("Error: invalid character range")


def test_read_result_slice_round_trips_a_capped_results_full_content() -> None:
    """The full loop this ticket demonstrates: cap a large result, then
    read it back through the tool the pointer names, in pieces."""
    store = InMemoryArtifactStore()
    content = "".join(f"line {i}\n" for i in range(500))
    capped = cap_tool_result(content, tool_call_id="call-3", limit=100, store=store)
    assert capped != content

    tool = build_read_result_slice_tool(store)
    first_half = tool.invoke({"artifact_id": "call-3", "start": 0, "end": len(content) // 2})
    second_half = tool.invoke(
        {"artifact_id": "call-3", "start": len(content) // 2, "end": len(content)}
    )

    assert first_half + second_half == content
