"""Typed reads of the Effective Configuration record (issue #655).

The record module owns every typed read: the Attempt Ceiling with its
legacy precedence, the step policy and usable thresholds, the route
round-handoff kind, the pinned-model values, the parallelism limit and
the project-profile / round-handoff sections. Recovery still refuses
partial records through the strict validator; these readers stay
tolerant of older-release shapes (ADR 0017).
"""

from __future__ import annotations

from typing import Any, cast

from coding_agent.workflow.effective import (
    effective_ceiling,
    effective_step_entry,
    effective_steps,
    is_full_effective_configuration,
    is_operation_step,
    make_test_record,
    max_parallel_subagents,
    model_context_window_tokens,
    pin_default_usable_threshold,
    pinned_context_threshold_tokens,
    pinned_context_window,
    pinned_model_section,
    project_profile_section,
    record_context_margins,
    round_handoff_policy,
    round_handoff_section,
    route_round_handoff_kind,
    selected_usable_thresholds,
    step_context_policy,
)


def _legacy_limits_record(
    limits: dict[str, Any],
    *,
    max_parallel: int | None = None,
) -> dict[str, Any]:
    """A pre-v4 record: ``limits`` with ``tokens`` instead of ``ceiling``."""
    workflow: dict[str, Any] = {"limits": dict(limits)}
    if max_parallel is not None:
        workflow["limits"]["max_parallel_subagents"] = max_parallel
    return {"workflow": workflow}


def test_ceiling_reads_current_shape() -> None:
    """The four meters map to seconds with absent-means-None."""
    ceiling = effective_ceiling(make_test_record())
    assert ceiling == {
        "cost_usd": 5.0,
        "effective_tokens": 100_000,
        "tool_calls": 200,
        "wall_clock_seconds": 3600.0,
    }


def test_ceiling_prefers_new_key_over_legacy() -> None:
    """``ceiling`` beats ``limits`` where a record carries both."""
    record = make_test_record()
    workflow = dict(cast(dict[str, Any], record["workflow"]))
    workflow["limits"] = {
        "cost_usd": 1.0,
        "effective_tokens": 10,
        "tokens": 11,
        "tool_calls": 1,
        "wall_clock_minutes": 1,
    }
    record["workflow"] = workflow
    ceiling = effective_ceiling(record)
    assert ceiling["cost_usd"] == 5.0
    assert ceiling["effective_tokens"] == 100_000


def test_ceiling_falls_back_to_limits_and_tokens() -> None:
    """Legacy ``limits`` + ``tokens`` rows read through the same reader."""
    ceiling = effective_ceiling(
        _legacy_limits_record(
            {
                "tokens": 500_000,
                "cost_usd": 2.0,
                "wall_clock_minutes": 30,
                "tool_calls": 200,
            }
        )
    )
    assert ceiling == {
        "cost_usd": 2.0,
        "effective_tokens": 500_000,
        "tool_calls": 200,
        "wall_clock_seconds": 1800.0,
    }


def test_ceiling_prefers_effective_tokens_over_tokens() -> None:
    """``effective_tokens`` beats raw ``tokens`` where both are present."""
    ceiling = effective_ceiling(
        _legacy_limits_record({"tokens": 1, "effective_tokens": 2})
    )
    assert ceiling["effective_tokens"] == 2


def test_ceiling_junk_meters_read_none_never_zero() -> None:
    """Booleans and non-numeric meters read as ``None``, never zero."""
    ceiling = effective_ceiling(
        {
            "workflow": {
                "ceiling": {
                    "effective_tokens": "lots",
                    "cost_usd": True,
                    "wall_clock_minutes": "soon",
                    "tool_calls": False,
                }
            }
        }
    )
    assert ceiling == {
        "cost_usd": None,
        "effective_tokens": None,
        "tool_calls": None,
        "wall_clock_seconds": None,
    }


def test_ceiling_missing_record_reads_all_none() -> None:
    """A missing or malformed record reads an absent ceiling."""
    assert effective_ceiling(None) == {
        "cost_usd": None,
        "effective_tokens": None,
        "tool_calls": None,
        "wall_clock_seconds": None,
    }
    assert effective_ceiling({}) == {
        "cost_usd": None,
        "effective_tokens": None,
        "tool_calls": None,
        "wall_clock_seconds": None,
    }


def test_max_parallel_prefers_workflow_key() -> None:
    """The top-level workflow key beats the legacy ``limits`` placement."""
    record = make_test_record(max_parallel_subagents_value=3)
    workflow = dict(cast(dict[str, Any], record["workflow"]))
    workflow["limits"] = {"max_parallel_subagents": 7}
    record["workflow"] = workflow
    assert max_parallel_subagents(record) == 3


def test_max_parallel_falls_back_to_legacy_limits() -> None:
    """Older releases pin the limit under ``workflow.limits``."""
    record = _legacy_limits_record({}, max_parallel=5)
    assert max_parallel_subagents(record) == 5


def test_max_parallel_defaults_where_missing_or_malformed() -> None:
    """Missing, boolean, non-integer or ``< 1`` values read the default."""
    assert max_parallel_subagents(None) == 2
    assert max_parallel_subagents({}) == 2
    assert max_parallel_subagents(make_test_record()) == 2
    for bad in (True, 0, -1, "many", 2.5):
        record: dict[str, Any] = {"workflow": {"max_parallel_subagents": bad}}
        assert max_parallel_subagents(record) == 2


def test_steps_and_entry() -> None:
    """Steps map and single-entry reads route through the record."""
    record = make_test_record()
    steps = effective_steps(record)
    assert set(steps) == {"build"}
    assert effective_step_entry(record, "build")["id"] == "build"
    assert effective_step_entry(record, "missing") == {}
    assert effective_step_entry(record, None) == {}
    assert effective_steps(None) == {}


def test_step_context_policy_and_thresholds() -> None:
    """A step policy resolves selected and hard usable thresholds."""
    steps: dict[str, Any] = {
        "process": {
            "id": "process",
            "agent": {"tools": ["workspace-read"]},
            "operation": None,
            "routes": {"done": {"end": "completed"}},
            "context_thresholds": {"hard_tokens": 40_000, "soft_tokens": 30_000},
        }
    }
    record = make_test_record(steps=steps)
    policy = step_context_policy(record, "process")
    assert policy == {"hard_tokens": 40_000, "soft_tokens": 30_000}
    assert step_context_policy(record, "missing") is None
    selected, hard = selected_usable_thresholds(record, "process")
    # Soft 30000 less the 1000 safety margin and 500 request overhead;
    # hard 40000 less the same margins.
    assert selected == 30_000 - 1_000 - 500
    assert hard == 40_000 - 1_000 - 500


def test_selected_thresholds_fall_back_to_pin_default() -> None:
    """Steps without a policy resolve the pin-default usable threshold."""
    record = make_test_record()
    selected, hard = selected_usable_thresholds(record, "build")
    assert selected == 80_000 - 1_000 - 500
    assert hard is None
    assert pin_default_usable_threshold(record) == 80_000 - 1_000 - 500
    assert pin_default_usable_threshold(None) is None


def test_is_operation_step() -> None:
    """Operation steps read ``True``; agent steps and missing ones do not."""
    steps: dict[str, Any] = {
        "agent-step": {
            "id": "agent-step",
            "agent": {"tools": ["workspace-read"]},
            "operation": None,
            "routes": {"done": {"end": "completed"}},
        },
        "op-step": {
            "id": "op-step",
            "agent": None,
            "operation": {"kind": "comment"},
            "routes": {"done": {"end": "completed"}},
        },
    }
    record = make_test_record(steps=steps)
    assert is_operation_step(record, "op-step") is True
    assert is_operation_step(record, "agent-step") is False
    assert is_operation_step(record, "missing") is False
    assert is_operation_step(None, "op-step") is False


def test_route_round_handoff_kind() -> None:
    """The taken route's round-handoff kind reads through the record."""
    steps: dict[str, Any] = {
        "process": {
            "id": "process",
            "agent": {"tools": ["workspace-read"]},
            "operation": None,
            "routes": {
                "done": {
                    "end": "completed",
                    "round_handoff": {"kind": "hard", "note": "n"},
                },
                "plain": {"end": "completed"},
            },
        }
    }
    record = make_test_record(steps=steps)
    assert route_round_handoff_kind(record, "process", "done") == "hard"
    assert route_round_handoff_kind(record, "process", "plain") is None
    assert route_round_handoff_kind(record, "process", "missing") is None
    assert route_round_handoff_kind(record, "missing", "done") is None
    assert route_round_handoff_kind(record, None, "done") is None


def test_pinned_model_reads() -> None:
    """Model section, window triple, threshold and margins read through."""
    record = make_test_record()
    section = pinned_model_section(record)
    assert section["model_context_window_tokens"] == 200_000
    assert pinned_context_window(record)["threshold_tokens"] == 80_000
    assert model_context_window_tokens(record) == 200_000
    assert pinned_context_threshold_tokens(record) == 80_000
    safety, overhead = record_context_margins(record)
    assert (safety, overhead) == (1_000, 500)
    assert pinned_model_section(None) == {}
    assert pinned_context_window(None) == {}
    assert model_context_window_tokens(None) is None
    assert pinned_context_threshold_tokens(None) is None


def test_context_margins_fall_back_to_code_defaults() -> None:
    """A record without a pinned window reads the code-owned margins."""
    from coding_agent.runtime.context_budget import code_owned_margins

    safety, overhead = record_context_margins(None)
    assert (safety, overhead) == code_owned_margins()


def test_round_handoff_section_and_policy() -> None:
    """The enablement section and policy read through the record."""
    record = make_test_record(
        round_handoff={"pickup_label": "round-finished", "max_automatic_pickups": 2}
    )
    section = round_handoff_section(record)
    assert section["pickup_label"] == "round-finished"
    assert round_handoff_policy(record) == ("round-finished", 2)
    plain = make_test_record()
    assert round_handoff_section(plain) == {}
    assert round_handoff_policy(plain) is None
    assert round_handoff_policy(None) is None
    malformed: dict[str, Any] = {
        "workflow": {"round_handoff": {"pickup_label": "", "max_automatic_pickups": 0}}
    }
    assert round_handoff_policy(malformed) is None


def test_project_profile_section_passes_through() -> None:
    """The raw section passes through; absent reads ``None``."""
    profile = {"locator": "profile:main", "sha256": "a" * 64, "size_bytes": 12}
    record = make_test_record(project_profile=profile)
    assert project_profile_section(record) == profile
    assert project_profile_section(make_test_record()) is None
    assert project_profile_section(None) is None


def test_builder_record_passes_strict_validator() -> None:
    """Builder records are full current-shape records; partials are not."""
    assert is_full_effective_configuration(make_test_record()) is True
    assert is_full_effective_configuration(None) is False
    assert is_full_effective_configuration({}) is False
    assert is_full_effective_configuration({"workflow": {"ceiling": {}}}) is False
    partial = make_test_record()
    del partial["pinned_model"]
    assert is_full_effective_configuration(partial) is False
    wrong_version = make_test_record()
    wrong_version["version"] = 1
    assert is_full_effective_configuration(wrong_version) is False
