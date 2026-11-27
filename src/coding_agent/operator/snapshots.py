"""Public operator-contract assembly from a :class:`RunLedgerReader` (#435).

Contract: the operator contract's ``status``, ``identity`` and ``attempts``
shapes. Every function here reads the Run Ledger's lifecycle tables
(``attempts``, ``outcomes``, ``step_executions``,
``step_effects``, ``step_transitions``, ``run_ledger_events``) only through
a :class:`~coding_agent.ledger.reader.RunLedgerReader`, and the operator
tables (``operator_instance``, ``operator_halt``) only through
:mod:`coding_agent.operator.store`'s read side. Intake and Operator Command
reads go through :mod:`coding_agent.operator.commands`, Contract Events
through :mod:`coding_agent.operator.events`. No SQL lives in this module:
the table layout stays in :mod:`coding_agent.ledger.queries`.

The live listener (:func:`build_status`, :func:`build_identity`) and the
Offline Read copy (:func:`build_offline_status`,
:func:`build_offline_identity`, :func:`attempts_page`) share these
builders, so live and offline output cannot drift apart.
"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from coding_agent.ledger.reader import RunLedgerReader
from coding_agent.operator.contract import (
    IDENTITY_CONTRACT,
    figure,
    to_z,
    utc_now_z,
)
from coding_agent.operator.native_events import native_events_for_attempt
from coding_agent.operator.live import LiveView
from coding_agent.operator.step_projection import (
    ceiling_from_effective_configuration,
)
from coding_agent.operator.store import (
    OPERATOR_BACKEND,
    agent_version,
    read_instance_repository,
    read_instance_row,
    read_last_halt,
    read_provider_capability_reports,
)

_STATUS_SNAPSHOT_LIMIT_BYTES = 256 * 1024
"""The Status Snapshot cap: the serialized ``status`` never exceeds 256 KiB.

Trimming drops entries in contract order (``recent_events`` first, then
``recent_commands``, oldest first) and marks the snapshot ``truncated``.
"""

MAX_LIST_LIMIT = 100
"""``attempts list`` never returns more than this per page."""

_UNAVAILABLE = "unavailable"
"""A figure the ledger or live view could carry but does not yet.

Unknown values are never reported as zero: before the first usage flush the
Attempt simply has no spend yet.
"""

_UPPER_BOUND = "estimated"
"""A figure that includes a worst-case charge (§11.4).

A model response without usage (or with partial usage) settles at its
Admission hold's worst case, so the Attempt's token and cost figures are a
ceiling on the true spend, never a measured value. The contract's figure
basis enum has no ``upper-bound`` value, so worst-case charges report
``estimated`` with the reason. ``unavailable`` is kept for figures the
Attempt has not produced yet; it is never produced for a worst-case charge.
"""

_TURNS_ESTIMATED_REASON = (
    "the count may include requests that received no response"
)
"""The ``turns`` reason where the per-origin spend predates the split count.

A ``main`` cell recorded before ``requests_without_response`` existed
cannot tell failed requests apart from turns, so the count reports
``estimated`` where that cell carries a worst-case charge.
"""

_CONTEXT_WINDOW_REASON = (
    "the Pinned Model's Model Context Window, from the pin's "
    "context-window-tokens"
)
"""The ``context_window`` reason: the largest request the model accepts.

A request and its response share it (contract §9.2, §16); the context
threshold compares against the Usable Threshold (``context_threshold``),
never against this figure.
"""

_CONTEXT_THRESHOLD_REASON = (
    "the Usable Threshold: the Context Threshold less the safety margin "
    "and the request overhead"
)
"""The ``context_threshold`` reason: the compared value's ceiling (§16).

The value the engine compares ``context_used`` with: the main
conversation's context size at which the engine stops growing it.
"""

_HARD_USABLE_THRESHOLD_REASON = (
    "the Hard Usable Threshold: the step's Hard Context Threshold less "
    "the safety margin and the request overhead"
)
"""The ``native.context_hard_usable_threshold_tokens`` reason (§16 v1.2.1)."""

_PICKUP_LIMIT_REASON = "the human-authorised pickup limit for the series"
"""The ``native.pickup_limit`` reason: the configured Round Handoff cap (§16)."""

_PICKUP_USED_REASON = "pickups the series consumed before this round"
"""The ``native.pickups_used`` reason: the recorded terminal block (§16)."""

_RECEIVER_NOT_EVALUATED_REASON = (
    "the receiver has not evaluated its own request yet"
)
"""The ``context_used`` reason before an inheriting receiver measures (§16)."""

_ANCHORED_CONTEXT_REASON = "anchored on provider-reported input"
"""The ``context_used`` reason with a measured response anchoring it (§16).

The value is the last provider-reported input of the conversation plus a
local estimate of what was appended since.
"""

_LOCAL_ESTIMATE_CONTEXT_REASON = "local estimate, no measured response yet"
"""The ``context_used`` reason before the first measured response (§16)."""

_SINCE_UPGRADE_REASON = "covers only requests recorded since upgrade"
"""The partial-coverage reason for ``context_peak`` and ``subagents_ended``.

An Attempt whose Run Ledger holds requests from before the 1.1 producer
— model responses without a Conversation Context record, or spend without
per-origin cells — computes these figures only from
the records since the upgrade, never mixed with the old measures.
"""


class InvalidCursor(ValueError):
    """A ``--before`` cursor that does not decode (cursors are opaque)."""


def encode_before_cursor(last_rowid: int) -> str:
    """The opaque ``--before`` cursor for the page ending at ``last_rowid``."""
    raw = json.dumps({"v": 1, "last": last_rowid}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_before_cursor(cursor: str) -> int:
    """The rowid bound a ``--before`` cursor carries.

    Raises :class:`InvalidCursor` where the cursor does not decode: cursors
    are opaque, so anything unrecognised is rejected rather than guessed.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        payload = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursor(f"invalid --before cursor {cursor!r}") from exc
    if not isinstance(payload, dict):
        raise InvalidCursor(f"invalid --before cursor {cursor!r}")
    if payload.get("v") != 1:
        raise InvalidCursor(f"invalid --before cursor {cursor!r}")
    last = payload.get("last")
    if isinstance(last, bool) or not isinstance(last, int) or last < 1:
        raise InvalidCursor(f"invalid --before cursor {cursor!r}")
    return last


def check_limit(limit: int) -> None:
    """Reject a page size outside ``1..100``."""
    if not isinstance(limit, bool) and isinstance(limit, int):
        if 1 <= limit <= MAX_LIST_LIMIT:
            return
    raise ValueError(f"invalid --limit {limit!r}: expected 1..{MAX_LIST_LIMIT}")


def trim_status_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    """Cap the serialized snapshot at 256 KiB, in contract order.

    The unbounded tails trim first -- ``recent_events``, then
    ``recent_commands`` -- oldest entries first (both lists run oldest
    first). The snapshot that lost anything reports ``truncated: true``.
    """
    if len(json.dumps(result).encode("utf-8")) <= _STATUS_SNAPSHOT_LIMIT_BYTES:
        result["truncated"] = False
        return result
    for key in ("recent_events", "recent_commands"):
        items = result.get(key)
        while (
            isinstance(items, list)
            and items
            and len(json.dumps(result).encode("utf-8"))
            > _STATUS_SNAPSHOT_LIMIT_BYTES
        ):
            del items[0]
    result["truncated"] = True
    return result


def _issue_ref_for_attempt(attempt_id: str, title: Any = None) -> dict[str, Any]:
    """The ``{number, title}`` issue ref for an Attempt id."""
    from coding_agent.attempt_id import AttemptId

    attempt = AttemptId.parse(attempt_id)
    resolved_title = title if isinstance(title, str) else None
    return {"number": attempt.input_number, "title": resolved_title}


def _missing_usage_reason(count: int) -> str:
    """The Status Snapshot reason for upper-bound token and cost figures (§11.4)."""
    return (
        f"{count} model response(s) settled at their worst-case bound; "
        "token and cost figures are upper bounds"
    )


def _unreported_cache_write_reason(count: int) -> str:
    """The Status Snapshot reason for a gateway unreported cache write (§11.4)."""
    return (
        f"{count} model response(s) priced an unreported cache write at "
        "max(input, cache-write); token and cost figures are upper bounds"
    )


def _upper_bound_reason(missing_usage: int, unreported_cache_write: int) -> str | None:
    """The combined upper-bound reason, or ``None`` where figures are measured.

    Either worst-case settlement (§11.4, missing or partial usage) or an
    unreported gateway cache write marks the Attempt's token and cost
    figures ``upper-bound``; where both happened the reason names both, in
    that order.
    """
    reasons: list[str] = []
    if missing_usage > 0:
        reasons.append(_missing_usage_reason(missing_usage))
    if unreported_cache_write > 0:
        reasons.append(_unreported_cache_write_reason(unreported_cache_write))
    if not reasons:
        return None
    return " ".join(reasons)


def _figure_tokens(
    value: Any, *, estimated_reason: str | None = None
) -> dict[str, Any]:
    """One ``tokens`` figure: measured where int, estimated where worst-case."""
    if isinstance(value, bool):
        return figure(None, "tokens", "unavailable")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if estimated_reason is not None:
            return figure(value, "tokens", "estimated", estimated_reason)
        return figure(value, "tokens", "measured")
    if estimated_reason is not None:
        if isinstance(value, (int, float)):
            return figure(value, "tokens", "estimated", estimated_reason)
        return figure(None, "tokens", "unavailable")
    return figure(None, "tokens", "unavailable")


def _figure_generic(
    value: Any, unit: str, *, basis: str = "measured", reason: str | None = None
) -> dict[str, Any]:
    """One contract figure, forcing null values to a null basis."""
    if isinstance(value, bool):
        return figure(None, unit, "unavailable")
    if isinstance(value, (int, float)):
        if basis in ("unavailable", "not_applicable"):
            return figure(None, unit, basis)
        if reason is not None:
            return figure(value, unit, basis, reason)
        return figure(value, unit, basis)
    return figure(None, unit, "unavailable")


def _configured_figure(value: Any, unit: str) -> dict[str, Any]:
    """One configured limit figure: configured where numeric, else unavailable."""
    if isinstance(value, bool):
        return figure(None, unit, "unavailable")
    if isinstance(value, (int, float)):
        return figure(value, unit, "configured")
    return figure(None, unit, "unavailable")


def _attempt_detail(
    reader: RunLedgerReader, attempt_id: str
) -> dict[str, Any] | None:
    """One ``attempts`` row: lifecycle state, pinned config, ledger start."""
    record = reader.get_attempt(attempt_id)
    if record is None:
        return None
    effective = record.effective_configuration
    if not isinstance(effective, dict):
        effective = {}
    started_raw = reader.get_attempt_created_at(attempt_id)
    return {
        "attempt_id": record.attempt_id,
        "state": record.state,
        "effective_configuration": effective,
        "started_at": str(started_raw) if started_raw is not None else "",
    }


def step_projection_for(
    reader: RunLedgerReader, attempt_id: str
) -> dict[str, Any] | None:
    """The §19 projection for a step-workflow Attempt, or ``None`` unknown.

    Reads the Attempt row, its outcome, Step Executions, step effects,
    transitions and pinned Effective Configuration through ``reader`` and
    projects them through :mod:`coding_agent.operator.step_projection`.
    ``None`` where the Attempt row itself is missing. A released Claim
    carries no outcome and never requires attention.
    """
    from coding_agent.operator import step_projection as projection

    attempt = reader.get_attempt(attempt_id)
    if attempt is None:
        return None
    effective = attempt.effective_configuration
    if not isinstance(effective, dict):
        effective = {}
    outcome_record = reader.get_outcome(attempt_id)
    recorded: tuple[str, str] | None = None
    if outcome_record is not None:
        recorded = (str(outcome_record.outcome), str(outcome_record.reason))
    return projection.project_attempt(
        state=str(attempt.state),
        recorded=recorded,
        executions=reader.list_step_executions(attempt_id),
        step_effects=reader.list_step_effects(attempt_id),
        transitions=reader.list_step_transitions(attempt_id),
        effective_configuration=effective,
    )


def sealed_for_attempt(
    reader: RunLedgerReader, attempt_id: str, *, state_dir: Path | None = None
) -> tuple[bool, str | None]:
    """Sealed state for an Attempt via its archive manifest, if locatable.

    ``state_dir`` overrides the archive root (the ledger copy an Offline
    Read opens lives in a temporary directory, so the caller passes the
    real state directory); by default the ledger's own parent is the
    state directory.
    """
    from coding_agent.operator.archive import archive_dir_for, sealed_info

    repository = read_instance_repository(reader.connection)
    if repository is None:
        return (False, None)
    try:
        archive = archive_dir_for(
            state_dir if state_dir is not None else reader.path.parent,
            repository,
            attempt_id,
        )
    except ValueError:
        return (False, None)
    try:
        sealed, sealed_at_raw = sealed_info(archive)
    except Exception:
        return (False, None)
    sealed_at = to_z(sealed_at_raw) if sealed_at_raw else None
    if sealed and sealed_at is None:
        return (False, None)
    if not sealed:
        return (False, None)
    assert sealed_at is not None
    return (True, sealed_at)


def outcome_for_attempt(
    reader: RunLedgerReader, attempt_id: str, lifecycle_state: str
) -> dict[str, Any] | None:
    """The ``v1.1`` outcome, or ``None`` for a released Claim."""
    if lifecycle_state == "released":
        return None
    recorded = reader.get_outcome(attempt_id)
    if recorded is None:
        return None
    native = str(recorded.outcome)
    reason_raw = recorded.reason
    reason = str(reason_raw) if isinstance(reason_raw, str) and reason_raw else None
    label = f"{native} ({reason})" if reason else str(native)
    return {"native": str(native), "reason": reason, "label": label}


def _attention_for_attempt(
    outcome_native: str | None, lifecycle_state: str, has_pending: bool
) -> bool:
    """Whether an Attempt needs attention in ``v1.1`` shape."""
    if lifecycle_state == "released":
        return False
    if outcome_native in ("failed", "failed-limit"):
        return True
    if outcome_native in ("delivered", "abandoned"):
        return False
    if lifecycle_state == "outcome-recorded" and has_pending:
        return True
    return False


def attempt_summary(
    reader: RunLedgerReader,
    attempt_id: str,
    lifecycle_state: str,
    observed_at: str,
    *,
    state_dir: Path | None = None,
) -> dict[str, Any] | None:
    """One ``v1.1`` AttemptSummary for ``attempt_id``.

    ``state_dir`` overrides the archive root for the sealed lookup (see
    :func:`sealed_for_attempt`); by default the ledger's own parent is
    the state directory.
    """
    from coding_agent.attempt_id import AttemptId

    detail = _attempt_detail(reader, attempt_id)
    attempt = AttemptId.try_parse(attempt_id)
    is_step = attempt is not None and attempt.workflow is not None
    started_raw = detail["started_at"] if detail is not None else None
    started_at = to_z(str(started_raw)) if started_raw else observed_at
    if started_at is None:
        started_at = observed_at
    issue_title: Any = None
    proj: dict[str, Any] | None = None
    if is_step:
        proj = step_projection_for(reader, attempt_id)
        if proj is not None:
            title_candidate = proj.get("issue_title")
            if isinstance(title_candidate, str):
                issue_title = title_candidate
    issue_ref = _issue_ref_for_attempt(attempt_id, issue_title)
    if is_step:
        outcome_obj: dict[str, Any] | None = None
        delivery_obj: dict[str, Any] | None = None
        attention = False
        if proj is not None:
            raw_outcome = proj.get("outcome")
            if isinstance(raw_outcome, dict):
                native = str(raw_outcome.get("native") or "")
                reason_val = raw_outcome.get("reason")
                reason = (
                    str(reason_val)
                    if isinstance(reason_val, str) and reason_val
                    else None
                )
                if native:
                    outcome_obj = {
                        "native": native,
                        "reason": reason,
                        "label": f"{native} ({reason})" if reason else native,
                    }
            # A step Attempt's delivery comes only from its step projection.
            # Legacy ``effects`` rows are never consulted: no projection
            # delivery means ``{"pull_request": None}``.
            raw_delivery = proj.get("delivery")
            if isinstance(raw_delivery, dict):
                number = raw_delivery.get("number")
                url = raw_delivery.get("url")
                if (
                    isinstance(number, int)
                    and not isinstance(number, bool)
                    and number >= 1
                    and isinstance(url, str)
                    and url
                ):
                    delivery_obj = {"pull_request": {"number": number, "url": url}}
                else:
                    delivery_obj = {"pull_request": None}
            else:
                delivery_obj = {"pull_request": None}
            attention = bool(proj.get("attention_required", False))
        else:
            outcome_obj = outcome_for_attempt(reader, attempt_id, lifecycle_state)
            delivery_obj = {"pull_request": None}
            attention = _attention_for_attempt(
                outcome_obj["native"] if outcome_obj else None,
                lifecycle_state,
                False,
            )
    else:
        # Finished Legacy Attempts lose effect-derived delivery and
        # attention (Decision A): their sealed archives still hold the
        # evidence. Report no pull request and no pending effects, keeping
        # outcome and lifecycle state as recorded.
        outcome_obj = outcome_for_attempt(reader, attempt_id, lifecycle_state)
        delivery_obj = {"pull_request": None}
        attention = _attention_for_attempt(
            outcome_obj["native"] if outcome_obj else None,
            lifecycle_state,
            False,
        )
    sealed, sealed_at = sealed_for_attempt(reader, attempt_id, state_dir=state_dir)
    finished_at: str | None = None
    if lifecycle_state in ("finished", "released", "outcome-recorded"):
        if sealed and sealed_at is not None:
            finished_at = sealed_at
        else:
            finished_at = None
    return {
        "attempt_id": attempt_id,
        "issue": issue_ref,
        "started_at": started_at,
        "finished_at": finished_at,
        "lifecycle_state": lifecycle_state,
        "outcome": outcome_obj,
        "delivery": delivery_obj,
        "attention_required": attention,
        "sealed": sealed,
        "sealed_at": sealed_at,
    }


def last_finished(
    reader: RunLedgerReader, observed_at: str, *, state_dir: Path | None = None
) -> dict[str, Any] | None:
    """The last finished Attempt as a ``v1.1`` AttemptSummary.

    ``state_dir`` overrides the archive root for the sealed lookup (an
    Offline Read opens a ledger copy, so the copy's parent is not the
    state directory); by default the ledger's own parent is used.
    """
    found = reader.last_finished_attempt()
    if found is None:
        return None
    attempt_id, lifecycle_state = found
    return attempt_summary(
        reader, attempt_id, lifecycle_state, observed_at, state_dir=state_dir
    )


def _budget_for_attempt(
    usage: dict[str, Any],
    usage_payload: dict[str, Any],
    detail: dict[str, Any] | None,
    estimated_reason: str | None,
    by_origin_source: Any,
) -> dict[str, Any]:
    """The ``v1.1`` budget for a running Attempt, with per-origin spend.

    ``by_origin_source`` is the flush payload's ``by_origin`` where recorded,
    else the Step-Execution-fallback ``spend_from_executions`` ``by_origin``
    (or ``None``): every origin reads its six 1.1 figures, ``unavailable``
    with the not-recorded reason where no per-origin spend was recorded.
    Totals read as before.
    """
    raw = usage.get("tokens")
    effective = usage.get("effective_tokens")
    raw_fig = _figure_tokens(raw)
    if isinstance(effective, bool):
        effective_fig = figure(None, "tokens", "unavailable")
    elif isinstance(effective, (int, float)):
        if estimated_reason is not None:
            effective_fig = figure(effective, "tokens", "estimated", estimated_reason)
        else:
            effective_fig = figure(effective, "tokens", "measured")
    else:
        effective_fig = figure(None, "tokens", "unavailable")
    pinned = (
        ceiling_from_effective_configuration(detail.get("effective_configuration"))
        if detail is not None
        else {}
    )
    configured_effective = pinned.get("effective_tokens")
    configured_cost = pinned.get("cost_usd")
    configured_wall = pinned.get("wall_clock_seconds")
    configured_tools = pinned.get("tool_calls")
    cost_raw = usage.get("cost_usd")
    if isinstance(cost_raw, bool) or not isinstance(cost_raw, (int, float)):
        cost_used = figure(None, "usd", "unavailable")
    elif estimated_reason is not None:
        cost_used = figure(float(cost_raw), "usd", "estimated", estimated_reason)
    else:
        cost_used = figure(float(cost_raw), "usd", "estimated")
    elapsed_raw = usage_payload.get("elapsed_seconds")
    elapsed_used = (
        _figure_generic(elapsed_raw, "seconds", basis="measured")
        if isinstance(elapsed_raw, (int, float)) and not isinstance(elapsed_raw, bool)
        else figure(None, "seconds", "unavailable")
    )
    tool_raw = usage.get("tool_calls")
    tool_used = (
        _figure_generic(tool_raw, "count", basis="measured")
        if isinstance(tool_raw, int) and not isinstance(tool_raw, bool)
        else figure(None, "count", "unavailable")
    )
    limits: list[dict[str, Any]] = [
        {
            "name": "effective_tokens",
            "used": effective_fig,
            "limit": _configured_figure(configured_effective, "tokens"),
        },
        {
            "name": "cost_usd",
            "used": cost_used,
            "limit": _configured_figure(configured_cost, "usd"),
        },
        {
            "name": "wall_clock_seconds",
            "used": elapsed_used,
            "limit": _configured_figure(configured_wall, "seconds"),
        },
        {
            "name": "tool_calls",
            "used": tool_used,
            "limit": _configured_figure(configured_tools, "count"),
        },
    ]
    return {
        "raw_tokens": raw_fig,
        "effective_tokens": effective_fig,
        "enforced_tokens": "effective",
        "limits": limits,
        "by_origin": _by_origin_for_attempt(by_origin_source),
    }


def _pinned_model_section(detail: dict[str, Any] | None) -> dict[str, Any]:
    """The Attempt's pinned-model section, or ``{}`` where none is pinned."""
    if not isinstance(detail, dict):
        return {}
    effective = detail.get("effective_configuration")
    if not isinstance(effective, dict):
        return {}
    pinned = effective.get("pinned_model")
    return pinned if isinstance(pinned, dict) else {}


def _pinned_context_window(detail: dict[str, Any] | None) -> dict[str, Any]:
    """The pinned ``context_window`` triple, or ``{}`` where none is pinned."""
    window = _pinned_model_section(detail).get("context_window")
    return window if isinstance(window, dict) else {}


def _model_window_from(detail: dict[str, Any] | None) -> Any:
    """The Pinned Model's Model Context Window, or ``None`` where unpinned.

    The pin's ``model_context_window_tokens`` (§16): the largest request
    the model accepts, published as ``context_window``.
    """
    value = _pinned_model_section(detail).get("model_context_window_tokens")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _context_threshold_from(detail: dict[str, Any] | None) -> Any:
    """The pinned Context Threshold, or ``None`` where none is pinned.

    The ``context_window.threshold_tokens`` from the Attempt's Effective
    Configuration (mirrored as ``pinned_model.context_threshold_tokens``).
    Retained for the pin-default fallback; the legacy
    ``native.handoff_threshold_tokens`` figure is no longer projected
    (§16 v1.2.1).
    """
    window = _pinned_context_window(detail)
    threshold: Any = window.get("threshold_tokens")
    if threshold is None:
        threshold = _pinned_model_section(detail).get("context_threshold_tokens")
    if isinstance(threshold, bool):
        return None
    if isinstance(threshold, (int, float)):
        return threshold
    return None


def _margins_from(detail: dict[str, Any] | None) -> tuple[Any, Any]:
    """The safety margin and request overhead in force, with code defaults."""
    from coding_agent.runtime.context_budget import code_owned_margins

    default_safety, default_overhead = code_owned_margins()
    window = _pinned_context_window(detail)
    safety: Any = window.get("safety_margin_tokens")
    if isinstance(safety, bool) or not isinstance(safety, (int, float)):
        safety = default_safety
    overhead: Any = window.get("request_overhead_tokens")
    if isinstance(overhead, bool) or not isinstance(overhead, (int, float)):
        overhead = default_overhead
    return safety, overhead


def _usable_threshold_from(detail: dict[str, Any] | None) -> Any:
    """The pin-default Usable Threshold, or ``None`` where unpinned.

    The pin's Context Threshold less the safety margin and the request
    overhead (§16), published as ``context_threshold`` for steps without
    their own policy. Steps with a policy resolve through
    :func:`_selected_usable_thresholds`.
    """
    from coding_agent.runtime.context_budget import pin_usable_threshold

    threshold = _context_threshold_from(detail)
    if threshold is None:
        return None
    safety, overhead = _margins_from(detail)
    return pin_usable_threshold(threshold, safety, overhead)


def _effective_steps(detail: dict[str, Any] | None) -> dict[str, Any]:
    """The Effective Configuration's ``workflow.steps`` mapping, if any."""
    if not isinstance(detail, dict):
        return {}
    effective = detail.get("effective_configuration")
    if not isinstance(effective, dict):
        return {}
    workflow = effective.get("workflow")
    if not isinstance(workflow, dict):
        return {}
    steps = workflow.get("steps")
    return steps if isinstance(steps, dict) else {}


def _step_policy_for(
    detail: dict[str, Any] | None, step_id: str | None
) -> dict[str, Any] | None:
    """The step's ``context_thresholds`` policy record, if configured."""
    if not step_id:
        return None
    steps = _effective_steps(detail)
    entry = steps.get(step_id)
    if not isinstance(entry, dict):
        return None
    policy = entry.get("context_thresholds")
    return policy if isinstance(policy, dict) else None


def _as_usable_number(value: Any) -> int | float | None:
    """``value`` as a usable token count, or ``None`` where not numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _selected_usable_thresholds(
    detail: dict[str, Any] | None, step_id: str | None
) -> tuple[Any, Any]:
    """The selected and optional hard Usable Thresholds (§16 v1.2.1).

    Returns ``(selected, hard_or_None)`` where ``selected`` is the current
    agent Step Execution's soft Usable Threshold where the step configures
    one, else its hard or the pin-default Usable Threshold. Where a step
    configures both soft and hard thresholds, the hard Usable Threshold is
    also returned for ``native.context_hard_usable_threshold_tokens``.
    Threshold figures use configured tokens.
    """
    from coding_agent.runtime.context_budget import (
        hard_usable_threshold,
        select_usable_threshold,
        soft_usable_threshold,
    )

    safety, overhead = _margins_from(detail)
    policy = _step_policy_for(detail, step_id)
    if policy is not None:
        hard_raw = _as_usable_number(policy.get("hard_tokens"))
        soft_raw = _as_usable_number(policy.get("soft_tokens"))
        hard_usable = (
            hard_usable_threshold(hard_raw, safety, overhead)
            if hard_raw is not None
            else None
        )
        soft_usable = (
            soft_usable_threshold(soft_raw, safety, overhead)
            if soft_raw is not None
            else None
        )
        if hard_raw is not None and soft_raw is not None:
            return (
                select_usable_threshold(soft_usable, hard_usable, None),
                hard_usable,
            )
        if hard_raw is not None:
            return (hard_usable, None)
        if soft_raw is not None:
            # Defensive: validation requires hard alongside soft.
            return (soft_usable, None)
    return (_usable_threshold_from(detail), None)


def _current_step_id(
    live_current: dict[str, Any] | None,
    reader: RunLedgerReader,
    attempt_id: str,
) -> str | None:
    """The current Step Execution's step id, if determinable.

    Prefers the live ``<step>#<seq>`` phase; otherwise the latest Step
    Execution decides. ``None`` where no Step Execution has opened yet.
    """
    phase: Any = None
    if isinstance(live_current, dict):
        phase = live_current.get("phase")
    if isinstance(phase, str) and "#" in phase:
        candidate = phase.split("#", 1)[0]
        if candidate:
            return candidate
    try:
        executions = reader.list_step_executions(attempt_id)
    except Exception:
        return None
    latest: str | None = None
    for raw in executions:
        step_id: Any = None
        if isinstance(raw, Mapping):
            step_id = raw.get("step_id")
        else:
            step_id = getattr(raw, "step_id", None)
        if isinstance(step_id, str) and step_id:
            latest = step_id
    return latest


def _current_step_seq(
    live_current: dict[str, Any] | None,
    reader: RunLedgerReader,
    attempt_id: str,
) -> int | None:
    """The current Step Execution's sequence number, if determinable."""
    phase: Any = None
    if isinstance(live_current, dict):
        phase = live_current.get("phase")
    if isinstance(phase, str) and "#" in phase:
        tail = phase.rsplit("#", 1)[1]
        try:
            seq = int(tail)
        except (TypeError, ValueError):
            seq = None
        if seq is not None and seq >= 1:
            return seq
    try:
        executions = reader.list_step_executions(attempt_id)
    except Exception:
        return None
    latest: int | None = None
    for raw in executions:
        seq_value: Any = None
        if isinstance(raw, Mapping):
            seq_value = raw.get("seq")
        else:
            seq_value = getattr(raw, "seq", None)
        if isinstance(seq_value, bool):
            continue
        if isinstance(seq_value, int) and seq_value >= 1:
            latest = seq_value
    return latest


def _effective_step_entry(
    detail: dict[str, Any] | None, step_id: str | None
) -> dict[str, Any]:
    """The Effective Configuration entry for ``step_id``, if a mapping."""
    if not step_id:
        return {}
    steps = _effective_steps(detail)
    entry = steps.get(step_id)
    return entry if isinstance(entry, dict) else {}


def _is_operation_step(detail: dict[str, Any] | None, step_id: str | None) -> bool:
    """Whether the current step is an operation (non-agent) step."""
    entry = _effective_step_entry(detail, step_id)
    if not entry:
        return False
    if entry.get("operation") is not None:
        return True
    return entry.get("agent") is None


def _omit_context_pair(
    reader: RunLedgerReader,
    attempt_id: str,
    detail: dict[str, Any] | None,
    step_id: str | None,
) -> bool:
    """Whether the current ``context_used``/``context_threshold`` pair omits.

    The pair is omitted during operations and finalisation (§16 v1.2.1):
    while the current step is an operation step, or once the Attempt has
    recorded its Terminal Outcome and finalisation owns the ledger.
    """
    if _is_operation_step(detail, step_id):
        return True
    try:
        if reader.get_outcome(attempt_id) is not None:
            return True
    except Exception:
        return False
    return False


def _context_state_from(usage_payload: dict[str, Any]) -> dict[str, Any]:
    """The main-conversation context the last usage flush carried, if any.

    Ledgers flushed before the context travelled in the payload — older
    ledgers and the Step Execution fallback path — carry no ``context`` key
    and read ``unavailable``, never ``not_applicable`` or zero.
    """
    context = usage_payload.get("context")
    return context if isinstance(context, dict) else {}


def _context_estimate_value(usage_payload: dict[str, Any]) -> int | float | None:
    """The flush's next-request Conversation Context, if the loop estimated one."""
    context = _context_state_from(usage_payload)
    estimate = context.get("estimate")
    if isinstance(estimate, bool) or not isinstance(estimate, (int, float)):
        return None
    return estimate


def _parse_step_conversation(value: Any) -> tuple[str, int | None] | None:
    """The ``(step_id, seq)`` a main conversation label names, if parseable.

    Current labels are ``step/<step-id>/<seq>`` (see
    :func:`coding_agent.runtime.transcript.work_conversation`); ledgers
    predating the sequence carry the legacy ``step/<step-id>`` form, which
    still reports with ``seq`` ``None``. Anything else is not a step
    conversation.
    """
    if not isinstance(value, str):
        return None
    if not value.startswith("step/"):
        return None
    rest = value[len("step/") :]
    if "/" in rest:
        step_id, _, tail = rest.partition("/")
        if not step_id or "/" in tail:
            return None
        try:
            seq = int(tail)
        except (TypeError, ValueError):
            return None
        if seq < 1:
            return None
        return (step_id, seq)
    if not rest:
        return None
    return (rest, None)


def _context_used_figure(
    usage_payload: dict[str, Any], *, expected_conversation: str | None = None
) -> dict[str, Any]:
    """The main conversation's next-request Conversation Context (§16 v1.2.1).

    Exactly the value the same Step Execution compares with its selected
    Usable Threshold: the flush's ``context.estimate`` with its basis. Both
    cases report ``estimated`` — the contract's basis enum is unchanged —
    with the anchored reason where a measured response anchors the value
    and the local-estimate reason before the first measured response.
    Fresh and inherited openings read ``unavailable`` until the receiver
    evaluates its own request: where the flush still carries another
    conversation, the current Step Execution has produced no compared value
    yet. Unknown is never zero.
    """
    context = _context_state_from(usage_payload)
    estimate = _context_estimate_value(usage_payload)
    if estimate is None:
        return figure(
            None,
            "tokens",
            "unavailable",
            "the step loop has not estimated the main conversation yet",
        )
    if expected_conversation is not None:
        parsed = _parse_step_conversation(context.get("conversation"))
        if parsed is not None:
            expected_parsed = _parse_step_conversation(expected_conversation)
            if expected_parsed is not None and parsed[0] != expected_parsed[0]:
                # The flush belongs to another Step Execution — whether it
                # carries a current or a legacy sequence-less label — so the
                # receiver has produced no compared value yet.
                return figure(
                    None, "tokens", "unavailable", _RECEIVER_NOT_EVALUATED_REASON
                )
            if parsed[1] is not None:
                carried = f"step/{parsed[0]}/{parsed[1]}"
                if carried != expected_conversation:
                    return figure(
                        None, "tokens", "unavailable", _RECEIVER_NOT_EVALUATED_REASON
                    )
    if context.get("estimate_basis") == "anchored":
        return figure(estimate, "tokens", "estimated", _ANCHORED_CONTEXT_REASON)
    return figure(estimate, "tokens", "estimated", _LOCAL_ESTIMATE_CONTEXT_REASON)


_MODEL_RESPONSE_KIND = "model_response"
"""Run Ledger event kind: one settled model response (main or Sub-agent)."""

_CONTEXT_THRESHOLD_KIND = "context_threshold"
"""Run Ledger event kind: the main conversation reached the Usable Threshold."""

_HARD_CONTEXT_THRESHOLD_KIND = "hard_context_threshold"
"""Run Ledger event kind: the main conversation reached a step's Hard Context Threshold.

Produced by the agent loop (``agent_step.HARD_CONTEXT_THRESHOLD_KIND``);
the payload carries the same Conversation Context record, so the peak scan
reads it like the pin-threshold event.
"""

_CONTEXT_OVERFLOW_KIND = "context_overflow"
"""Run Ledger event kind: one refused or cut-short Context Overflow."""

_SUBAGENT_ENDED_KIND = "subagent_ended"
"""Run Ledger event kind: one Sub-agent conversation ended."""

_SOFT_CONTEXT_THRESHOLD_KIND = "soft_context_threshold"
"""Run Ledger event kind: a step's Soft Context Threshold crossing (§16)."""

_CONTEXT_SCAN_KINDS = (
    _MODEL_RESPONSE_KIND,
    _CONTEXT_THRESHOLD_KIND,
    _HARD_CONTEXT_THRESHOLD_KIND,
    _SOFT_CONTEXT_THRESHOLD_KIND,
    _CONTEXT_OVERFLOW_KIND,
)
"""Event kinds carrying a main-conversation Conversation Context record."""


def _context_peak_scan(
    reader: RunLedgerReader, attempt_id: str
) -> tuple[list[int | float], bool]:
    """Main-conversation compared values plus whether earlier records lack.

    Every ``model_response``/``context_threshold``/``hard_context_threshold``/
    ``context_overflow`` record's Conversation Context value — Sub-agent
    conversations (a ``call_id`` on the payload) excluded — with ``True``
    where any scanned record carries no usable value: a request from before
    the 1.1 producer. An undecodable payload counts as lacking, never as zero.
    """
    from coding_agent.runtime.conversation_context import recorded_conversation_context

    pairs = reader.list_event_payloads(attempt_id, _CONTEXT_SCAN_KINDS)
    values: list[int | float] = []
    partial = False
    for kind, payload in pairs:
        if not isinstance(payload, dict):
            partial = True
            continue
        if kind in (_MODEL_RESPONSE_KIND, _CONTEXT_OVERFLOW_KIND):
            if payload.get("call_id") is not None:
                continue
        recorded = recorded_conversation_context(payload)
        if recorded.value is None:
            partial = True
        else:
            values.append(recorded.value)
    return (values, partial)


def _context_peak_figure(
    reader: RunLedgerReader,
    attempt_id: str,
    usage_payload: dict[str, Any],
    *,
    partial: bool,
) -> dict[str, Any]:
    """The largest ``context_used`` over the Attempt, kept across conversations.

    Computed only from Conversation Context records — the scanned compared
    values plus the flush's current next-request estimate — never mixed
    with the old ``input+output`` peak. ``estimated`` where any request was
    compared; ``unavailable`` before the first compared request. Where
    earlier requests lack records the reason says the peak covers only the
    requests recorded since the upgrade.
    """
    values, events_partial = _context_peak_scan(reader, attempt_id)
    estimate = _context_estimate_value(usage_payload)
    if estimate is not None:
        values.append(estimate)
    if not values:
        if partial or events_partial:
            return figure(None, "tokens", "unavailable", _SINCE_UPGRADE_REASON)
        return figure(
            None,
            "tokens",
            "unavailable",
            "no main-conversation request has been compared yet",
        )
    if partial or events_partial:
        return figure(max(values), "tokens", "estimated", _SINCE_UPGRADE_REASON)
    return figure(max(values), "tokens", "estimated")


def _subagent_figures(
    reader: RunLedgerReader, attempt_id: str, *, partial: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The Attempt's Sub-agent counts: running now and ended (§16).

    ``running`` is started minus ended, floored at zero; ``ended`` counts
    recorded endings only. Detail stays in the Run Ledger and the export.
    Where earlier endings lack records — a ``subagent_ended`` event from
    before the usage cell travelled in the payload, or an undecodable one —
    the ended figure carries the since-upgrade reason; it still counts every
    recorded ending, never zero-filling the unrecorded ones.
    """
    started = reader.count_events(attempt_id, "subagent_started")
    ended_pairs = reader.list_event_payloads(attempt_id, (_SUBAGENT_ENDED_KIND,))
    local_partial = partial
    for _, payload in ended_pairs:
        if not isinstance(payload, dict):
            local_partial = True
            continue
        usage_cell = payload.get("usage")
        if not isinstance(usage_cell, Mapping):
            local_partial = True
    ended = len(ended_pairs)
    running = max(0, started - ended)
    running_fig = figure(running, "count", "measured")
    if local_partial:
        ended_fig = figure(ended, "count", "measured", _SINCE_UPGRADE_REASON)
    else:
        ended_fig = figure(ended, "count", "measured")
    return (running_fig, ended_fig)


_MAIN_CACHE_NO_USAGE_REASON = (
    "the main conversation has not settled a response carrying usage yet"
)
"""The ``cache_hit_rate`` reason before the first measured main response."""


def _cache_hit_rate_figure_from_cell(cell: Any) -> dict[str, Any]:
    """The main-conversation cache-hit rate, derived from the ``main`` cell.

    ``cache_read_tokens`` over ``input_tokens`` of the ``main`` Usage Origin
    only (contract §9.2, §16); Sub-agent spend never contributes.
    Unknown is never zero: a missing cell, a zero input total, or any main
    response without ``usage_metadata`` each read ``unavailable`` with a
    reason — a ratio over partial data is never ``measured``.
    """
    if not isinstance(cell, Mapping):
        return figure(None, "ratio", "unavailable", _NOT_RECORDED_REASON)
    missing = _as_cell_count(cell.get("responses_without_usage"))
    if missing is not None and missing > 0:
        return figure(
            None,
            "ratio",
            "unavailable",
            f"usage metadata missing for {missing} response(s)",
        )
    read = _as_cell_count(cell.get("cache_read_tokens"))
    total = _as_cell_count(cell.get("input_tokens"))
    if read is None or total is None:
        return figure(None, "ratio", "unavailable", _NOT_RECORDED_REASON)
    if total <= 0:
        return figure(None, "ratio", "unavailable", _MAIN_CACHE_NO_USAGE_REASON)
    ratio = read / total
    if 0 <= ratio <= 1:
        return figure(ratio, "ratio", "measured")
    return figure(
        None, "ratio", "unavailable", "the cached-token ratio is outside 0..1"
    )


def _turns_figure_from_cell(cell: Any) -> dict[str, Any]:
    """The Attempt's model turns, derived from the ``main`` cell (§16).

    ``responses`` less ``requests_without_response`` of the ``main`` Usage
    Origin only (contract §9.2); Sub-agent responses never count. Unknown is
    never zero: a missing cell — a pre-1.1 Attempt or a fallback without
    ``by_origin`` — reads ``unavailable`` with the not-recorded reason. A
    cell recorded before the split count reads ``measured`` where it carries
    no worst-case charge (no request failed), else ``estimated`` with the
    turns reason.
    """
    if not isinstance(cell, Mapping):
        return figure(None, "count", "unavailable", _NOT_RECORDED_REASON)
    responses = _as_cell_count(cell.get("responses"))
    if responses is None:
        return figure(None, "count", "unavailable", _NOT_RECORDED_REASON)
    without_response = _as_cell_count(cell.get("requests_without_response"))
    if without_response is not None:
        return figure(
            max(0, responses - without_response), "count", "measured"
        )
    without_usage = _as_cell_count(cell.get("responses_without_usage"))
    if without_usage is not None and without_usage > 0:
        return figure(responses, "count", "estimated", _TURNS_ESTIMATED_REASON)
    return figure(responses, "count", "measured")


def _round_handoff_section(detail: dict[str, Any] | None) -> dict[str, Any]:
    """The Effective Configuration's ``workflow.round_handoff`` section, if any."""
    if not isinstance(detail, dict):
        return {}
    effective = detail.get("effective_configuration")
    if not isinstance(effective, dict):
        return {}
    workflow = effective.get("workflow")
    if not isinstance(workflow, dict):
        return {}
    section = workflow.get("round_handoff")
    return section if isinstance(section, dict) else {}


def _pickup_figures(
    reader: RunLedgerReader, attempt_id: str, detail: dict[str, Any] | None
) -> dict[str, Any]:
    """The Attempt's Round Handoff pickup count figures, when known (§16 v1.2.1).

    ``native.pickup_limit`` reads ``configured`` where the Attempt's pinned
    Effective Configuration opts into Round Handoff; ``native.pickups_used``
    reads ``measured`` where the recorded Terminal Outcome carries a
    machine-readable handoff block, and is absent otherwise — a running
    Attempt that has recorded no outcome yet has consumed no recorded
    pickup. Both are Attempt-level counts, never ``unavailable`` placeholders.
    """
    figures: dict[str, Any] = {}
    section = _round_handoff_section(detail)
    label = section.get("pickup_label")
    limit = section.get("max_automatic_pickups")
    if (
        isinstance(label, str)
        and label
        and isinstance(limit, int)
        and not isinstance(limit, bool)
        and limit >= 1
    ):
        figures["native.pickup_limit"] = figure(
            limit, "count", "configured", _PICKUP_LIMIT_REASON
        )
    try:
        outcome = reader.get_outcome(attempt_id)
    except Exception:
        outcome = None
    body = getattr(outcome, "terminal_comment_body", "") if outcome is not None else ""
    if isinstance(body, str) and body:
        from coding_agent.runtime.round_handoff import parse_handoff_block

        try:
            offer = parse_handoff_block(body)
        except Exception:
            offer = None
        if offer is not None:
            figures["native.pickups_used"] = figure(
                offer.used, "count", "measured", _PICKUP_USED_REASON
            )
    return figures


def _figures_for_attempt(
    live_current: dict[str, Any] | None,
    reader: RunLedgerReader,
    attempt_id: str,
    usage: dict[str, Any],
    usage_payload: dict[str, Any],
    estimated_reason: str | None,
    detail: dict[str, Any] | None,
    main_cell: Any,
    *,
    partial: bool,
) -> dict[str, Any]:
    """The figures map for a running Attempt (§16 v1.2.1).

    ``main_cell`` is the flush (or fallback) ``by_origin`` ``main`` cell
    the ``cache_hit_rate`` derives from; ``partial`` marks an Attempt whose
    ledger holds pre-1.1 records, so ``context_peak`` and
    ``subagents_ended`` carry the since-upgrade reason.

    ``context_threshold`` is the active Step Execution's selected Usable
    Threshold — the step's soft threshold where configured, else its hard
    or the pin-default — with the hard Usable Threshold alongside as
    ``native.context_hard_usable_threshold_tokens`` where the step
    configures both. ``context_used`` is the exact next main request the
    same Step Execution compares against it, unavailable until an
    inheriting receiver evaluates its own request. The pair is absent
    during operations and finalisation; the Attempt-wide ``context_peak``
    (including refused requests) and the pickup counts stay.
    """
    tool_raw = usage.get("tool_calls")
    tool_fig = (
        _figure_generic(tool_raw, "count", basis="measured")
        if isinstance(tool_raw, int) and not isinstance(tool_raw, bool)
        else figure(None, "count", "unavailable")
    )
    cache_fig = _cache_hit_rate_figure_from_cell(main_cell)
    elapsed_raw = usage_payload.get("elapsed_seconds")
    elapsed_fig = (
        _figure_generic(elapsed_raw, "seconds", basis="measured")
        if isinstance(elapsed_raw, (int, float)) and not isinstance(elapsed_raw, bool)
        else figure(None, "seconds", "unavailable")
    )
    context_peak_fig = _context_peak_figure(
        reader, attempt_id, usage_payload, partial=partial
    )
    model_window = _model_window_from(detail)
    if isinstance(model_window, (int, float)) and not isinstance(model_window, bool):
        context_window_fig = figure(
            model_window, "tokens", "configured", _CONTEXT_WINDOW_REASON
        )
    else:
        context_window_fig = figure(
            None, "tokens", "unavailable", "no pinned context window"
        )
    running_fig, ended_fig = _subagent_figures(reader, attempt_id, partial=partial)
    step_id = _current_step_id(live_current, reader, attempt_id)
    pair_figures: dict[str, Any] = {}
    if not _omit_context_pair(reader, attempt_id, detail, step_id):
        step_seq = _current_step_seq(live_current, reader, attempt_id)
        expected: str | None = None
        if step_id is not None and step_seq is not None:
            from coding_agent.runtime.transcript import work_conversation

            expected = work_conversation(step_id, step_seq)
        pair_figures["context_used"] = _context_used_figure(
            usage_payload, expected_conversation=expected
        )
        selected, hard = _selected_usable_thresholds(detail, step_id)
        if isinstance(selected, (int, float)) and not isinstance(selected, bool):
            pair_figures["context_threshold"] = figure(
                selected, "tokens", "configured", _CONTEXT_THRESHOLD_REASON
            )
            if isinstance(hard, (int, float)) and not isinstance(hard, bool):
                pair_figures["native.context_hard_usable_threshold_tokens"] = figure(
                    hard, "tokens", "configured", _HARD_USABLE_THRESHOLD_REASON
                )
        else:
            pair_figures["context_threshold"] = figure(
                None, "tokens", "unavailable", "no pinned context window"
            )
    figures: dict[str, Any] = {
        "cost_usd": _figure_generic(
            usage.get("cost_usd"),
            "usd",
            basis="estimated" if estimated_reason is not None else "estimated",
            reason=estimated_reason,
        ),
        "tool_calls": tool_fig,
        "turns": _turns_figure_from_cell(main_cell),
        "cache_hit_rate": cache_fig,
    }
    if "context_used" in pair_figures:
        figures["context_used"] = pair_figures["context_used"]
    figures["context_peak"] = context_peak_fig
    figures["context_window"] = context_window_fig
    if "context_threshold" in pair_figures:
        figures["context_threshold"] = pair_figures["context_threshold"]
    if "native.context_hard_usable_threshold_tokens" in pair_figures:
        figures["native.context_hard_usable_threshold_tokens"] = pair_figures[
            "native.context_hard_usable_threshold_tokens"
        ]
    figures["subagents_running"] = running_fig
    figures["subagents_ended"] = ended_fig
    figures["elapsed_seconds"] = elapsed_fig
    figures.update(_pickup_figures(reader, attempt_id, detail))
    if live_current is not None:
        seats = live_current.get("review_seats_running")
        figures["review_seats_running"] = (
            _figure_generic(seats, "count", basis="measured")
            if isinstance(seats, int) and not isinstance(seats, bool)
            else figure(None, "count", "unavailable")
        )
    return figures


_NOT_RECORDED_REASON = "not recorded"
"""The ``by_origin`` reason where no per-origin spend was recorded.

Ledgers flushed before per-origin tracking — an archived Attempt usage
flush without ``by_origin``, or Step Executions without it — never assign
old spend to an origin: every per-origin figure reads ``unavailable`` with
this reason, never zero.
"""


def _as_cell_count(value: Any) -> int | None:
    """One readable cell count, or ``None`` where the cell lacks it."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _as_cell_dollars(value: Any) -> float | None:
    """One readable cell dollar amount, or ``None`` where the cell lacks it."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _origin_figures(cell: Any) -> dict[str, Any]:
    """One Usage Origin's spend as the six 1.1 contract figures (§11.1).

    Token figures and the effective figure read ``measured`` where the cell
    is recorded — ``estimated`` with the worst-case reason where the cell's
    own ``responses_without_usage`` is positive — and ``unavailable`` with
    the not-recorded reason where the cell itself was never recorded. Cost
    is always ``estimated`` (prices are estimates), with the worst-case
    reason where the cell carries a worst-case charge. A missing key is
    never read as zero.
    """
    if not isinstance(cell, Mapping):
        return {
            "input_tokens": figure(
                None, "tokens", "unavailable", _NOT_RECORDED_REASON
            ),
            "cache_read_tokens": figure(
                None, "tokens", "unavailable", _NOT_RECORDED_REASON
            ),
            "cache_write_tokens": figure(
                None, "tokens", "unavailable", _NOT_RECORDED_REASON
            ),
            "output_tokens": figure(
                None, "tokens", "unavailable", _NOT_RECORDED_REASON
            ),
            "effective_tokens": figure(
                None, "tokens", "unavailable", _NOT_RECORDED_REASON
            ),
            "cost_usd": figure(None, "usd", "unavailable", _NOT_RECORDED_REASON),
        }
    missing = _as_cell_count(cell.get("responses_without_usage"))
    estimated_reason = (
        _missing_usage_reason(missing) if missing is not None and missing > 0 else None
    )

    def _token_figure(key: str) -> dict[str, Any]:
        value = _as_cell_count(cell.get(key))
        if value is None:
            return figure(None, "tokens", "unavailable", _NOT_RECORDED_REASON)
        if estimated_reason is not None:
            return figure(value, "tokens", "estimated", estimated_reason)
        return figure(value, "tokens", "measured")

    cost_parts = [
        _as_cell_dollars(cell.get("cost_input_usd")),
        _as_cell_dollars(cell.get("cost_output_usd")),
        _as_cell_dollars(cell.get("cost_cache_read_usd")),
        _as_cell_dollars(cell.get("cost_cache_write_usd")),
    ]
    if all(part is not None for part in cost_parts):
        cost_total = sum(part for part in cost_parts if part is not None)
        if estimated_reason is not None:
            cost_fig = figure(cost_total, "usd", "estimated", estimated_reason)
        else:
            cost_fig = figure(cost_total, "usd", "estimated")
    else:
        cost_fig = figure(None, "usd", "unavailable", _NOT_RECORDED_REASON)
    return {
        "input_tokens": _token_figure("input_tokens"),
        "cache_read_tokens": _token_figure("cache_read_tokens"),
        "cache_write_tokens": _token_figure("cache_write_tokens"),
        "output_tokens": _token_figure("output_tokens"),
        "effective_tokens": _token_figure("effective_tokens"),
        "cost_usd": cost_fig,
    }


def _by_origin_for_attempt(source: Any) -> dict[str, Any]:
    """The running Attempt's per-origin figures, in flush order (§16 v1.2.1).

    ``main`` and ``subagent`` cells only: the AI-assisted Handoff
    receiver's main requests and tools project under ``main`` once,
    attributed to its Step Execution, and Sub-agent spend stays under
    ``subagent``. ``source`` is the flush payload's ``by_origin`` where
    recorded, else the Step-Execution-fallback
    ``spend_from_executions`` ``by_origin``; ``None`` (or a cell missing
    for an origin) reads ``unavailable`` per origin with the not-recorded
    reason, never zero. The legacy ``handoff`` cell is not reported.
    """
    from coding_agent.runtime.ceilings import ORIGINS

    cells: Mapping[str, Any] | None = source if isinstance(source, Mapping) else None
    return {
        origin: _origin_figures(cells.get(origin) if cells is not None else None)
        for origin in ORIGINS
    }


def build_current_attempt(
    reader: RunLedgerReader, live: LiveView, observed_at: str
) -> dict[str, Any] | None:
    """The running Attempt in ``v1.1`` shape, or ``None`` where idle.

    Per-origin spend lives under ``budget.by_origin`` (from the flush's
    ``by_origin`` where recorded, else the Step-Execution fallback); there
    is no top-level ``by_origin``. Where the ledger predates the 1.1 fields
    yet already carries spend, ``context_peak`` and ``subagents_ended``
    carry the since-upgrade reason instead of zero-filling the gap.
    """
    current = live.get_current()
    if current is None:
        return None
    attempt_id = str(current.get("attempt_id") or "")
    if not attempt_id:
        return None
    detail = _attempt_detail(reader, attempt_id)
    started_raw = detail["started_at"] if detail is not None else current.get("started_at")
    started_at = to_z(str(started_raw)) if started_raw else observed_at
    if started_at is None:
        started_at = observed_at
    last_activity_raw = current.get("last_activity_at")
    last_activity_at = to_z(str(last_activity_raw)) if last_activity_raw else None
    usage_payload = reader.get_latest_usage(attempt_id) or {}
    raw_usage = usage_payload.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    fallback_by_origin: Any = None
    if not usage:
        # Ledgers without an Attempt usage flush keep their metered spend in
        # the Step Executions. Fall back to the summed execution spend so the
        # figures mirror the legacy projection instead of reading
        # ``unavailable`` (§11.4 never reads unavailable where a worst-case
        # charge exists). The flush is the primary path while a step runs;
        # this stays for ledgers that hold no flush.
        from coding_agent.operator import step_projection as projection

        executions = reader.list_step_executions(attempt_id)
        metered = [
            execution.usage
            for execution in executions
            if isinstance(execution.usage, dict) and execution.usage
        ]
        if metered:
            spend = projection.spend_from_executions(executions)
            usage = {
                "effective_tokens": spend["effective_tokens"],
                "cost_usd": spend["cost_usd"],
                "tool_calls": spend["tool_calls"],
            }
            for key in (
                "tokens",
                "input_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            ):
                if key in spend:
                    usage[key] = spend[key]
            fallback_by_origin = spend.get("by_origin")
            wall_clock = spend["wall_clock_seconds"]
            usage_payload = (
                {"elapsed_seconds": wall_clock}
                if isinstance(wall_clock, (int, float))
                and not isinstance(wall_clock, bool)
                else {}
            )
    missing_usage = reader.get_responses_without_usage(attempt_id)
    unreported = reader.get_responses_with_unreported_cache_write(attempt_id)
    estimated_reason = _upper_bound_reason(missing_usage, unreported)
    issue_title = current.get("issue_title")
    issue_ref = _issue_ref_for_attempt(attempt_id, issue_title)
    phase_raw = current.get("phase")
    native_phase = str(phase_raw) if isinstance(phase_raw, str) and phase_raw else "claim"
    raw_by_origin: Any = usage_payload.get("by_origin")
    by_origin_source = (
        raw_by_origin if isinstance(raw_by_origin, Mapping) else fallback_by_origin
    )
    has_spend = bool(usage)
    partial = (not isinstance(by_origin_source, Mapping)) and has_spend
    budget = _budget_for_attempt(
        usage, usage_payload, detail, estimated_reason, by_origin_source
    )
    main_cell = (
        by_origin_source.get("main")
        if isinstance(by_origin_source, Mapping)
        else None
    )
    figures = _figures_for_attempt(
        current,
        reader,
        attempt_id,
        usage,
        usage_payload,
        estimated_reason,
        detail,
        main_cell,
        partial=partial,
    )
    return {
        "attempt_id": attempt_id,
        "issue": issue_ref,
        "native_phase": native_phase,
        "started_at": started_at,
        "last_activity_at": last_activity_at,
        "observed_at": observed_at,
        "budget": budget,
        "figures": figures,
    }


def build_identity(
    reader: RunLedgerReader, live: LiveView, *, input_type: str = "issue"
) -> dict[str, Any]:
    """The ``identity`` result: backend, version, contract, repo, mode, clocks."""
    stored = read_instance_row(reader.connection)
    repository = stored["target_repository"] if stored is not None else ""
    backend = stored["backend"] if stored is not None else OPERATOR_BACKEND
    version = stored["agent_version"] if stored is not None else agent_version()
    process_started_at = to_z(live.process_started_at_iso())
    from coding_agent.operator.step_projection import capabilities_for

    result: dict[str, Any] = {
        "backend": backend,
        "agent_version": version,
        "contract": dict(IDENTITY_CONTRACT),
        "repository": repository,
        "mode": "online",
        "process_started_at": process_started_at,
        "capabilities": sorted(capabilities_for(input_type)),
    }
    return result


def _read_intake_detail(reader: RunLedgerReader) -> dict[str, Any]:
    """The durable Intake state plus pending stop, stop plan and priority.

    ``priority`` is ``{request_id, issue}`` while an accepted ``next-issue``
    command waits for the next Claim boundary, else ``None`` (contract
    ``v1.1``).
    """
    from coding_agent.operator.commands import (
        pending_next_priority,
        read_intake,
    )

    try:
        intake = read_intake(reader.connection)
        try:
            intake["priority"] = pending_next_priority(reader.connection)
        except sqlite3.Error:
            intake["priority"] = None
        return intake
    except sqlite3.Error:
        return {
            "state": "open",
            "pending_stop": None,
            "stop_plan": None,
            "priority": None,
        }


def _read_recent_commands(
    reader: RunLedgerReader, limit: int = 20
) -> list[dict[str, Any]]:
    """The newest ``limit`` commands, newest last."""
    from coding_agent.operator.commands import list_recent_commands

    try:
        return [
            record.to_dict()
            for record in list_recent_commands(reader.connection, limit)
        ]
    except sqlite3.Error:
        return []


def _read_capability_reports(
    reader: RunLedgerReader,
) -> dict[str, dict[str, Any]] | None:
    """Every referenced pin's latest Provider Capability Report, by pin id.

    The single home for the ``status.provider_capability`` read: the live
    listener (:func:`build_status`) and the Offline Read copy
    (:func:`build_offline_status`) share it, so live and offline output
    cannot drift apart. ``None`` where no assertion has run yet — which
    matters most after a refused startup, when no Worker is running.
    """
    try:
        reports = read_provider_capability_reports(reader.connection)
    except sqlite3.Error:
        return None
    if reports is None:
        return None
    return dict(reports)


def _read_recent_events(
    reader: RunLedgerReader, process_started_at: str, limit: int = 50
) -> list[dict[str, Any]]:
    """The newest ``limit`` Contract Events of this process, oldest first."""
    try:
        from coding_agent.operator import events as contract_events

        return contract_events.list_recent_contract_events(
            reader.connection,
            process_started_at=process_started_at,
            limit=limit,
        )
    except sqlite3.Error:
        return []


def build_status(reader: RunLedgerReader, live: LiveView) -> dict[str, Any]:
    """The ``status``: instance state, Intake state, Halt, checks, events.

    Instance states: ``starting`` (before readiness, while startup checks or
    recovery run), ``recovering`` (unfinished Attempts from an earlier
    process are being settled), then the §7.2 states ``idle`` (Intake open,
    nothing active), ``active`` (Intake open, an Attempt active),
    ``draining`` (Intake closed, an Attempt still active) and ``paused``
    (Intake closed, nothing active). Readiness — a state other than
    ``starting`` — is reached only after recovery. While a startup check is
    held, ``startup_check`` names it. ``instance.last_halt`` carries the
    durable Halt, if any. ``recent_events`` holds at most the newest 50
    Contract Events of this process, oldest first, every one redacted and
    capped at 300 characters at write time. ``current_attempt`` joins the
    prepared live view (native id, phase, activity) with the ledger's start
    time, pinned ceilings and latest usage flush; ``last_finished`` projects
    the latest finished Attempt with its outcome, pull request and attention
    flag. The snapshot never exceeds 256 KiB: the unbounded tails trim in
    contract order and the snapshot reports ``truncated``.
    ``recent_commands`` carries at most 20 commands, newest last;
    ``intake.pending_stop`` is the pending ``stop``'s ``CommandRef`` or
    ``None``; ``intake.stop_plan`` carries the ``stop --after`` countdown
    (``request_id``, ``remaining`` and ``counted``) or ``None``;
    ``intake.priority`` carries the accepted ``next-issue`` priority
    (``request_id`` and ``issue``) while one waits for the next Claim
    boundary, else ``None``. The snapshot carries the same ``identity``
    object ``identity`` returns. ``provider_capability`` carries every
    referenced pin's latest Provider Capability Report keyed by pin id,
    or ``None`` where no assertion has run yet.
    """
    now = utc_now_z()
    active = reader.has_unfinished_attempt()
    intake = _read_intake_detail(reader)
    intake_state = str(intake.get("state") or "open")
    if intake_state not in ("open", "closed"):
        intake_state = "open"
    pending = intake.get("pending_stop")
    pending_ref: dict[str, Any] | None = None
    if isinstance(pending, dict) and pending.get("request_id"):
        pending_ref = {
            "request_id": str(pending["request_id"]),
            "sequence": int(pending.get("sequence") or 0),
        }
    startup_check = live.get_startup_check()
    raw_plan = intake.get("stop_plan")
    plan_ref: dict[str, Any] | None = None
    if isinstance(raw_plan, dict) and raw_plan.get("request_id"):
        plan_ref = {
            "request_id": str(raw_plan["request_id"]),
            "remaining": int(raw_plan.get("remaining") or 0),
            "counted": int(raw_plan.get("counted") or 0),
        }
    raw_priority = intake.get("priority")
    priority_ref: dict[str, Any] | None = None
    if (
        isinstance(raw_priority, dict)
        and raw_priority.get("request_id")
        and isinstance(raw_priority.get("issue"), int)
        and not isinstance(raw_priority.get("issue"), bool)
        and int(raw_priority["issue"]) >= 1
    ):
        priority_ref = {
            "request_id": str(raw_priority["request_id"]),
            "issue": int(raw_priority["issue"]),
        }
    if not live.is_ready():
        if live.is_recovering():
            instance_state = "recovering"
        else:
            instance_state = "starting"
    elif intake_state == "open":
        instance_state = "active" if active else "idle"
    else:
        instance_state = "draining" if active else "paused"
    last_halt = read_last_halt(reader.connection)
    recent_events = _read_recent_events(reader, live.process_started_at_iso())
    try:
        target: str | None = None
        current_live = live.get_current()
        if isinstance(current_live, dict) and current_live.get("attempt_id"):
            target = str(current_live["attempt_id"])
        if target is None:
            found = reader.last_finished_attempt()
            if found is not None:
                target = str(found[0])
        if target is not None:
            derived = native_events_for_attempt(
                reader,
                target,
                process_started_at=live.process_started_at_iso(),
                observed_at=now,
            )
            if derived:
                recent_events = ([*recent_events, *derived])[-50:]
    except Exception:
        pass
    result: dict[str, Any] = {
        "generated_at": now,
        "identity": build_identity(reader, live),
        "instance": {
            "state": instance_state,
            "startup_check": startup_check,
            "last_halt": last_halt,
            "observed_at": now,
        },
        "intake": {
            "state": intake_state,
            "pending_stop": pending_ref,
            "stop_plan": plan_ref,
            "priority": priority_ref,
            "observed_at": now,
        },
        "current_attempt": build_current_attempt(reader, live, now),
        "last_finished": last_finished(reader, now),
        "recent_commands": _read_recent_commands(reader),
        "recent_events": recent_events,
        "provider_capability": _read_capability_reports(reader),
    }
    return trim_status_snapshot(result)


def summaries_for_page(
    reader: RunLedgerReader,
    page_rows: list[Any],
    observed_at: str,
    *,
    state_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """``v1.1`` AttemptSummaries for one page's ledger rows, newest first.

    Each row contributes its Attempt id and lifecycle state; the summary
    itself (issue ref, outcome, delivery, sealed state) comes from the
    shared producer. Rows with malformed Attempt ids are skipped rather
    than failing the read. ``state_dir`` overrides the archive root for
    the sealed lookup (an Offline Read opens a ledger copy, so the copy's
    parent is not the state directory).
    """
    from coding_agent.attempt_id import AttemptId

    summaries: list[dict[str, Any]] = []
    for row in page_rows:
        attempt_id = str(row[1])
        lifecycle_state = str(row[2])
        if AttemptId.try_parse(attempt_id) is None:
            continue
        summary = attempt_summary(
            reader,
            attempt_id,
            lifecycle_state,
            observed_at,
            state_dir=state_dir,
        )
        if summary is None:
            continue
        summaries.append(summary)
    return summaries


def attempts_page(
    reader: RunLedgerReader,
    *,
    limit: int,
    before: str | None,
    state_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of AttemptSummaries, newest first.

    An Attempt is listed as soon as its Claim opens: the rows come straight
    from the ledger's ``attempts`` table in reverse claim order. ``before``
    is the opaque cursor of the previous page's last row; the page holds
    attempts older than it. Returns ``(attempts, next_cursor)`` where
    ``next_cursor`` is the cursor for the following page, or ``None`` where
    this page is the last (contract ``v1.1`` ``attempts-list``). Raises
    :class:`InvalidCursor` for a cursor that does not decode and
    :class:`ValueError` for a ``limit`` outside ``1..100``.
    """
    check_limit(limit)
    bound: int | None = None
    if before is not None:
        bound = decode_before_cursor(before)
    rows = reader.list_attempts_page_rows(limit=limit, bound=bound)
    page_rows = list(rows[:limit])
    observed_at = utc_now_z()
    summaries = summaries_for_page(
        reader, page_rows, observed_at, state_dir=state_dir
    )
    next_cursor: str | None = None
    if len(rows) > limit and page_rows:
        next_cursor = encode_before_cursor(int(page_rows[-1][0]))
    return (summaries, next_cursor)


def offline_identity_from_stored(
    stored: dict[str, str] | None,
) -> dict[str, Any]:
    """The offline ``identity`` result for an instance row (or none).

    Contract ``v1.1`` shape: the same document ``identity`` returns,
    with ``mode`` ``offline`` and ``process_started_at`` ``None`` (the
    Workflow Configuration is not mounted). The envelope's ``contract``
    stays the ``"major.minor"`` string; only this nested object uses the
    ``{"major", "minor"}`` shape.
    """
    from coding_agent.agentctl import ADVERTISED_CAPABILITIES

    repository = stored["target_repository"] if stored is not None else ""
    backend = stored["backend"] if stored is not None else OPERATOR_BACKEND
    version = stored["agent_version"] if stored is not None else agent_version()
    return {
        "backend": backend,
        "agent_version": version,
        "contract": dict(IDENTITY_CONTRACT),
        "repository": repository,
        "mode": "offline",
        "process_started_at": None,
        "capabilities": sorted(ADVERTISED_CAPABILITIES),
    }


def offline_intake_projection(
    intake: dict[str, Any], now: str
) -> dict[str, Any]:
    """The ``v1.1`` intake projection for an offline status snapshot."""
    intake_state = str(intake.get("state") or "open")
    if intake_state not in ("open", "closed"):
        intake_state = "open"
    pending = intake.get("pending_stop")
    pending_ref: dict[str, Any] | None = None
    if isinstance(pending, dict) and pending.get("request_id"):
        try:
            pending_ref = {
                "request_id": str(pending["request_id"]),
                "sequence": int(pending.get("sequence") or 0),
            }
        except (TypeError, ValueError):
            pending_ref = None
    raw_plan = intake.get("stop_plan")
    plan_ref: dict[str, Any] | None = None
    if isinstance(raw_plan, dict) and raw_plan.get("request_id"):
        try:
            plan_ref = {
                "request_id": str(raw_plan["request_id"]),
                "remaining": int(raw_plan.get("remaining") or 0),
                "counted": int(raw_plan.get("counted") or 0),
            }
        except (TypeError, ValueError):
            plan_ref = None
    raw_priority = intake.get("priority")
    priority_ref: dict[str, Any] | None = None
    if (
        isinstance(raw_priority, dict)
        and raw_priority.get("request_id")
        and isinstance(raw_priority.get("issue"), int)
        and not isinstance(raw_priority.get("issue"), bool)
        and int(raw_priority["issue"]) >= 1
    ):
        priority_ref = {
            "request_id": str(raw_priority["request_id"]),
            "issue": int(raw_priority["issue"]),
        }
    return {
        "state": intake_state,
        "pending_stop": pending_ref,
        "stop_plan": plan_ref,
        "priority": priority_ref,
        "observed_at": now,
    }


def fresh_offline_status(now: str) -> dict[str, Any]:
    """The offline ``status`` for a volume with no recorded instance rows."""
    result: dict[str, Any] = {
        "generated_at": now,
        "identity": offline_identity_from_stored(None),
        "instance": {
            "state": "stopped",
            "startup_check": None,
            "last_halt": None,
            "observed_at": now,
        },
        "intake": {
            "state": "open",
            "pending_stop": None,
            "stop_plan": None,
            "priority": None,
            "observed_at": now,
        },
        "current_attempt": None,
        "last_finished": None,
        "recent_commands": [],
        "recent_events": [],
        "provider_capability": None,
    }
    return trim_status_snapshot(result)


def _read_offline_intake(reader: RunLedgerReader) -> dict[str, Any]:
    """The durable Intake state from the ledger copy, open where absent.

    Includes the ``stop_plan`` (``None`` where none is recorded) and the
    ``priority`` (``{request_id, issue}`` while an accepted ``next-issue``
    command waits for the next Claim boundary, else ``None``).
    """
    from coding_agent.operator.commands import pending_next_priority, read_intake

    try:
        intake = read_intake(reader.connection)
        try:
            intake["priority"] = pending_next_priority(reader.connection)
        except sqlite3.Error:
            intake["priority"] = None
        if "stop_plan" not in intake:
            intake["stop_plan"] = None
        return intake
    except sqlite3.Error:
        return {
            "state": "open",
            "pending_stop": None,
            "stop_plan": None,
            "priority": None,
        }


def _read_offline_commands(
    reader: RunLedgerReader, limit: int = 20
) -> list[dict[str, Any]]:
    """The newest ``limit`` commands from the copy, newest last."""
    from coding_agent.operator.commands import list_recent_commands

    try:
        return [
            record.to_dict()
            for record in list_recent_commands(reader.connection, limit)
        ]
    except sqlite3.Error:
        return []


def build_offline_status(
    reader: RunLedgerReader, *, state_dir: Path | None = None
) -> dict[str, Any]:
    """The offline ``status`` result: ``stopped`` with the last recorded rows.

    Instance state is always ``stopped``: there is no live process to be
    ``starting`` or ``active``. The snapshot carries the same ``identity``
    object ``identity`` returns (``mode`` ``offline``). Intake,
    ``last_halt``, ``last_finished`` and ``recent_commands`` are read from
    the ledger copy as last recorded; ``current_attempt`` is ``None`` and
    ``recent_events`` is empty (no live process owns events). The snapshot
    is capped at 256 KiB in the same contract order as the live one.

    ``state_dir`` is the real state directory for the sealed lookup (the
    ledger copy lives in a temporary directory, so the copy's own parent
    is not the archive root); where omitted the copy's parent is used.
    ``provider_capability`` carries every referenced pin's latest
    Provider Capability Report keyed by pin id, or ``None`` where no
    assertion has run yet.
    """
    now = utc_now_z()
    stored = read_instance_row(reader.connection)
    intake = _read_offline_intake(reader)
    last_halt = read_last_halt(reader.connection)
    finished = last_finished(reader, now, state_dir=state_dir)
    recent_commands = _read_offline_commands(reader)
    result = {
        "generated_at": now,
        "identity": offline_identity_from_stored(stored),
        "instance": {
            "state": "stopped",
            "startup_check": None,
            "last_halt": last_halt,
            "observed_at": now,
        },
        "intake": offline_intake_projection(intake, now),
        "current_attempt": None,
        "last_finished": finished,
        "recent_commands": recent_commands,
        "recent_events": [],
        "provider_capability": _read_capability_reports(reader),
    }
    return trim_status_snapshot(result)
