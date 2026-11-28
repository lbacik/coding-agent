"""Relay configuration (#716, contract §3 and §19): parsing and readers.

The refusals are the table-driven invalid fixtures ``69``–``81`` under
``tests/fixtures/workflows/invalid/`` (exercised by
``test_step_workflow_config.py``); this module pins the accepted shape and
the Effective Configuration readers the runtime reads.
"""

from __future__ import annotations

from pathlib import Path

from coding_agent.workflow.effective import (
    effective_relay_declared,
    effective_relay_label,
    effective_relay_target,
    make_test_record,
)
from support.workflow_config import load_config

VALID = """\
version: 4
target: {kind: github, repository: OWNER/REPO, base-branch: main, token-env: GITHUB_TOKEN}
models:
  sonnet: {provider: anthropic, model: claude-sonnet-5, endpoint: messages, effort: medium, api-key-env: ANTHROPIC_API_KEY,
           prices-usd-per-mtok: {input: 2.0, output: 10.0, cache-read: 0.2, cache-write: 2.5}, context-window-tokens: 1000000, context-threshold-tokens: 80000, output-cap-tokens: 32000}
workflows:
  triage:
    model: sonnet
    input: {type: issue, label: agent:triage}
    limits: {cost-usd: 6, effective-tokens: 1500000, tool-calls: 300, wall-clock-minutes: 30}
    outcomes:
      completed: {labels: [triage-done], relay: implement}
    start: work
    defaults:
      max-executions: 1
      routes: {error: {end: failed}, limit-exceeded: {end: failed}}
    steps:
      work:
        operation: check-readiness
        routes: {ready: {end: completed}, not-ready: {end: blocked}}
  implement:
    model: sonnet
    input: {type: issue, label: ready-for-agent, relay-label: triaged}
    limits: {cost-usd: 6, effective-tokens: 1500000, tool-calls: 300, wall-clock-minutes: 30}
    start: work
    defaults:
      max-executions: 1
      routes: {error: {end: failed}, limit-exceeded: {end: failed}}
    steps:
      work:
        operation: check-readiness
        routes: {ready: {end: completed}, not-ready: {end: blocked}}
"""


def test_triage_to_implement_relay_validates(tmp_path: Path) -> None:
    path = tmp_path / "workflows.yaml"
    path.write_text(VALID, encoding="utf-8")

    config, faults = load_config(path)

    assert not faults
    assert config is not None
    assert config.workflows["triage"].relays == {"completed": "implement"}
    assert config.workflows["triage"].outcomes == {"completed": ("triage-done",)}
    assert config.workflows["implement"].input.relay_label == "triaged"
    assert config.workflows["triage"].input.relay_label is None


def test_effective_record_readers() -> None:
    sender = make_test_record(relays={"completed": "implement"})
    receiver = make_test_record(
        workflow_id="implement",
        relay_label="triaged",
        relay_sources={"triage": ["completed"]},
    )
    plain = make_test_record()

    assert effective_relay_target(sender, "completed") == "implement"
    assert effective_relay_target(sender, "failed") is None
    assert effective_relay_target(plain, "completed") is None
    assert effective_relay_label(receiver) == "triaged"
    assert effective_relay_label(plain) is None
    assert effective_relay_declared(receiver, "triage", "completed")
    assert not effective_relay_declared(receiver, "triage", "failed")
    assert not effective_relay_declared(receiver, "other", "completed")
    assert not effective_relay_declared(plain, "triage", "completed")


def test_pinned_workflow_record_carries_both_ends_of_a_relay(tmp_path: Path) -> None:
    from coding_agent.workflow.effective import _workflow_record

    path = tmp_path / "workflows.yaml"
    path.write_text(VALID, encoding="utf-8")
    config, _ = load_config(path)
    assert config is not None

    sender = {"workflow": _workflow_record(config.workflows["triage"], config)}
    receiver = {"workflow": _workflow_record(config.workflows["implement"], config)}

    assert effective_relay_target(sender, "completed") == "implement"
    assert effective_relay_label(receiver) == "triaged"
    assert effective_relay_declared(receiver, "triage", "completed")
    assert effective_relay_label(sender) is None
    # A Workflow with no Relay records no Relay key at all.
    assert "relays" not in receiver["workflow"]
    assert "relay_label" not in sender["workflow"]["input"]  # type: ignore[operator]
