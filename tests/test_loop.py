from __future__ import annotations

import json
from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, tool

from coding_agent.github.issues import TargetIssue
from coding_agent.implement.ceilings import InMemoryUsageLedger, LoopCeilings, UsageTotals
from coding_agent.implement.loop import ToolLoopResult, build_opening_messages, run_tool_loop
from coding_agent.implement.pinned_prefix import InjectedFile, PinnedPrefix, estimate_tokens
from coding_agent.implement.result_capping import (
    ArtifactNotFound,
    InMemoryArtifactStore,
    build_read_result_slice_tool,
    cap_tool_result,
)
from coding_agent.provider.price_table import TokenPrices
from conftest import FakeChatModel

_PRICE = TokenPrices(input=2.0, output=10.0, cache_read=0.2, cache_write=2.5)
_UNBOUNDED = LoopCeilings()


def _run(
    model: FakeChatModel,
    tools: Sequence[BaseTool],
    opening_messages: Sequence[BaseMessage],
    **overrides: object,
) -> ToolLoopResult:
    """`run_tool_loop` with every budget generous enough that none of it
    kicks in — the shape every pre-existing loop test wants. A test
    exercising capping, compaction or a ceiling overrides just the one
    knob it cares about."""
    kwargs: dict[str, object] = dict(
        price=_PRICE,
        compaction_threshold=1_000_000,
        result_cap_limit=1_000_000,
        result_store=InMemoryArtifactStore(),
        ceilings=_UNBOUNDED,
        usage_ledger=InMemoryUsageLedger(),
    )
    kwargs.update(overrides)
    return run_tool_loop(model, tools, opening_messages, **kwargs)  # type: ignore[arg-type]


@tool
def fake_write_file(path: str, content: str) -> str:
    """Records a write for the test to inspect."""
    return f"wrote {path!r}"


def _ai_message(*, tool_calls: list[dict[str, object]] | None = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=tool_calls or [],
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )


def _tool_call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


# --- well-formed tool call round trip ---------------------------------------


def test_run_tool_loop_executes_a_tool_call_then_stops() -> None:
    call_response = _ai_message(
        tool_calls=[
            {
                "name": "fake_write_file",
                "args": {"path": "a.txt", "content": "hi"},
                "id": "call-1",
                "type": "tool_call",
            }
        ]
    )
    final_response = AIMessage(content="done", tool_calls=[])
    model = FakeChatModel([call_response, final_response])

    result = _run(model, [fake_write_file], [SystemMessage(content="hello")])

    assert result.tool_call_count == 1
    assert result.stopped_by is None
    assert result.conversation[0] == SystemMessage(content="hello")
    assert result.conversation[1] is call_response
    tool_message = result.conversation[2]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "call-1"
    assert tool_message.content == "wrote 'a.txt'"
    assert result.conversation[3] is final_response

    # The second invoke saw the tool result appended to the conversation.
    assert model.invocations[1][-1] is tool_message


def test_run_tool_loop_stops_immediately_when_no_tool_is_called() -> None:
    final_response = AIMessage(content="nothing to do", tool_calls=[])
    model = FakeChatModel([final_response])

    result = _run(model, [fake_write_file], [SystemMessage(content="hello")])

    assert result.tool_call_count == 0
    assert result.stopped_by is None
    assert result.conversation == (SystemMessage(content="hello"), final_response)


def test_run_tool_loop_stops_when_targeted_diagnostics_report_no_progress() -> None:
    @tool
    def targeted() -> str:
        """Return the adapter's terminal diagnostic signal."""
        return json.dumps({"classification": "infrastructure_failure", "stop_loop": True})

    call_response = _ai_message(
        tool_calls=[_tool_call("targeted", {}, "call-1")]
    )
    model = FakeChatModel([call_response, AIMessage(content="must not be called", tool_calls=[])])

    result = _run(model, [targeted], [SystemMessage(content="hello")])

    assert result.stopped_by == "targeted-diagnostic-no-progress"
    assert len(model.invocations) == 1


def test_run_tool_loop_reports_an_unknown_tool_without_raising() -> None:
    call_response = _ai_message(
        tool_calls=[{"name": "no_such_tool", "args": {}, "id": "call-1", "type": "tool_call"}]
    )
    final_response = AIMessage(content="done", tool_calls=[])
    model = FakeChatModel([call_response, final_response])

    result = _run(model, [fake_write_file], [SystemMessage(content="hello")])

    tool_message = result.conversation[2]
    assert isinstance(tool_message, ToolMessage)
    assert "no such tool" in str(tool_message.content)


def test_run_tool_loop_reports_usage_flushed_to_the_ledger() -> None:
    call_response = _ai_message(
        tool_calls=[_tool_call("fake_write_file", {"path": "a.txt", "content": "hi"}, "call-1")]
    )
    final_response = AIMessage(
        content="done", tool_calls=[], usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}
    )
    model = FakeChatModel([call_response, final_response])

    result = _run(model, [fake_write_file], [SystemMessage(content="hello")])

    # Two model responses (2 + 7 tokens) and one tool call flushed.
    assert result.usage.tokens == 9
    assert result.usage.tool_calls == 1


# --- ADR 0010: the assistant turn survives verbatim, opaque blocks included --


def test_run_tool_loop_appends_an_opaque_block_assistant_turn_verbatim() -> None:
    """A response carrying a provider-specific block (e.g. encrypted
    `reasoning` content on the Responses API) must come back exactly as
    received — this loop never rebuilds it from `.content`/`.tool_calls`."""
    opaque_response = AIMessage(
        content=[
            {"type": "reasoning", "encrypted_content": "opaque-blob-xyz"},
            {"type": "text", "text": "thinking..."},
        ],
        tool_calls=[
            {
                "name": "fake_write_file",
                "args": {"path": "a.txt", "content": "hi"},
                "id": "call-1",
                "type": "tool_call",
            }
        ],
        additional_kwargs={"some_provider_field": "must-not-be-dropped"},
    )
    final_response = AIMessage(content="done", tool_calls=[])
    model = FakeChatModel([opaque_response, final_response])

    result = _run(model, [fake_write_file], [SystemMessage(content="hello")])

    assert result.conversation[1] is opaque_response
    # The same opaque object, unmodified, is what the second call actually sent back.
    assert model.invocations[1][1] is opaque_response
    stored = result.conversation[1]
    assert isinstance(stored, AIMessage)
    assert stored.additional_kwargs == {"some_provider_field": "must-not-be-dropped"}
    assert stored.content == opaque_response.content


# --- cache_breakpoints: Anthropic `cache_control` on the outgoing request ---


def test_run_tool_loop_leaves_messages_untagged_by_default() -> None:
    call_response = _ai_message(
        tool_calls=[_tool_call("fake_write_file", {"path": "a.txt", "content": "hi"}, "call-1")]
    )
    final_response = AIMessage(content="done", tool_calls=[])
    model = FakeChatModel([call_response, final_response])

    _run(model, [fake_write_file], [SystemMessage(content="the pinned prefix")])

    for request in model.invocations:
        assert request[0].content == "the pinned prefix"


def test_run_tool_loop_tags_the_prefix_end_and_the_tail_end_when_cache_breakpoints_is_set() -> None:
    call_response = _ai_message(
        tool_calls=[_tool_call("fake_write_file", {"path": "a.txt", "content": "hi"}, "call-1")]
    )
    final_response = AIMessage(content="done", tool_calls=[])
    model = FakeChatModel([call_response, final_response])
    opening = [SystemMessage(content="the pinned prefix")]

    _run(model, [fake_write_file], opening, cache_breakpoints=True)

    first_request = model.invocations[0]
    # A single-message Pinned Prefix on the first turn: the prefix
    # breakpoint and the tail breakpoint land on the same message.
    assert first_request[0].content == [
        {"type": "text", "text": "the pinned prefix", "cache_control": {"type": "ephemeral"}}
    ]

    second_request = model.invocations[1]
    # The Pinned Prefix is tagged again...
    assert second_request[0].content == [
        {"type": "text", "text": "the pinned prefix", "cache_control": {"type": "ephemeral"}}
    ]
    # ...and so is the new tail end (the tool result the first turn produced).
    tool_message = second_request[-1]
    assert isinstance(tool_message, ToolMessage)
    tool_message_blocks = tool_message.content
    assert isinstance(tool_message_blocks, list)
    last_block = tool_message_blocks[-1]
    assert isinstance(last_block, dict)
    assert last_block["cache_control"] == {"type": "ephemeral"}


def test_run_tool_loop_cache_breakpoints_never_leak_into_the_stored_conversation() -> None:
    """`cache_breakpoints` tags only what is sent over the wire --
    `ToolLoopResult.conversation` (the audit trail) and the `history` a
    later eviction is measured against must come back exactly as
    produced, plain content and all."""
    call_response = _ai_message(
        tool_calls=[_tool_call("fake_write_file", {"path": "a.txt", "content": "hi"}, "call-1")]
    )
    final_response = AIMessage(content="done", tool_calls=[])
    model = FakeChatModel([call_response, final_response])

    result = _run(
        model, [fake_write_file], [SystemMessage(content="the pinned prefix")], cache_breakpoints=True
    )

    assert result.conversation[0] == SystemMessage(content="the pinned prefix")
    tool_message = result.conversation[2]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.content == "wrote 'a.txt'"


# --- build_opening_messages ---------------------------------------------------


def test_build_opening_messages_lays_out_header_files_and_issue_as_separate_elements() -> None:
    prefix = PinnedPrefix(
        attempt_header="# Attempt Header\n...",
        injected_files=(
            InjectedFile(label="implement/SKILL.md", text="implement skill text"),
            InjectedFile(label="tdd/SKILL.md", text="tdd skill text"),
        ),
    )
    issue = TargetIssue(number=34, title="Do the thing", body="Body text")

    messages = build_opening_messages(prefix, issue)

    assert len(messages) == 4
    assert messages[0] == SystemMessage(content=prefix.attempt_header)
    assert isinstance(messages[1], SystemMessage)
    assert "implement/SKILL.md" in str(messages[1].content)
    assert "implement skill text" in str(messages[1].content)
    assert isinstance(messages[2], SystemMessage)
    assert "tdd/SKILL.md" in str(messages[2].content)
    last = messages[3]
    assert "#34" in str(last.content)
    assert "Do the thing" in str(last.content)
    assert "Body text" in str(last.content)


# --- L3-IMP-5: an oversized tool result is capped, and a slice is readable --


def test_run_tool_loop_caps_an_oversized_tool_result_and_stores_the_full_content() -> None:
    @tool
    def dump() -> str:
        """Return a lot of text."""
        return "z" * 5000

    call_response = _ai_message(
        tool_calls=[_tool_call("dump", {}, "call-1")]
    )
    final_response = AIMessage(content="done", tool_calls=[])
    model = FakeChatModel([call_response, final_response])
    store = InMemoryArtifactStore()

    result = _run(model, [dump], [SystemMessage(content="hello")], result_cap_limit=100, result_store=store)

    tool_message = result.conversation[2]
    assert isinstance(tool_message, ToolMessage)
    assert len(str(tool_message.content)) < 5000
    assert "call-1" in str(tool_message.content)
    assert "5000" in str(tool_message.content)
    assert store.read("call-1") == "z" * 5000


def test_run_tool_loop_leaves_a_small_tool_result_uncapped() -> None:
    call_response = _ai_message(
        tool_calls=[_tool_call("fake_write_file", {"path": "a.txt", "content": "hi"}, "call-1")]
    )
    final_response = AIMessage(content="done", tool_calls=[])
    model = FakeChatModel([call_response, final_response])
    store = InMemoryArtifactStore()

    result = _run(
        model, [fake_write_file], [SystemMessage(content="hello")], result_cap_limit=100, result_store=store
    )

    tool_message = result.conversation[2]
    assert tool_message.content == "wrote 'a.txt'"
    try:
        store.read("call-1")
        raised = False
    except ArtifactNotFound:
        raised = True
    assert raised


def test_run_tool_loop_sends_the_capped_not_the_full_tool_result_on_the_next_request() -> None:
    """Capping happens before the `ExchangeUnit` is built, so a later
    model request — and any compaction budget it is measured against —
    sees the capped text, not the full content."""

    @tool
    def dump() -> str:
        """Return a lot of text."""
        return "q" * 5000

    call_response = _ai_message(tool_calls=[_tool_call("dump", {}, "call-1")])
    final_response = AIMessage(content="done", tool_calls=[])
    model = FakeChatModel([call_response, final_response])

    _run(model, [dump], [SystemMessage(content="hello")], result_cap_limit=100)

    second_request = model.invocations[1]
    sent_tool_message = second_request[-1]
    assert isinstance(sent_tool_message, ToolMessage)
    assert len(str(sent_tool_message.content)) < 5000


# --- L3-IMP-4, L3-IMP-12: Exchange Unit compaction --------------------------


def test_run_tool_loop_evicts_the_oldest_exchange_unit_once_over_the_compaction_threshold() -> None:
    """Three tool-calling turns, each one producing a result big enough to
    force at least one compaction. The final request must no longer carry
    the very first turn's tool result, even though the full audit trail
    still does."""

    @tool
    def dump(tag: str) -> str:
        """Return a lot of text tagged so a test can tell turns apart."""
        return f"marker-{tag}-" + "x" * 400

    responses: list[AIMessage] = []
    for i in range(3):
        responses.append(_ai_message(tool_calls=[_tool_call("dump", {"tag": str(i)}, f"call-{i}")]))
    responses.append(AIMessage(content="done", tool_calls=[]))
    model = FakeChatModel(responses)

    # A threshold sized to hold roughly one turn's worth of content atop
    # the (empty) opening prefix, forcing eviction on the later turns.
    threshold = estimate_tokens("marker-0-" + "x" * 400) + 5

    result = _run(model, [dump], [SystemMessage(content="hi")], compaction_threshold=threshold)

    assert result.tool_call_count == 3
    last_request = model.invocations[-1]
    assert not any(
        isinstance(m, ToolMessage) and "marker-0-" in str(m.content) for m in last_request
    )
    assert any(isinstance(m, ToolMessage) and "marker-2-" in str(m.content) for m in last_request)
    # The full, uncompacted audit trail still has every turn.
    tool_messages = [m for m in result.conversation if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 3
    assert any("marker-0-" in str(m.content) for m in tool_messages)


def test_run_tool_loop_never_evicts_the_pinned_prefix() -> None:
    """Even with a compaction threshold so small the prefix alone would
    overflow it, the opening messages are sent on every request — nothing
    in `evict_oldest`'s own signature could have dropped them (see
    `test_exchange.py`); this proves the wiring here does not either."""
    call_response = _ai_message(
        tool_calls=[_tool_call("fake_write_file", {"path": "a.txt", "content": "x" * 400}, "call-1")]
    )
    final_response = AIMessage(content="done", tool_calls=[])
    model = FakeChatModel([call_response, final_response])
    opening = [SystemMessage(content="the pinned prefix")]

    _run(model, [fake_write_file], opening, compaction_threshold=1)

    for request in model.invocations:
        assert request[0] == SystemMessage(content="the pinned prefix")


def test_run_tool_loop_does_not_evict_when_under_the_compaction_threshold() -> None:
    call_response = _ai_message(
        tool_calls=[_tool_call("fake_write_file", {"path": "a.txt", "content": "hi"}, "call-1")]
    )
    final_response = AIMessage(content="done", tool_calls=[])
    model = FakeChatModel([call_response, final_response])

    _run(model, [fake_write_file], [SystemMessage(content="hi")], compaction_threshold=1_000_000)

    second_request = model.invocations[1]
    assert any(isinstance(m, ToolMessage) and m.tool_call_id == "call-1" for m in second_request)


# --- L3-IMP-6, L3-IMP-8: ceilings stop the loop mid-loop, usage is preserved


def test_run_tool_loop_stops_immediately_when_the_wall_clock_ceiling_is_crossed_mid_loop() -> None:
    first_response = AIMessage(content="should not matter", tool_calls=[])
    never_reached = AIMessage(content="should not run", tool_calls=[])
    model = FakeChatModel([first_response, never_reached])
    # `clock()` is called once for `start`, then once per ceiling check;
    # the second call reports 1000 seconds elapsed, over the ceiling.
    ticks = iter([0.0, 1000.0])

    result = _run(
        model,
        [fake_write_file],
        [SystemMessage(content="hi")],
        ceilings=LoopCeilings(max_wall_clock_seconds=60),
        clock=lambda: next(ticks),
    )

    assert result.stopped_by == "wall_clock"
    assert len(model.invocations) == 1


def test_run_tool_loop_stops_immediately_when_the_token_ceiling_is_crossed_mid_loop() -> None:
    call_response = _ai_message(
        tool_calls=[_tool_call("fake_write_file", {"path": "a.txt", "content": "hi"}, "call-1")]
    )
    # A second turn the loop must never reach.
    never_reached = AIMessage(content="should not run", tool_calls=[])
    model = FakeChatModel([call_response, never_reached])

    result = _run(
        model,
        [fake_write_file],
        [SystemMessage(content="hi")],
        ceilings=LoopCeilings(max_effective_tokens=1),
    )

    assert result.stopped_by == "tokens"
    assert len(model.invocations) == 1
    # The turn that crossed the ceiling is still recorded and its usage kept.
    assert result.usage.tokens == 2


def test_run_tool_loop_does_not_cross_effective_token_ceiling_from_repeated_cache_reads() -> None:
    call_response = AIMessage(
        content="",
        tool_calls=[_tool_call("fake_write_file", {"path": "a.txt", "content": "hi"}, "call-1")],
        usage_metadata={"input_tokens": 10, "output_tokens": 0, "total_tokens": 10},
    )
    cached_final_response = AIMessage(
        content="done",
        tool_calls=[],
        usage_metadata={
            "input_tokens": 100_000,
            "output_tokens": 0,
            "total_tokens": 100_000,
            "input_token_details": {"cache_read": 100_000},
        },
    )
    model = FakeChatModel([call_response, cached_final_response])

    result = _run(
        model,
        [fake_write_file],
        [SystemMessage(content="hi")],
        ceilings=LoopCeilings(max_effective_tokens=10),
    )

    assert result.stopped_by is None
    assert len(model.invocations) == 2
    assert result.usage.tokens == 100_010
    assert result.usage.effective_tokens == 10


def test_run_tool_loop_progress_reports_effective_and_reported_tokens_separately() -> None:
    progress: list[str] = []
    response = AIMessage(
        content="done",
        tool_calls=[],
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "input_token_details": {"cache_read": 8},
        },
    )

    _run(
        FakeChatModel([response]),
        [fake_write_file],
        [SystemMessage(content="hi")],
        on_progress=progress.append,
    )

    response_progress = next(message for message in progress if "responded:" in message)
    assert "effective_tokens=4" in response_progress
    assert "reported_tokens=12" in response_progress


def test_run_tool_loop_stops_mid_turn_when_the_tool_call_ceiling_is_crossed() -> None:
    """Checked the same way as every other ceiling — right after it is
    flushed (`usage_ledger.flush_tool_call()`) — so with `max_tool_calls=1`
    the call that pushes the running total from 1 to 2 is the one whose
    flush detects the crossing; it has, by then, already run. What the
    ceiling guarantees is that the loop stops immediately once that's
    known, never that the triggering call itself is skipped."""
    call_response = _ai_message(
        tool_calls=[
            _tool_call("fake_write_file", {"path": "a.txt", "content": "hi"}, "call-1"),
            _tool_call("fake_write_file", {"path": "b.txt", "content": "hi"}, "call-2"),
        ]
    )
    never_reached = AIMessage(content="should not run", tool_calls=[])
    model = FakeChatModel([call_response, never_reached])

    result = _run(
        model, [fake_write_file], [SystemMessage(content="hi")], ceilings=LoopCeilings(max_tool_calls=1)
    )

    assert result.stopped_by == "tool_calls"
    assert result.tool_call_count == 2
    # The loop never invoked the model again for a next turn.
    assert len(model.invocations) == 1


def test_run_tool_loop_preserves_usage_already_flushed_when_a_ceiling_stops_it() -> None:
    call_response = _ai_message(
        tool_calls=[_tool_call("fake_write_file", {"path": "a.txt", "content": "hi"}, "call-1")]
    )
    never_reached = AIMessage(content="should not run", tool_calls=[])
    model = FakeChatModel([call_response, never_reached])
    ledger = InMemoryUsageLedger()

    result = _run(
        model,
        [fake_write_file],
        [SystemMessage(content="hi")],
        ceilings=LoopCeilings(max_cost_usd=0.0),
        usage_ledger=ledger,
    )

    assert result.stopped_by == "cost"
    assert ledger.totals == result.usage
    assert result.usage.tokens > 0


def test_run_tool_loop_replaying_after_a_simulated_restart_does_not_double_count_usage() -> None:
    """`L3-IMP-8`, exercised at the loop's own boundary: a ledger already
    carrying usage from an earlier, interrupted run is handed to a fresh
    `run_tool_loop` call standing in for the restart. Its own new usage is
    additive, never a re-derivation that would double the earlier total."""
    ledger = InMemoryUsageLedger(
        UsageTotals(tokens=1000, effective_tokens=1000, cost_usd=1.0, tool_calls=2)
    )
    final_response = AIMessage(
        content="done",
        tool_calls=[],
        usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
    )
    model = FakeChatModel([final_response])

    result = _run(model, [fake_write_file], [SystemMessage(content="hi")], usage_ledger=ledger)

    assert result.usage.tokens == 1007
    assert result.usage.tool_calls == 2


# --- One scripted run demonstrating every mechanism together ---------------


def test_run_tool_loop_demonstrates_capping_and_compaction_together_in_one_scripted_run() -> None:
    """The acceptance demonstration this ticket asks for: one scripted run,
    long enough to force at least one compaction and produce at least one
    capped result, against a fake chat model — never a real paid run.

    Four turns each dump a large, tagged result (capped every time); a
    fifth turn reads a chosen slice back through `read_result_slice`, the
    tool a capped result's own pointer names; a sixth turn ends the loop.
    """

    @tool
    def dump(tag: str) -> str:
        """Return a lot of text tagged so a test can tell turns apart."""
        return f"marker-{tag}-" + ("content" * 500)

    store = InMemoryArtifactStore()
    cap_limit = 150
    # Calibrated from the shape `cap_tool_result` actually produces, so
    # roughly one capped turn's worth of history fits the budget and the
    # next turn forces an eviction.
    sample = cap_tool_result(
        "marker-0-" + ("content" * 500), tool_call_id="calc", limit=cap_limit, store=InMemoryArtifactStore()
    )
    compaction_threshold = estimate_tokens(sample) + 5

    responses = [
        _ai_message(tool_calls=[_tool_call("dump", {"tag": str(i)}, f"call-{i}")]) for i in range(4)
    ]
    responses.append(
        _ai_message(
            tool_calls=[_tool_call("read_result_slice", {"artifact_id": "call-1", "start": 0, "end": 20}, "call-slice")]
        )
    )
    responses.append(AIMessage(content="done", tool_calls=[]))
    model = FakeChatModel(responses)

    tools = [dump, build_read_result_slice_tool(store)]

    result = _run(
        model,
        tools,
        [SystemMessage(content="the pinned prefix")],
        result_cap_limit=cap_limit,
        result_store=store,
        compaction_threshold=compaction_threshold,
    )

    assert result.stopped_by is None
    assert result.tool_call_count == 5

    # At least one capped result: every dump result is over the cap.
    capped_messages = [
        m
        for m in result.conversation
        if isinstance(m, ToolMessage) and "[pointer]" in str(m.content)
    ]
    assert len(capped_messages) == 4

    # At least one compaction: the final request no longer carries the
    # earliest turn, though the Pinned Prefix still opens every request.
    last_request = model.invocations[-1]
    assert last_request[0] == SystemMessage(content="the pinned prefix")
    assert not any(
        isinstance(m, ToolMessage) and "marker-0-" in str(m.content) for m in last_request
    )

    # The read-slice tool actually returned a real slice of the full,
    # uncapped content the earlier capped result stashed away.
    slice_message = result.conversation[-2]
    assert isinstance(slice_message, ToolMessage)
    assert slice_message.content == store.read("call-1")[0:20]
    assert slice_message.content == "marker-1-contentcont"
