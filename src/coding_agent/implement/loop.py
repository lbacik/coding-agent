from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

from coding_agent.github.issues import TargetIssue
from coding_agent.implement.ceilings import (
    LoopCeilings,
    UsageLedger,
    UsageBreakdown,
    UsageTotals,
    ceiling_crossed,
    usage_breakdown,
)
from coding_agent.implement.context_handoff import (
    ContextWindowConfig,
    ContinuationSnapshot,
    HandoffFailure,
    continuation_message,
    estimate_context_tokens,
    failure_artifact_key,
    load_recorded_snapshot,
    parse_snapshot_response,
    persist_snapshot,
    response_text,
)
from coding_agent.implement.exchange import ExchangeUnit, evict_oldest, flatten
from coding_agent.implement.pinned_prefix import PinnedPrefix, estimate_tokens
from coding_agent.implement.prompt_cache import apply_cache_breakpoints
from coding_agent.implement.progress import (
    AttemptPolicy,
    BudgetSnapshot,
    ProgressEvent,
    ProgressKind,
    PROGRESS_DIFF,
    PROGRESS_TARGETED_RESULT,
    GATE,
    RESERVE,
    SOFT_STALL,
)
from coding_agent.implement.result_capping import ArtifactStore, cap_tool_result
from coding_agent.provider.pinned_model import InvokableToolModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.provider.retry import invoke_with_retry
from coding_agent.validate.diagnostics import diagnostic_requests_loop_stop


def build_opening_messages(prefix: PinnedPrefix, issue: TargetIssue) -> list[BaseMessage]:
    """The Pinned Prefix's own elements opened as separate messages — the
    Attempt Header, each injected skill file, then the Target Issue — never
    `PinnedPrefix.rendered`'s concatenation, which exists for human
    inspection only. This whole list is the Pinned Prefix contract §5
    describes: `run_tool_loop` never hands any of it to the compactor
    (ADR 0011)."""
    messages: list[BaseMessage] = [SystemMessage(content=prefix.attempt_header)]
    messages.extend(
        SystemMessage(content=f"----- {f.label} -----\n{f.text}") for f in prefix.injected_files
    )
    messages.append(
        HumanMessage(content=f"# Target Issue\n\n#{issue.number}: {issue.title}\n\n{issue.body}")
    )
    return messages


@dataclass(frozen=True)
class ToolLoopResult:
    conversation: tuple[BaseMessage, ...]
    """Every opening message plus every assistant turn and tool result
    ever produced, in order — the full audit trail, never itself pruned
    by a context handoff (only what a later request sends to the model is).
    Each assistant turn is exactly the object the model returned (ADR
    0010) — never rebuilt. A capped tool result's content here is the
    capped text actually sent, not the full content stashed in the
    `ArtifactStore`."""
    tool_call_count: int
    usage: UsageTotals
    stopped_by: str | None
    """The ceiling name `ceilings.ceiling_crossed` returned when this loop
    stopped early (`L3-IMP-6`), the targeted diagnostic no-progress signal,
    or `None` where it stopped because a turn called no tool."""
    progress_events: tuple[ProgressEvent, ...] = ()
    policy_events: tuple[str, ...] = ()
    elapsed_seconds: float = 0.0
    finalization_deadline: float | None = None
    handoff_count: int = 0
    handoff_snapshot_digests: tuple[str, ...] = ()
    context_estimates: tuple[int, ...] = ()
    provider_input_tokens: tuple[int, ...] = ()
    provider_usage: tuple[UsageBreakdown, ...] = ()
    handoff_failure_ref: str | None = None


def _estimate_messages_tokens(messages: Sequence[BaseMessage]) -> int:
    return estimate_tokens("".join(str(m.content) for m in messages))


def _estimate_unit_tokens(unit: ExchangeUnit) -> int:
    return _estimate_messages_tokens(unit.messages)


def _preview(text: str, limit: int = 160) -> str:
    """A one-line, bounded rendering for a progress message -- never the
    full tool args or result, which `on_progress` is not the audit trail
    for (`conversation` already is)."""
    flattened = " ".join(text.split())
    return flattened if len(flattened) <= limit else flattened[: limit - 1] + "…"


def _redact_text(text: str, redactions: Mapping[str, str]) -> str:
    redacted = text
    for name, secret in redactions.items():
        if secret:
            redacted = redacted.replace(secret, f"«redacted:{name}»")
    return redacted


def _store_handoff_failure(
    store: ArtifactStore,
    attempt_id: str,
    sequence: int,
    reason: str,
    redactions: Mapping[str, str],
) -> str | None:
    """Keep a bounded, redacted explanation when handoff cannot continue."""
    artifact_ref = failure_artifact_key(attempt_id, sequence)
    content = json.dumps(
        {"classification": HandoffFailure.classification, "reason": _redact_text(reason, redactions)},
        sort_keys=True,
    )
    try:
        store.store(artifact_ref, content)
    except Exception:
        return None
    return artifact_ref


def _record_handoff_failure(
    policy: AttemptPolicy,
    attempt_id: str,
    artifact_ref: str | None,
    reason: str,
    elapsed_seconds: float,
    usage: UsageTotals,
) -> None:
    """Record the classification without allowing a ledger error to resume work."""
    if policy.ledger is None:
        return
    try:
        policy.ledger.record(
            attempt_id,
            "handoff_failure",
            BudgetSnapshot(elapsed_seconds, usage),
            detail=reason,
            artifact_ref=artifact_ref,
            payload={"classification": HandoffFailure.classification},
        )
    except Exception:
        pass


def run_tool_loop(
    model: InvokableToolModel,
    tools: Sequence[BaseTool],
    opening_messages: Sequence[BaseMessage],
    *,
    price: TokenPrices,
    compaction_threshold: int | None = None,
    context_window: ContextWindowConfig | None = None,
    result_cap_limit: int,
    result_store: ArtifactStore,
    ceilings: LoopCeilings,
    usage_ledger: UsageLedger,
    clock: Callable[[], float] = time.monotonic,
    on_progress: Callable[[str], None] = lambda _message: None,
    cache_breakpoints: bool = False,
    policy: AttemptPolicy | None = None,
    progress_probe: Callable[[], str] | None = None,
    progress_detector: Callable[[AIMessage], Iterable[ProgressKind]] | None = None,
    on_progress_event: Callable[[ProgressEvent], None] = lambda _event: None,
    redactions: Mapping[str, str] | None = None,
) -> ToolLoopResult:
    """The bounded tool loop (the runtime contract's `implement` node): bind
    the toolset, invoke, and where the assistant turn calls tools, run each
    and append its `ToolMessage`, then invoke again — until a turn calls
    none, or a ceiling stops it first.

    `opening_messages` is the whole Pinned Prefix (the Attempt Header, the
    injected skill files, and the Target Issue — contract §5); it is sent
    on every request but never touched by handoff. Every assistant turn
    plus the tool results answering it is instead folded into an
    `ExchangeUnit` and kept in the current node's history. A handoff replaces
    that history with an explicit snapshot, never with reconstructed messages,
    so the Pinned Prefix cannot be dropped by construction (ADR 0013).

    An oversized tool result is capped to head, tail and a pointer before
    it is ever appended (`L3-IMP-5`). Usage is flushed to `usage_ledger`
    after every model response and every tool call; the instant a
    configured ceiling is crossed, the loop stops without making another
    model call or running further tool calls for the turn just answered
    (`L3-IMP-6`) — whatever usage was already flushed stays flushed.

    The assistant turn is appended exactly as the model returned it,
    never rebuilt from `.content`/`.tool_calls` (ADR 0010), so a
    provider-specific block this loop does not interpret survives to the
    next request instead of being silently dropped.

    `on_progress` is an optional sink for one-line, human-readable status
    updates (a model turn starting and returning, each tool call starting
    and finishing, a ceiling tripping, history being compacted) -- the
    loop otherwise runs and reports nothing until it returns, which on a
    long Attempt looks indistinguishable from a hang. It defaults to a
    no-op so every existing caller and test is unaffected; `cli.py` is
    the only caller that wires it to `print` today.

    Each turn's update reports two distinct numbers that are easy to
    conflate: the *estimated* size of the request about to be sent
    (`prefix_tokens` plus the still-evictable history, the same estimate
    `compaction_threshold` bounds -- an `estimate_tokens` heuristic, never
    a provider's real count), and, once the response is back, the
    *actual* `usage_metadata` the provider reported, split via
    `ceilings.usage_breakdown` into cache-read, cache-write and uncached
    input tokens -- the only place this loop surfaces whether a request
    hit the provider's prompt cache.

    `context_window` selects the replacement handoff policy. Its deterministic
    estimate includes the Pinned Prefix, whole Exchange Units, tool schemas,
    request overhead and safety margin. Before an over-window request is sent,
    one dedicated model invocation writes a redacted Continuation Snapshot;
    the next work node contains only the Pinned Prefix and that snapshot. A
    recorded snapshot is loaded by digest before any new handoff, making
    restart after persistence idempotent. The old `compaction_threshold`
    argument remains a compatibility path for the pre-handoff S3 tests; real
    Attempts pass `context_window`.

    `cache_breakpoints`, where `True`, tags the outgoing request (never
    `opening_messages`, `history` or `conversation` themselves --
    `prompt_cache.apply_cache_breakpoints` returns a new list) with two
    Anthropic `cache_control` breakpoints -- the Pinned Prefix's end and
    the growing tail's end -- so the provider's own prompt cache, not
    just this loop's history eviction, keeps a long-running Attempt from
    re-billing its whole conversation on every turn. Anthropic-only; a
    caller on another provider's pin leaves this `False`.
    """
    if context_window is None and compaction_threshold is None:
        raise ValueError("either context_window or compaction_threshold is required")
    bound = model.bind_tools(tools)
    policy_enabled = policy is not None or progress_probe is not None or progress_detector is not None
    active_policy = policy or AttemptPolicy("attempt", ceilings)
    tools_by_name = {tool.name: tool for tool in tools}
    conversation: list[BaseMessage] = list(opening_messages)
    history: list[ExchangeUnit] = []
    tool_call_count = 0
    stopped_by: str | None = None
    start = clock()
    prefix_tokens = _estimate_messages_tokens(opening_messages)
    turn = 0
    guidance_pending = False
    candidate_has_diff = False
    handoff_count = 0
    handoff_snapshot_digests: list[str] = []
    context_estimates: list[int] = []
    provider_input_tokens: list[int] = []
    provider_usage: list[UsageBreakdown] = []
    handoff_failure_ref: str | None = None
    continuation: ContinuationSnapshot | None = None
    if context_window is not None and active_policy.ledger is not None:
        try:
            continuation = load_recorded_snapshot(active_policy.ledger, result_store, active_policy.attempt_id)
        except HandoffFailure as exc:
            handoff_failure_ref = _store_handoff_failure(
                result_store, active_policy.attempt_id, 0, str(exc), redactions or {}
            )
            _record_handoff_failure(
                active_policy,
                active_policy.attempt_id,
                handoff_failure_ref,
                _redact_text(str(exc), redactions or {}),
                0.0,
                usage_ledger.totals,
            )
            return ToolLoopResult(
                conversation=tuple(conversation),
                tool_call_count=0,
                usage=usage_ledger.totals,
                stopped_by=HandoffFailure.classification,
                handoff_failure_ref=handoff_failure_ref,
            )

    def store_failure(sequence: int, reason: str) -> str | None:
        artifact_ref = _store_handoff_failure(
            result_store, active_policy.attempt_id, sequence, reason, redactions or {}
        )
        _record_handoff_failure(
            active_policy,
            active_policy.attempt_id,
            artifact_ref,
            _redact_text(reason, redactions or {}),
            clock() - start,
            usage_ledger.totals,
        )
        return artifact_ref

    def handoff(history_to_handoff: Sequence[ExchangeUnit], estimate: int) -> ContinuationSnapshot:
        """Run the one-call handoff node and durably record its snapshot."""
        assert context_window is not None
        transcript_messages: list[BaseMessage] = [*opening_messages]
        if continuation is not None:
            transcript_messages.append(continuation_message(continuation))
        transcript_messages.extend(flatten(history_to_handoff))
        transcript = "\n\n".join(
            _redact_text(str(message.content), redactions or {})
            for message in transcript_messages
        )
        request = [
            SystemMessage(
                content=(
                    "You are the bounded context handoff node. Return only one JSON object with "
                    "version, attempt_id, base_revision, branch, commit, changed_files, commands "
                    "(each with command and result), validation_evidence, unresolved_questions, "
                    "and next_intended_action. Record explicit facts only; never include secrets "
                    "or provider-specific reasoning."
                )
            ),
            HumanMessage(content=f"Current work-node transcript (estimated context {estimate}):\n{transcript}"),
        ]
        try:
            response = model.invoke(request)
        except Exception as exc:
            raise HandoffFailure(f"handoff model invocation failed: {exc}") from exc
        conversation.append(response)
        usage_metadata = getattr(response, "usage_metadata", None)
        totals = usage_ledger.flush_model_response(usage_metadata, price)
        breakdown = usage_breakdown(usage_metadata)
        provider_input_tokens.append(breakdown.input_tokens)
        provider_usage.append(breakdown)
        if policy_enabled:
            active_policy.record_usage(BudgetSnapshot(clock() - start, totals), source="handoff")
        crossed = ceiling_crossed(totals, clock() - start, ceilings)
        if crossed is not None:
            raise HandoffFailure(f"handoff crossed the Attempt {crossed!r} ceiling")
        observed_output_tokens = max(
            breakdown.output_tokens,
            estimate_tokens(response_text(getattr(response, "content", ""))),
        )
        if observed_output_tokens > context_window.handoff_token_cap:
            raise HandoffFailure(
                f"handoff output used {observed_output_tokens} tokens, over the "
                f"{context_window.handoff_token_cap}-token handoff cap"
            )
        if getattr(response, "tool_calls", ()):
            raise HandoffFailure("handoff node returned tool calls instead of a snapshot")
        snapshot = parse_snapshot_response(
            getattr(response, "content", ""), redactions=redactions or {}
        )
        if snapshot.attempt_id != active_policy.attempt_id:
            raise HandoffFailure(
                f"snapshot belongs to {snapshot.attempt_id!r}, not {active_policy.attempt_id!r}"
            )
        artifact_ref = persist_snapshot(result_store, snapshot)
        if policy_enabled:
            record = active_policy.ledger.record if active_policy.ledger is not None else None
            if record is not None:
                try:
                    record(
                        active_policy.attempt_id,
                        "handoff",
                        BudgetSnapshot(clock() - start, totals),
                        artifact_ref=artifact_ref,
                        payload={
                            "snapshot_digest": snapshot.digest,
                            "snapshot_version": snapshot.version,
                            "input_estimate_tokens": estimate,
                            "provider_input_tokens": breakdown.input_tokens,
                            "provider_output_tokens": breakdown.output_tokens,
                            "observed_output_tokens": observed_output_tokens,
                            "handoff_token_cap": context_window.handoff_token_cap,
                        },
                    )
                except Exception as exc:
                    raise HandoffFailure(f"could not record handoff in the Run Ledger: {exc}") from exc
        return snapshot

    def snapshot() -> BudgetSnapshot:
        return BudgetSnapshot(clock() - start, usage_ledger.totals)

    def record_progress(
        kind: str,
        current: BudgetSnapshot,
        *,
        artifact_ref: str | None = None,
        diff_ref: str | None = None,
    ) -> None:
        event = active_policy.record_progress(
            kind, current, artifact_ref=artifact_ref, diff_ref=diff_ref
        )
        on_progress_event(event)

    def tool_action(name: str) -> str:
        if name in {"write_file", "edit_file"}:
            return "implementation"
        if name in {"test_targeted", "read_result_slice"}:
            return "verification"
        return "exploration"

    def diagnostic_is_progress(content: str) -> bool:
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return False
        return (
            isinstance(payload, dict)
            and bool(payload.get("classification"))
            and not bool(payload.get("suppressed"))
            and not bool(payload.get("stop_loop"))
        )

    def reserve_entered(transitions: Sequence[str]) -> bool:
        return policy_enabled and (
            RESERVE in transitions or active_policy.phase == "verification_reserve"
        )

    while True:
        turn += 1
        budget_transitions = active_policy.observe_budget(snapshot()) if policy_enabled else ()
        if reserve_entered(budget_transitions):
            stopped_by = "verification-reserve"
            on_progress("verification reserve entered: no further model responses")
            break
        if policy_enabled and active_policy.soft_stalled:
            guidance_pending = True
        estimated_context = (
            estimate_context_tokens(
                opening_messages, history, tools, continuation=continuation
            )
            if context_window is not None
            else prefix_tokens + sum(_estimate_unit_tokens(u) for u in history)
        )
        context_estimates.append(estimated_context)
        if context_window is not None and estimated_context > context_window.usable_threshold:
            if continuation is not None and not history:
                reason = "continuation snapshot does not fit the configured context window"
                handoff_failure_ref = store_failure(handoff_count, reason)
                stopped_by = HandoffFailure.classification
                on_progress(f"context handoff failed: {reason}")
                break
            if continuation is None and not history:
                reason = "Pinned Prefix and tool request overhead do not fit the configured context window"
                handoff_failure_ref = store_failure(handoff_count, reason)
                stopped_by = HandoffFailure.classification
                on_progress(f"context handoff failed: {reason}")
                break
            try:
                next_snapshot = handoff(history, estimated_context)
            except HandoffFailure as exc:
                handoff_failure_ref = store_failure(handoff_count, str(exc))
                stopped_by = HandoffFailure.classification
                on_progress(f"context handoff failed: {_redact_text(str(exc), redactions or {})}")
                break
            handoff_count += 1
            handoff_snapshot_digests.append(next_snapshot.digest)
            continuation = next_snapshot
            history = []
            on_progress(
                f"context handoff {handoff_count}: snapshot {next_snapshot.digest} persisted; "
                "starting a fresh work node"
            )
            continue
        on_progress(
            f"model turn {turn}: waiting on the model... "
            f"(context ~{estimated_context}/"
            f"{context_window.threshold_tokens if context_window is not None else compaction_threshold} estimated tokens)"
        )
        sent: list[BaseMessage] = [*opening_messages, *flatten(history)]
        if continuation is not None:
            sent = [*opening_messages, continuation_message(continuation), *flatten(history)]
        if guidance_pending:
            sent.append(
                HumanMessage(
                    content=(
                        "Soft stall: take one narrow action that creates a progress event, "
                        "prepare the Delivery Snapshot, or verify the existing diff. "
                        "Do not perform broad exploration."
                    )
                )
            )
            guidance_pending = False
        request_messages = (
            apply_cache_breakpoints(sent, prefix_length=len(opening_messages))
            if cache_breakpoints
            else sent
        )
        response = invoke_with_retry(bound, request_messages)
        conversation.append(response)

        usage_metadata = response.usage_metadata if isinstance(response, AIMessage) else None
        totals = usage_ledger.flush_model_response(usage_metadata, price)
        breakdown = usage_breakdown(usage_metadata)
        provider_input_tokens.append(breakdown.input_tokens)
        provider_usage.append(breakdown)
        tool_calls = response.tool_calls if isinstance(response, AIMessage) else []
        on_progress(
            f"model turn {turn} responded: {len(tool_calls)} tool call(s) requested; "
            f"input={breakdown.input_tokens} (cache_read={breakdown.cache_read_tokens}, "
            f"cache_write={breakdown.cache_write_tokens}, "
            f"uncached={breakdown.uncached_input_tokens}) output={breakdown.output_tokens}; "
            f"usage so far: effective_tokens={totals.effective_tokens} "
            f"reported_tokens={totals.tokens} cost=${totals.cost_usd:.4f}"
        )
        response_snapshot = snapshot() if policy_enabled else BudgetSnapshot(0.0, totals)
        if policy_enabled:
            active_policy.record_usage(response_snapshot, source="model_response")
        response_progress = False
        if policy_enabled and progress_detector is not None and isinstance(response, AIMessage):
            for kind in progress_detector(response):
                record_progress(kind, response_snapshot)
                response_progress = True
        stopped_by = ceiling_crossed(totals, clock() - start, ceilings)
        if stopped_by is not None:
            on_progress(f"ceiling crossed: {stopped_by}")
            break

        budget_transitions = active_policy.observe_budget(response_snapshot) if policy_enabled else ()
        if reserve_entered(budget_transitions):
            stopped_by = "verification-reserve"
            on_progress("verification reserve entered: preserving capacity for finalization")
            break

        if not tool_calls:
            if policy_enabled:
                stall_transitions = active_policy.observe_response(
                    response_snapshot, progress=response_progress
                )
                if SOFT_STALL in stall_transitions:
                    on_progress("soft stall: two model responses without qualifying progress")
            break
        # `tool_calls` is only ever non-empty on the `isinstance` branch
        # above, so `response` is an `AIMessage` here -- narrowed
        # explicitly for `ExchangeUnit`, which an `ExchangeUnit` never
        # holds anything else (ADR 0010: the assistant turn is always the
        # provider's own `AIMessage`).
        assert isinstance(response, AIMessage)

        results: list[ToolMessage] = []
        diff_before = progress_probe() if progress_probe is not None else None
        for call in tool_calls:
            tool_call_count += 1
            action = tool_action(call["name"])
            if policy_enabled and not active_policy.allows(action):
                stopped_by = "soft-stall" if active_policy.soft_stalled else GATE
                on_progress(f"policy stopped disallowed {action} action: {call['name']}")
                break
            # A provider is expected to always send one; a fallback keeps
            # the artifact store and `ToolMessage` keyed on a real string
            # rather than propagating `None` into either.
            call_id = call["id"] or f"unidentified-{tool_call_count}"
            on_progress(f"tool call {tool_call_count}: {call['name']}({_preview(str(call['args']))})")
            tool = tools_by_name.get(call["name"])
            if tool is None:
                content = f"Error: no such tool {call['name']!r}"
            else:
                try:
                    content = str(tool.invoke(call["args"]))
                except Exception as exc:  # a tool's own failure never ends the loop
                    content = f"Error: {exc}"
            content = cap_tool_result(
                content, tool_call_id=call_id, limit=result_cap_limit, store=result_store
            )
            on_progress(f"tool call {tool_call_count} finished: {call['name']} -> {_preview(content)}")
            message = ToolMessage(content=content, tool_call_id=call_id)
            conversation.append(message)
            results.append(message)

            totals = usage_ledger.flush_tool_call()
            current_snapshot = snapshot() if policy_enabled else BudgetSnapshot(0.0, totals)
            if policy_enabled:
                active_policy.record_usage(current_snapshot, source="tool_call")
            diff_after = progress_probe() if progress_probe is not None else None
            if (
                action == "implementation"
                and diff_before is not None
                and diff_after != diff_before
            ):
                if candidate_has_diff:
                    record_progress(PROGRESS_DIFF, current_snapshot, diff_ref=diff_after)
                    response_progress = True
                else:
                    # The first edit is telemetry only. A later change to the
                    # candidate diff is the qualifying progress event.
                    candidate_has_diff = True
            elif (
                action == "verification"
                and call["name"] == "test_targeted"
                and diagnostic_is_progress(content)
                and any(event.kind == "diff" for event in active_policy.progress_events)
            ):
                record_progress(PROGRESS_TARGETED_RESULT, current_snapshot, artifact_ref=call_id)
                response_progress = True
            stopped_by = ceiling_crossed(totals, clock() - start, ceilings)
            if stopped_by is None and diagnostic_requests_loop_stop(content):
                if policy_enabled:
                    active_policy.observe_diagnostic(current_snapshot)
                stopped_by = "targeted-diagnostic-no-progress"
                on_progress("tool loop stopped: targeted diagnostic made no further progress")
            if stopped_by is not None:
                if stopped_by != "targeted-diagnostic-no-progress":
                    on_progress(f"ceiling crossed: {stopped_by}")
                break
            budget_transitions = active_policy.observe_budget(current_snapshot) if policy_enabled else ()
            if reserve_entered(budget_transitions):
                stopped_by = "verification-reserve"
                on_progress("verification reserve entered: preserving capacity for finalization")
                break

            diff_before = diff_after

        # A ceiling crossed partway through this turn (the `break` above)
        # means `results` can be shorter than `response.tool_calls` here --
        # an `ExchangeUnit` that pairs fewer results than its assistant
        # turn requested. That never becomes a malformed request, because
        # the loop always stops (below) the same iteration it happens:
        # this unit is appended for the audit trail in `conversation`
        # only, never flattened into a later one.
        history.append(ExchangeUnit(assistant=response, results=tuple(results)))
        if stopped_by is not None:
            break

        if policy_enabled:
            stall_transitions = active_policy.observe_response(
                current_snapshot if results else response_snapshot,
                progress=response_progress,
            )
            if SOFT_STALL in stall_transitions:
                on_progress("soft stall: two model responses without qualifying progress")

        if context_window is None:
            assert compaction_threshold is not None
            total_estimate = prefix_tokens + sum(_estimate_unit_tokens(u) for u in history)
            if total_estimate > compaction_threshold:
                budget = max(0, compaction_threshold - prefix_tokens)
                evicted_from = len(history)
                history = list(
                    evict_oldest(history, estimate=_estimate_unit_tokens, budget_tokens=budget)
                )
                on_progress(
                    f"legacy compacted history: {evicted_from} -> {len(history)} exchange unit(s) "
                    f"(~{total_estimate} tokens over the {compaction_threshold} threshold)"
                )

    return ToolLoopResult(
        conversation=tuple(conversation),
        tool_call_count=tool_call_count,
        usage=usage_ledger.totals,
        stopped_by=stopped_by,
        progress_events=tuple(active_policy.progress_events) if policy_enabled else (),
        policy_events=tuple(active_policy.policy_events) if policy_enabled else (),
        elapsed_seconds=clock() - start if policy_enabled else 0.0,
        finalization_deadline=(
            start + ceilings.max_wall_clock_seconds
            if policy_enabled and ceilings.max_wall_clock_seconds is not None
            else None
        ),
        handoff_count=handoff_count,
        handoff_snapshot_digests=tuple(handoff_snapshot_digests),
        context_estimates=tuple(context_estimates),
        provider_input_tokens=tuple(provider_input_tokens),
        provider_usage=tuple(provider_usage),
        handoff_failure_ref=handoff_failure_ref,
    )
