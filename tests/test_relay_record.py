"""The Relay terminal record: render and strict parse (#716, §22)."""

from __future__ import annotations

import pytest

from coding_agent.runtime.relay import (
    RelayRecord,
    parse_relay_block,
    render_relay_block,
    render_relay_section,
)

RECORD = RelayRecord(
    source_attempt="triage-7-1",
    source_workflow="triage",
    target_workflow="implement",
    issue=7,
    outcome="completed",
    relay_label="triaged",
    origin_actor="alice",
    origin_actor_id=42,
    origin_actor_type="User",
    origin_actor_at="2026-11-28T10:00:00Z",
    identity_mode="dedicated",
)


def test_record_round_trips_through_the_terminal_comment() -> None:
    body = "head\n\n" + render_relay_section(RECORD) + "\n\nmarker\n"

    assert parse_relay_block(body) == RECORD


def test_optional_fields_may_be_absent() -> None:
    minimal = RelayRecord(
        source_attempt="a", source_workflow="s", target_workflow="t",
        issue=1, outcome="failed", relay_label="l",
        origin_actor="bob", origin_actor_id=1,
    )

    assert parse_relay_block(render_relay_block(minimal)) == minimal


def test_unknown_fields_are_ignored_and_a_newer_version_reads_as_absent() -> None:
    block = render_relay_block(RECORD).replace("-->", "future_field: x\n-->")
    assert parse_relay_block(block) == RECORD
    assert parse_relay_block(render_relay_block(RECORD).replace("v1", "v2")) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.replace("issue: 7", "issue: seven"),
        lambda b: b.replace("issue: 7", "issue: 0"),
        lambda b: b.replace("origin_actor_id: 42", "origin_actor_id: 0"),
        lambda b: b.replace("relay_label: triaged\n", ""),
        lambda b: b.replace("source_attempt: triage-7-1\n", ""),
        lambda b: b.replace("-->", ""),
        lambda b: b.replace("issue: 7", "issue: 7\nissue: 8"),
        lambda b: "no block here",
    ],
)
def test_malformed_or_forged_records_read_as_absent(mutate) -> None:  # type: ignore[no-untyped-def]
    assert parse_relay_block(mutate(render_relay_block(RECORD))) is None
