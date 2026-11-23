"""Round Handoff enablement for issue Workflows (contract §8).

Issue #612, acceptance bullet 1. An opted-in issue Workflow sets the
``round-handoff`` mapping with a required positive
``max-automatic-pickups`` limit and an optional ``pickup-label``
(defaulting to ``round-finished``). The mapping is issue-only and v4-only;
the Pickup Label must differ from every Selection Label and configured end
label in the file and must stay out of its Workflow's ``require-all``,
``require-any`` and ``exclude`` filters; an enabled Workflow needs at least
one reachable annotated publishing Route and the optional note on every
possible first agent step. Explicit and implicit default labels pin
identical canonical bytes; a custom label moves the digest; legacy v4
records without the mapping keep recovering.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from coding_agent import cli
from coding_agent.ledger.store import digest_effective_configuration
from coding_agent.workflow.effective import (
    is_full_effective_configuration,
    sha256_effective_core,
)
from coding_agent.workflow.effective import _workflow_record
from coding_agent.workflow.jsonschema import build_workflow_json_schema
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    InputRef,
    RoundHandoffAnnotation,
    RoundHandoffConfig,
    RouteTarget,
    Workflow,
    WorkflowStep,
)
from support.effective_config import full_effective_configuration
from support.workflow_config import load_config

BASE = """\
version: 4
target:
  kind: github
  repository: OWNER/REPO
  base-branch: main
  token-env: GITHUB_TOKEN
models:
  sonnet:
    provider: anthropic
    model: claude-sonnet-5
    endpoint: messages
    effort: medium
    api-key-env: ANTHROPIC_API_KEY
    prices-usd-per-mtok: {input: 2.00, output: 10.00, cache-read: 0.20, cache-write: 2.50}
    context-window-tokens: 1000000
    context-threshold-tokens: 80000
    output-cap-tokens: 32000
"""

CEILING = (
    "    limits: {cost-usd: 6, effective-tokens: 1500000,"
    " tool-calls: 300, wall-clock-minutes: 30}\n"
)

DEFAULTS = """\
    defaults:
      max-executions: 1
      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}
"""

OUTCOMES = """\
    outcomes:
      unfinished: {labels: [needs-followup]}
"""

PUBLISHING_STEPS = """\
    steps:
      preflight:
        operation: check-readiness
        routes: {ready: process, not-ready: {end: blocked}}
      process:
        agent:
          tools: [workspace-read]
          inputs: [{optional: round-handoff.note}]
          results:
            done: {summary: document}
        routes: {done: ai-note}
      ai-note:
        agent:
          tools: [workspace-read]
          inputs: [process.facts]
          results:
            handoff: {note: document}
        routes: {handoff: handoff-push}
      handoff-push:
        operation: commit-and-push
        routes:
          pushed: {end: unfinished,
            round-handoff: {kind: ai-assisted, note: ai-note.handoff.note}}
          no-changes: {end: unfinished, round-handoff: {kind: hard}}
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "workflows.yaml"
    path.write_text(BASE + body, encoding="utf-8")
    return path


def _enabled_body(enablement: str, steps: str = PUBLISHING_STEPS) -> str:
    return (
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:implement}\n"
        + CEILING
        + enablement
        + "    start: preflight\n"
        + DEFAULTS
        + OUTCOMES
        + steps
    )


def _without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def _faults_text(faults: Any) -> str:
    return "\n".join(f"{fault.path}: {fault.detail}" for fault in faults)


# --- valid enablement ----------------------------------------------------------


def test_default_pickup_label_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The limit alone enables pickup under the default label."""
    _without_env(monkeypatch)
    path = _write(
        tmp_path,
        _enabled_body("    round-handoff: {max-automatic-pickups: 2}\n"),
    )

    config, faults = load_config(path)

    assert config is not None, _faults_text(faults)
    assert not faults
    enabled = config.workflows["implement"].round_handoff
    assert enabled == RoundHandoffConfig(
        max_automatic_pickups=2, pickup_label="round-finished"
    )


def test_custom_pickup_label_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit label is pinned beside the limit."""
    _without_env(monkeypatch)
    path = _write(
        tmp_path,
        _enabled_body(
            "    round-handoff: {max-automatic-pickups: 3, pickup-label: team:resume}\n"
        ),
    )

    config, faults = load_config(path)

    assert config is not None, _faults_text(faults)
    assert not faults
    enabled = config.workflows["implement"].round_handoff
    assert enabled == RoundHandoffConfig(
        max_automatic_pickups=3, pickup_label="team:resume"
    )


def test_null_enablement_mapping_disables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A null mapping reads as omitted: declarations keep disabled semantics."""
    _without_env(monkeypatch)
    path = _write(tmp_path, _enabled_body("    round-handoff:\n"))

    config, faults = load_config(path)

    assert config is not None, _faults_text(faults)
    assert not faults
    assert config.workflows["implement"].round_handoff is None


def test_omitted_enablement_disables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the mapping the Workflow carries no enablement."""
    _without_env(monkeypatch)
    path = _write(tmp_path, _enabled_body(""))

    config, faults = load_config(path)

    assert config is not None, _faults_text(faults)
    assert not faults
    assert config.workflows["implement"].round_handoff is None


def test_enabled_file_validates_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`agent config validate` accepts the enabled shape offline."""
    _without_env(monkeypatch)
    path = _write(
        tmp_path,
        _enabled_body("    round-handoff: {max-automatic-pickups: 2}\n"),
    )

    assert cli.main(["config", "validate", "--config", str(path)]) == 0
    assert "config validate: OK" in capsys.readouterr().out


# --- the limit and the label ---------------------------------------------------


@pytest.mark.parametrize("limit", ["'2'", '"2"', "2.0", "true", "True", "null", "~"])
def test_non_integer_limit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    """The limit is a positive integer, never a string, float, bool or null."""
    _without_env(monkeypatch)
    path = _write(
        tmp_path, _enabled_body(f"    round-handoff: {{max-automatic-pickups: {limit}}}\n")
    )

    config, faults = load_config(path)

    assert config is None
    text = _faults_text(faults)
    assert "max-automatic-pickups" in text
    assert "positive integer" in text


@pytest.mark.parametrize("limit", ["0", "-1", "-40"])
def test_non_positive_limit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    """Zero and negative limits are refused."""
    _without_env(monkeypatch)
    path = _write(
        tmp_path, _enabled_body(f"    round-handoff: {{max-automatic-pickups: {limit}}}\n")
    )

    config, faults = load_config(path)

    assert config is None
    text = _faults_text(faults)
    assert "max-automatic-pickups" in text
    assert "positive integer" in text


def test_missing_limit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The limit is required: a label alone enables nothing."""
    _without_env(monkeypatch)
    path = _write(
        tmp_path,
        _enabled_body("    round-handoff: {pickup-label: team:resume}\n"),
    )

    config, faults = load_config(path)

    assert config is None
    assert "max-automatic-pickups" in _faults_text(faults)


@pytest.mark.parametrize("label", ["''", '""', "42", "true", "null", "~"])
def test_empty_or_non_string_label_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """The Pickup Label is a non-empty string."""
    _without_env(monkeypatch)
    path = _write(
        tmp_path,
        _enabled_body(
            f"    round-handoff: {{max-automatic-pickups: 2, pickup-label: {label}}}\n"
        ),
    )

    config, faults = load_config(path)

    assert config is None
    assert "pickup-label" in _faults_text(faults)


def test_unknown_enablement_field_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown enablement keys are refused."""
    _without_env(monkeypatch)
    path = _write(
        tmp_path,
        _enabled_body(
            "    round-handoff: {max-automatic-pickups: 2, retries: 3}\n"
        ),
    )

    config, faults = load_config(path)

    assert config is None
    text = _faults_text(faults)
    assert "round-handoff" in text
    assert "retries" in text


def test_non_mapping_enablement_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scalar enablement is refused."""
    _without_env(monkeypatch)
    path = _write(tmp_path, _enabled_body("    round-handoff: 2\n"))

    config, faults = load_config(path)

    assert config is None
    assert "round-handoff" in _faults_text(faults)


# --- issue-only -----------------------------------------------------------------


def test_pull_request_enablement_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pull-request Workflows never enable Round Handoff."""
    _without_env(monkeypatch)
    body = (
        "workflows:\n"
        "  review:\n"
        "    model: sonnet\n"
        "    input: {type: pull-request, label: agent:review}\n"
        + CEILING
        + "    round-handoff: {max-automatic-pickups: 2}\n"
        + "    start: preflight\n"
        + DEFAULTS
        + "    steps:\n"
          "      preflight:\n"
          "        operation: check-readiness\n"
          "        routes: {ready: handoff-push, not-ready: {end: blocked}}\n"
          "      handoff-push:\n"
          "        operation: commit-and-push\n"
          "        routes:\n"
          "          pushed: {end: unfinished, round-handoff: {kind: hard}}\n"
          "          no-changes: {end: completed}\n"
          "          stale-head: {end: failed}\n"
    )

    config, faults = load_config(_write(tmp_path, body))

    assert config is None
    text = _faults_text(faults)
    assert "round-handoff" in text
    assert "pull-request" in text


# --- label collisions -------------------------------------------------------------


def test_pickup_label_equal_to_own_selection_label_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Pickup Label never equals its own Selection Label."""
    _without_env(monkeypatch)
    path = _write(
        tmp_path,
        _enabled_body(
            "    round-handoff: {max-automatic-pickups: 2, pickup-label: agent:implement}\n"
        ),
    )

    config, faults = load_config(path)

    assert config is None
    text = _faults_text(faults)
    assert "pickup-label" in text
    assert "agent:implement" in text


def test_pickup_label_equal_to_another_selection_label_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Pickup Label never equals any Selection Label in the file."""
    _without_env(monkeypatch)
    body = (
        _enabled_body(
            "    round-handoff: {max-automatic-pickups: 2, pickup-label: agent:triage}\n"
        )
        + "  triage:\n"
          "    model: sonnet\n"
          "    input: {type: issue, label: agent:triage}\n"
        + CEILING
        + "    start: only\n"
        + DEFAULTS
        + "    steps:\n"
          "      only:\n"
          "        agent:\n"
          "          tools: [workspace-read]\n"
          "          results: {done: {}}\n"
          "        routes: {done: {end: completed}}\n"
    )

    config, faults = load_config(_write(tmp_path, body))

    assert config is None
    text = _faults_text(faults)
    assert "pickup-label" in text
    assert "agent:triage" in text


def test_pickup_label_equal_to_an_end_label_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Pickup Label never equals a configured end label."""
    _without_env(monkeypatch)
    path = _write(
        tmp_path,
        _enabled_body(
            "    round-handoff: {max-automatic-pickups: 2, pickup-label: needs-followup}\n"
        ),
    )

    config, faults = load_config(path)

    assert config is None
    text = _faults_text(faults)
    assert "needs-followup" in text


def test_pickup_label_in_input_filters_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Pickup Label stays out of require-all, require-any and exclude."""
    _without_env(monkeypatch)
    body = (
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:implement, require-all: [round-finished]}\n"
        + CEILING
        + "    round-handoff: {max-automatic-pickups: 2}\n"
        + "    start: preflight\n"
        + DEFAULTS
        + OUTCOMES
        + PUBLISHING_STEPS
    )

    config, faults = load_config(_write(tmp_path, body))

    assert config is None
    text = _faults_text(faults)
    assert "round-finished" in text
    assert "require-all" in text


@pytest.mark.parametrize("field", ["require-any", "exclude"])
def test_pickup_label_in_other_filters_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    """The remaining filters refuse the Pickup Label too."""
    _without_env(monkeypatch)
    body = (
        "workflows:\n"
        "  implement:\n"
        "    model: sonnet\n"
        f"    input: {{type: issue, label: agent:implement, {field}: [round-finished]}}\n"
        + CEILING
        + "    round-handoff: {max-automatic-pickups: 2}\n"
        + "    start: preflight\n"
        + DEFAULTS
        + OUTCOMES
        + PUBLISHING_STEPS
    )

    config, faults = load_config(_write(tmp_path, body))

    assert config is None
    text = _faults_text(faults)
    assert "round-finished" in text
    assert field in text


def test_shared_pickup_label_across_workflows_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two Workflows may share one Pickup Label under distinct Selection Labels."""
    _without_env(monkeypatch)
    second = (
        "  second:\n"
        "    model: sonnet\n"
        "    input: {type: issue, label: agent:second}\n"
        + CEILING
        + "    round-handoff: {max-automatic-pickups: 1, pickup-label: team:resume}\n"
        + "    start: preflight\n"
        + DEFAULTS
        + "    steps:\n"
          "      preflight:\n"
          "        operation: check-readiness\n"
          "        routes: {ready: work, not-ready: {end: blocked}}\n"
          "      work:\n"
          "        agent:\n"
          "          tools: [workspace-read]\n"
          "          inputs: [{optional: round-handoff.note}]\n"
          "          results: {done: {}}\n"
          "        routes: {done: push}\n"
          "      push:\n"
          "        operation: commit-and-push\n"
          "        routes:\n"
          "          pushed: {end: unfinished, round-handoff: {kind: hard}}\n"
          "          no-changes: {end: completed}\n"
    )
    body = (
        _enabled_body(
            "    round-handoff: {max-automatic-pickups: 2, pickup-label: team:resume}\n"
        )
        + second
    )

    config, faults = load_config(_write(tmp_path, body))

    assert config is not None, _faults_text(faults)
    assert not faults


# --- the publishing Route and the note ----------------------------------------------


def test_enabled_without_a_publishing_route_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enablement needs at least one annotated publishing Route."""
    _without_env(monkeypatch)
    steps = PUBLISHING_STEPS.replace(
        "pushed: {end: unfinished,\n"
        "            round-handoff: {kind: ai-assisted, note: ai-note.handoff.note}}",
        "pushed: {end: unfinished}",
    ).replace(
        "no-changes: {end: unfinished, round-handoff: {kind: hard}}",
        "no-changes: {end: unfinished}",
    )
    path = _write(
        tmp_path,
        _enabled_body("    round-handoff: {max-automatic-pickups: 2}\n", steps),
    )

    config, faults = load_config(path)

    assert config is None
    text = _faults_text(faults)
    assert "round-handoff" in text
    assert "publishing Route" in text


def test_annotation_on_an_unreachable_step_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An annotation off the reachable graph cannot publish a round."""
    _without_env(monkeypatch)
    steps = (
        "    steps:\n"
        "      preflight:\n"
        "        operation: check-readiness\n"
        "        routes: {ready: work, not-ready: {end: blocked}}\n"
        "      work:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          inputs: [{optional: round-handoff.note}]\n"
        "          results: {done: {}}\n"
        "        routes: {done: push}\n"
        "      push:\n"
        "        operation: commit-and-push\n"
        "        routes:\n"
        "          pushed: {end: unfinished}\n"
        "          no-changes: {end: completed}\n"
        "      stranded:\n"
        "        operation: commit-and-push\n"
        "        routes:\n"
        "          pushed: {end: unfinished, round-handoff: {kind: hard}}\n"
        "          no-changes: {end: completed}\n"
    )
    path = _write(
        tmp_path,
        _enabled_body("    round-handoff: {max-automatic-pickups: 2}\n", steps),
    )

    config, faults = load_config(path)

    assert config is None
    assert "unreachable" in _faults_text(faults)


def test_enabled_requires_the_note_on_every_first_agent_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each path's first agent step declares the optional note."""
    _without_env(monkeypatch)
    steps = (
        "    steps:\n"
        "      preflight:\n"
        "        operation: check-readiness\n"
        "        routes: {ready: left, not-ready: right}\n"
        "      left:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          inputs: [{optional: round-handoff.note}]\n"
        "          results: {done: {}}\n"
        "        routes: {done: push}\n"
        "      right:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          results: {done: {}}\n"
        "        routes: {done: push}\n"
        "      push:\n"
        "        operation: commit-and-push\n"
        "        routes:\n"
        "          pushed: {end: unfinished, round-handoff: {kind: hard}}\n"
        "          no-changes: {end: completed}\n"
    )
    path = _write(
        tmp_path,
        _enabled_body("    round-handoff: {max-automatic-pickups: 2}\n", steps),
    )

    config, faults = load_config(path)

    assert config is None
    text = _faults_text(faults)
    assert "round-handoff.note" in text
    assert "right" in text


def test_note_on_every_first_agent_step_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both first steps declaring the note is the enabled happy path."""
    _without_env(monkeypatch)
    steps = (
        "    steps:\n"
        "      preflight:\n"
        "        operation: check-readiness\n"
        "        routes: {ready: left, not-ready: right}\n"
        "      left:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          inputs: [{optional: round-handoff.note}]\n"
        "          results: {done: {}}\n"
        "        routes: {done: push}\n"
        "      right:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          inputs: [{optional: round-handoff.note}]\n"
        "          results: {done: {}}\n"
        "        routes: {done: push}\n"
        "      push:\n"
        "        operation: commit-and-push\n"
        "        routes:\n"
        "          pushed: {end: unfinished, round-handoff: {kind: hard}}\n"
        "          no-changes: {end: completed}\n"
    )
    path = _write(
        tmp_path,
        _enabled_body("    round-handoff: {max-automatic-pickups: 2}\n", steps),
    )

    config, faults = load_config(path)

    assert config is not None, _faults_text(faults)
    assert not faults


# --- schema round-trip -----------------------------------------------------------------


def _validator() -> Draft202012Validator:
    return Draft202012Validator(build_workflow_json_schema())


def test_enabled_file_passes_the_generated_schema(tmp_path: Path) -> None:
    """The enablement mapping is a structural schema shape."""
    path = _write(
        tmp_path,
        _enabled_body("    round-handoff: {max-automatic-pickups: 2}\n"),
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert _validator().is_valid(data), list(_validator().iter_errors(data))


def test_custom_label_passes_the_generated_schema(tmp_path: Path) -> None:
    """A custom Pickup Label is structural too."""
    path = _write(
        tmp_path,
        _enabled_body(
            "    round-handoff: {max-automatic-pickups: 2, pickup-label: team:resume}\n"
        ),
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert _validator().is_valid(data), list(_validator().iter_errors(data))


def test_unknown_enablement_field_fails_the_generated_schema(tmp_path: Path) -> None:
    """Unknown enablement keys fail structurally as well as statically."""
    path = _write(
        tmp_path,
        _enabled_body("    round-handoff: {max-automatic-pickups: 2}\n"),
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    data["workflows"]["implement"]["round-handoff"]["retries"] = 3

    assert not _validator().is_valid(data)


# --- effective configuration --------------------------------------------------------------


def _implement_workflow(
    *, pickup_label: str | None = None, limit: int = 2
) -> Workflow:
    """A typed enabled Workflow for record tests."""
    push_routes = {
        "pushed": RouteTarget(
            end="unfinished",
            round_handoff=RoundHandoffAnnotation(
                kind="ai-assisted", note="ai-note.handoff.note"
            ),
        ),
        "no-changes": RouteTarget(
            end="unfinished", round_handoff=RoundHandoffAnnotation(kind="hard")
        ),
    }
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:implement"),
        ceiling=AttemptCeiling(
            cost_usd=20, effective_tokens=1000, tool_calls=10, wall_clock_minutes=5
        ),
        outcomes={"unfinished": ("needs-followup",)},
        start="preflight",
        defaults_routes={
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
        round_handoff=RoundHandoffConfig(
            max_automatic_pickups=limit,
            pickup_label=(
                pickup_label if pickup_label is not None else "round-finished"
            ),
        ),
        steps={
            "preflight": WorkflowStep(
                id="preflight",
                operation="check-readiness",
                routes={
                    "ready": RouteTarget(step="process"),
                    "not-ready": RouteTarget(end="blocked"),
                },
            ),
            "process": WorkflowStep(
                id="process",
                agent=Agent(
                    tools=("workspace-read",),
                    inputs=(InputRef(reference="round-handoff.note", optional=True),),
                    results={"done": {"summary": "document"}},
                ),
                routes={"done": RouteTarget(step="ai-note")},
            ),
            "ai-note": WorkflowStep(
                id="ai-note",
                agent=Agent(
                    tools=("workspace-read",),
                    inputs=(InputRef(reference="process.facts", optional=False),),
                    results={"handoff": {"note": "document"}},
                ),
                routes={"handoff": RouteTarget(step="handoff-push")},
            ),
            "handoff-push": WorkflowStep(
                id="handoff-push",
                operation="commit-and-push",
                routes=push_routes,
            ),
        },
    )


def _implement_record(*args: Any, **kwargs: Any) -> dict[str, object]:
    record = full_effective_configuration(_implement_workflow(*args, **kwargs))
    assert is_full_effective_configuration(record) is True
    workflow = record["workflow"]
    assert isinstance(workflow, dict)
    return workflow


def test_explicit_default_label_matches_implicit_digest() -> None:
    """Spelling the default label pins the same canonical bytes."""
    implicit = _workflow_record(_implement_workflow(pickup_label=None))
    explicit = _workflow_record(
        _implement_workflow(pickup_label="round-finished")
    )

    assert implicit["round_handoff"] == explicit["round_handoff"]
    assert sha256_effective_core({"workflow": implicit}) == sha256_effective_core(
        {"workflow": explicit}
    )


def test_custom_label_moves_the_digest() -> None:
    """A custom Pickup Label belongs to the pinned identity core."""
    default = _workflow_record(_implement_workflow(pickup_label=None))
    custom = _workflow_record(_implement_workflow(pickup_label="team:resume"))

    assert custom["round_handoff"] == {
        "pickup_label": "team:resume",
        "max_automatic_pickups": 2,
    }
    assert sha256_effective_core({"workflow": default}) != sha256_effective_core(
        {"workflow": custom}
    )


def test_limit_moves_the_digest() -> None:
    """The pickup limit belongs to the pinned identity core."""
    two = _workflow_record(_implement_workflow(limit=2))
    three = _workflow_record(_implement_workflow(limit=3))

    assert sha256_effective_core({"workflow": two}) != sha256_effective_core(
        {"workflow": three}
    )


def test_legacy_record_without_enablement_is_full() -> None:
    """A v4 record from before the feature keeps recovering."""
    record = full_effective_configuration(
        _implement_workflow(pickup_label=None, limit=2)
    )
    workflow_value = record["workflow"]
    assert isinstance(workflow_value, dict)
    workflow = dict(workflow_value)
    assert "round_handoff" in workflow
    legacy_workflow = {
        key: value for key, value in workflow.items() if key != "round_handoff"
    }
    legacy = dict(record)
    legacy["workflow"] = legacy_workflow

    assert is_full_effective_configuration(legacy) is True


def test_valid_enablement_record_is_full() -> None:
    """A well-shaped enablement section recovers."""
    record = full_effective_configuration(_implement_workflow())

    assert is_full_effective_configuration(record) is True
    workflow = record["workflow"]
    assert isinstance(workflow, dict)
    assert workflow["round_handoff"] == {
        "pickup_label": "round-finished",
        "max_automatic_pickups": 2,
    }


@pytest.mark.parametrize(
    "section",
    [
        {"pickup_label": "", "max_automatic_pickups": 2},
        {"pickup_label": 42, "max_automatic_pickups": 2},
        {"pickup_label": None, "max_automatic_pickups": 2},
        {"pickup_label": "round-finished", "max_automatic_pickups": 0},
        {"pickup_label": "round-finished", "max_automatic_pickups": -1},
        {"pickup_label": "round-finished", "max_automatic_pickups": True},
        {"pickup_label": "round-finished", "max_automatic_pickups": "2"},
        {"pickup_label": "round-finished"},
        {"max_automatic_pickups": 2},
        {"pickup_label": "round-finished", "max_automatic_pickups": 2, "extra": 1},
        "round-finished",
        42,
        [],
    ],
)
def test_malformed_enablement_record_is_not_full(section: Any) -> None:
    """A corrupt enablement section refuses recovery instead of guessing."""
    record = full_effective_configuration(_implement_workflow())
    workflow_value = record["workflow"]
    assert isinstance(workflow_value, dict)
    workflow = dict(workflow_value)
    workflow["round_handoff"] = section
    corrupt = dict(record)
    corrupt["workflow"] = workflow

    assert is_full_effective_configuration(corrupt) is False


def test_enabled_record_digest_differs_from_legacy() -> None:
    """Enabling pickup moves the Attempt digest: legacy bytes stay stable."""
    legacy_workflow = _implement_workflow()
    legacy_workflow = Workflow(
        id=legacy_workflow.id,
        model=legacy_workflow.model,
        input=legacy_workflow.input,
        ceiling=legacy_workflow.ceiling,
        outcomes=legacy_workflow.outcomes,
        start=legacy_workflow.start,
        defaults_max_executions=legacy_workflow.defaults_max_executions,
        defaults_routes=legacy_workflow.defaults_routes,
        steps=legacy_workflow.steps,
    )
    enabled = full_effective_configuration(_implement_workflow())
    legacy = full_effective_configuration(legacy_workflow)

    assert digest_effective_configuration(enabled) != digest_effective_configuration(
        legacy
    )
    legacy_workflow_record = legacy["workflow"]
    assert isinstance(legacy_workflow_record, dict)
    assert "round_handoff" not in legacy_workflow_record
