"""The Context-Window Handoff at the Attempt seam (contract §11, ADR 0013).

Contract: ``docs/contract/mvp-runtime-contract.md`` §11 ("Context-Window
Handoff") and [ADR 0013](../docs/adr/0013-context-window-handoff.md): before
a request would exceed the Pinned Model's usable threshold the work node
stops with no over-threshold call and no split Exchange Unit; one bounded
handoff node (one model invocation, no tools, its own output cap) produces a
versioned redacted Continuation Snapshot capped at 32 KiB after redaction,
persisted by SHA-256 digest, recorded in the Run Ledger and charged to the
Attempt-wide meters; the fresh node reopens with the intact Pinned Prefix,
the snapshot in place of history and the same forced activation, both
work-node budgets restored and Attempt-wide meters unbroken. A malformed,
incomplete, over-limit, unpersistable or unverifiable snapshot — or a fresh
opening that cannot fit — ends the Attempt ``failed`` / ``handoff-failure``
with a redacted failure artifact and no continuation.

MVP row ids: L3-IMP-4, L3-IMP-12, L3-IMP-21, L3-IMP-23, L3-IMP-31, C-CTX-1.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from coding_agent.ledger.store import LedgerRecord, RunLedger
from coding_agent.runtime.context_handoff import (
    CONTINUATION_SNAPSHOT_VERSION,
    SNAPSHOT_SIZE_LIMIT_BYTES,
    ContinuationSnapshot,
    ContextWindowConfig,
    HandoffFailure,
    handoff_trace,
)
from coding_agent.runtime.graph.attempt import AttemptResult, run_attempt
from coding_agent.runtime.result_capping import ArtifactStore, InMemoryArtifactStore
from conftest import init_origin_repo, run_git
from support.fake_tracker import FakeTracker
from support.scripted_model import ScriptedModel

_PROFILE_YAML = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "true"
  test_targeted: "true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""

# A roomy usable threshold: the scripted crossings below pad one assistant
# turn so the crossing request is far above every fitting request, and every
# fresh node fits far below. All estimates are deterministic, so the window
# never moves between runs.
ROOMY_THRESHOLD = 1500

# One silent assistant turn padded to simulate a filling context (contract
# §11: the estimate grows with whole Exchange Units, never a split one).
PAD = "p" * 4000


def _commit_step_profile(src: Path) -> None:
    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(_PROFILE_YAML, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=src, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add project profile"], cwd=src, check=True)


def _call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ai(
    *,
    content: str = "",
    calls: list[dict[str, object]] | None = None,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=list(calls or []),
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    )


def _activate(call_id: str = "call-act") -> AIMessage:
    return _ai(calls=[_call("activate_implement", {}, call_id)])


def _report(call_id: str, test_id: str = "pkg::phantom") -> AIMessage:
    return _ai(calls=[_call("report_completion", {"AC-1": [test_id]}, call_id)])


def _snapshot_json(
    *,
    next_action: str = "run the targeted test, then report completion",
) -> str:
    """One minimal valid Continuation Snapshot for attempt ``#7/1``."""
    payload: dict[str, object] = {
        "version": 1,
        "attempt_id": "#7/1",
        "base_revision": "base-rev-abc",
        "branch": "agent/issue-7/attempt-1",
        "commit": "head-sha-def",
        "changed_files": ["src/greet.py"],
        "commands": [
            {
                "command": "test_targeted tests/test_greet.py::test_greet",
                "result": "passed",
            }
        ],
        "validation_evidence": [
            {"test_id": "tests/test_greet.py::test_greet", "passed": True}
        ],
        "unresolved_questions": ["which greeting wording?"],
        "next_intended_action": next_action,
    }
    return json.dumps(payload)


def _snapshot_response(
    *, next_action: str = "run the targeted test, then report completion"
) -> AIMessage:
    return _ai(
        content=_snapshot_json(next_action=next_action),
        input_tokens=100,
        output_tokens=50,
    )


@dataclass
class _Handles:
    result: AttemptResult
    model: ScriptedModel
    ledger: RunLedger
    store: ArtifactStore


@contextmanager
def _attempt(
    tmp_path: Path,
    skills_dir: Path,
    outcomes: list[AIMessage],
    *,
    threshold: int = ROOMY_THRESHOLD,
    handoff_cap: int = 2000,
    store: ArtifactStore | None = None,
    redactions: dict[str, str] | None = None,
) -> Iterator[_Handles]:
    """Run one Attempt with a scripted model over a real local clone."""
    tracker = FakeTracker()
    model = ScriptedModel(list(outcomes))
    artifact_store: ArtifactStore = (
        store if store is not None else InMemoryArtifactStore()
    )
    ledger = RunLedger(tmp_path / "ledger.sqlite3")
    try:
        src = tmp_path / "origin-src"
        init_origin_repo(src)
        _commit_step_profile(src)
        bare = tmp_path / "remote.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(src), str(bare)], check=True
        )
        workdir = tmp_path / "workspace"
        run_git(["clone", "-q", str(bare), str(workdir)], cwd=tmp_path)
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=tracker,
            ledger=ledger,
            model=model,
            workspace=workdir,
            effective_configuration={"repo": "x/y", "issue": 7},
            skills_dir=skills_dir,
            context_window=ContextWindowConfig(
                threshold_tokens=threshold,
                safety_margin_tokens=0,
                request_overhead_tokens=0,
                handoff_token_cap=handoff_cap,
            ),
            artifact_store=artifact_store,
            redactions=redactions,
        )
        yield _Handles(
            result=result, model=model, ledger=ledger, store=artifact_store
        )
    finally:
        ledger.close()


def _wire_text(message: object) -> str:
    """Message content with Anthropic cache breakpoints stripped.

    The runtime tags the outgoing request copy with ephemeral
    ``cache_control`` breakpoints at the Pinned Prefix end and the tail end
    (contract §11); the stored conversation keeps exactly what the provider
    produced. The fresh node reopens with a longer prefix, so its breakpoint
    sits at a different position than the first node's. Comparing wire text
    proves the intact Pinned Prefix without coupling to breakpoint
    placement.
    """
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


def _handoff_invocation_indexes(model: ScriptedModel) -> list[int]:
    """Invocation indexes whose single message is the handoff request."""
    indexes: list[int] = []
    for index, invocation in enumerate(model.invocations):
        if len(invocation) != 1:
            continue
        content = invocation[0].content
        if (
            isinstance(invocation[0], HumanMessage)
            and isinstance(content, str)
            and "Produce exactly one Continuation Snapshot" in content
        ):
            indexes.append(index)
    return indexes


class _SnapshotFailingStore(InMemoryArtifactStore):
    """Refuses snapshot persists but keeps failure artifacts (L3-IMP-21)."""

    def store(self, artifact_id: str, content: str) -> None:
        if "handoff-failure" not in artifact_id:
            raise OSError("simulated snapshot persist failure")
        super().store(artifact_id, content)


class _TamperingStore(InMemoryArtifactStore):
    """Corrupts reads so digest verification fails (L3-IMP-21)."""

    def read(self, artifact_id: str) -> str:
        return super().read(artifact_id) + "tampered"


def _failure_records(handles: _Handles) -> list[LedgerRecord]:
    return [
        record
        for record in handles.ledger.records("#7/1")
        if record.kind == "handoff_failure"
    ]


def test_successful_handoff_reopens_fresh_node(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L3-IMP-4: one bounded handoff, one persisted redacted versioned snapshot.

    The crossing request makes no model call; the handoff node makes exactly
    one invocation with no tools; the fresh node's first request holds only
    the intact Pinned Prefix, the snapshot and the same forced activation.
    Assistant turns stay verbatim within each node.
    """
    r1 = _activate("call-act-1")
    r2 = _ai(content=PAD)
    handoff = _snapshot_response()
    r4 = _activate("call-act-2")
    r5 = _report("call-rep-1")
    r6 = _report("call-rep-2")
    r7 = _report("call-rep-3")
    with _attempt(
        tmp_path, skills_dir, [r1, r2, handoff, r4, r5, r6, r7]
    ) as handles:
        assert handles.result.outcome == "failed"
        assert handles.result.reason == "claims-unverified"
        # Exactly one bounded handoff invocation: the scripted queue is
        # consumed exactly, so no extra model call happened anywhere.
        handoffs = _handoff_invocation_indexes(handles.model)
        assert handoffs == [2]
        assert len(handles.model.invocations) == 7
        # No tools are bound for the handoff node: one forced and one
        # unforced binding per work node, nothing in between.
        assert handles.model.tool_choices == [
            "activate_implement",
            None,
            "activate_implement",
            None,
        ]
        # One persisted, versioned, redacted snapshot with its digest recorded.
        handoff_records = [
            record
            for record in handles.ledger.records("#7/1")
            if record.kind == "handoff"
        ]
        assert len(handoff_records) == 1
        payload = handoff_records[0].payload
        assert payload["snapshot_version"] == CONTINUATION_SNAPSHOT_VERSION
        digest = str(payload["snapshot_digest"])
        artifact_ref = handoff_records[0].artifact_ref
        assert artifact_ref is not None and digest in artifact_ref
        stored = handles.store.read(artifact_ref)
        assert ContinuationSnapshot.from_json(stored).digest == digest
        # The fresh node's first request holds only the intact Pinned
        # Prefix, the snapshot and the same forced activation.
        first_opening = handles.model.invocations[0]
        fresh_opening = handles.model.invocations[3]
        assert len(fresh_opening) == 3
        assert [_wire_text(message) for message in fresh_opening[:2]] == [
            _wire_text(message) for message in first_opening[:2]
        ]
        assert isinstance(fresh_opening[2], HumanMessage)
        # The embedded snapshot is the recorded one: its digest matches the
        # ledger entry (wire text strips the tail cache breakpoint first).
        embedded_text = _wire_text(fresh_opening[2])
        assert embedded_text.startswith("# Continuation Snapshot")
        embedded = ContinuationSnapshot.from_json(
            embedded_text.split("\n\n", 1)[1]
        )
        assert embedded.digest == digest
        # Assistant turns stay verbatim within each node: the second request
        # of each node replays the opening plus the node's first assistant
        # turn and its result (only the prefix-end and tail messages carry
        # cache-breakpoint copies).
        assert handles.model.invocations[1][2] is r1
        assert handles.model.invocations[4][3] is r4


def test_handoff_moves_no_exchange_unit(tmp_path: Path, skills_dir: Path) -> None:
    """L3-IMP-12: no unit is split; the handoff gets facts, the fresh node none."""
    marker = "unit-marker-7f3a9c"
    r1 = _ai(
        content=marker,
        calls=[_call("activate_implement", {}, "call-act-1")],
    )
    r2 = _ai(content=PAD)
    handoff = _snapshot_response()
    r4 = _activate("call-act-2")
    r5 = _report("call-rep-1")
    r6 = _report("call-rep-2")
    r7 = _report("call-rep-3")
    with _attempt(
        tmp_path, skills_dir, [r1, r2, handoff, r4, r5, r6, r7]
    ) as handles:
        assert handles.result.reason == "claims-unverified"
        (handoff_index,) = _handoff_invocation_indexes(handles.model)
        prompt = str(handles.model.invocations[handoff_index][0].content)
        # Explicit repository facts travel; no old assistant turn or tool
        # result does — the old unit's marker is absent from the prompt.
        assert "#7/1" in prompt
        assert "next_intended_action_hint" in prompt
        assert marker not in prompt
        assert PAD not in prompt
        # The fresh node receives no old unit at all: prefix plus snapshot,
        # with no assistant or tool message from the previous node.
        fresh_opening = handles.model.invocations[handoff_index + 1]
        assert len(fresh_opening) == 3
        for message in fresh_opening:
            assert not isinstance(message, AIMessage)
            assert not isinstance(message, ToolMessage)
        assert marker not in json.dumps(
            [str(message.content) for message in fresh_opening]
        )


def test_handoff_trace_reports_attempt_usage(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L3-IMP-23: the Attempt exposes count, digest, version, estimate, caps, usage."""
    r1 = _activate("call-act-1")
    r2 = _ai(content=PAD, input_tokens=20, output_tokens=7)
    handoff = _snapshot_response()
    r4 = _activate("call-act-2")
    r5 = _report("call-rep-1")
    r6 = _report("call-rep-2")
    r7 = _report("call-rep-3")
    with _attempt(
        tmp_path, skills_dir, [r1, r2, handoff, r4, r5, r6, r7]
    ) as handles:
        assert handles.result.reason == "claims-unverified"
        trace = handoff_trace(handles.ledger, "#7/1")
        assert trace.count == 1
        (entry,) = trace.handoffs
        records = [
            record
            for record in handles.ledger.records("#7/1")
            if record.kind == "handoff"
        ]
        assert len(records) == 1
        assert entry.snapshot_digest == str(records[0].payload["snapshot_digest"])
        assert (
            entry.snapshot_version == CONTINUATION_SNAPSHOT_VERSION
        )
        assert entry.artifact_ref == records[0].artifact_ref
        assert entry.context_estimate_tokens > 0
        assert entry.node_cap_tokens == 2000
        assert entry.provider_input_tokens == 100
        assert entry.provider_output_tokens == 50
        # Work-node usage (15 + 27) plus the handoff usage (150), and the
        # single tool call the first node issued before the crossing.
        assert entry.cumulative_tokens == 15 + 27 + 150
        assert entry.cumulative_effective_tokens == 15 + 27 + 150
        assert entry.cumulative_cost_usd == 0.0
        assert entry.cumulative_tool_calls == 1


def test_handoff_restores_both_work_node_budgets(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L3-IMP-31: one continuation plus one return, then a fresh node with two of each."""
    r1 = _activate("call-act-1")
    r2 = _report("call-rep-1")
    r3 = _ai(content=PAD)
    handoff = _snapshot_response()
    r5 = _activate("call-act-2")
    r6 = _ai(content="quiet once")
    r7 = _ai(content="quiet twice")
    r8 = _report("call-rep-2")
    r9 = _report("call-rep-3")
    r10 = _report("call-rep-4")
    # Threshold 1800 keeps exactly one handoff: the first node's crossing
    # request estimates ~2175 tokens (the padded turn), while the fresh
    # node's longest request estimates ~1573, so the fresh node demonstrates
    # the full two continuations plus two returns without re-crossing.
    with _attempt(
        tmp_path,
        skills_dir,
        [r1, r2, r3, handoff, r5, r6, r7, r8, r9, r10],
        threshold=1800,
    ) as handles:
        # The fresh node spent two continuations and two returns before the
        # third report force-accepted: every queued response was consumed, so
        # neither budget carried over from the first node.
        assert handles.result.outcome == "failed"
        assert handles.result.reason == "claims-unverified"
        assert _handoff_invocation_indexes(handles.model) == [3]
        assert len(handles.model.invocations) == 10
        # The snapshot carries work facts, never runtime policy state.
        records = [
            record
            for record in handles.ledger.records("#7/1")
            if record.kind == "handoff"
        ]
        assert len(records) == 1
        assert records[0].artifact_ref is not None
        stored = json.loads(handles.store.read(records[0].artifact_ref or ""))
        assert set(stored.keys()) == {
            "version",
            "attempt_id",
            "base_revision",
            "branch",
            "commit",
            "changed_files",
            "commands",
            "validation_evidence",
            "unresolved_questions",
            "next_intended_action",
        }
        # Attempt-wide meters ran on unbroken: both pre-handoff tool calls
        # are in the cumulative count recorded at the handoff.
        trace = handoff_trace(handles.ledger, "#7/1")
        assert trace.count == 1
        assert trace.handoffs[0].cumulative_tool_calls == 2


def test_malformed_handoff_response_fails_closed(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L3-IMP-21 (malformed): not JSON ends failed/handoff-failure, redacted, no continuation."""
    secret = "s3cr3t-malformed-xyz"
    r1 = _activate("call-act-1")
    r2 = _ai(content=PAD)
    bad = _ai(content="not json at all {" + secret)
    with _attempt(
        tmp_path,
        skills_dir,
        [r1, r2, bad],
        redactions={"api_key": secret},
    ) as handles:
        assert handles.result.outcome == "failed"
        assert "handoff-failure" in handles.result.reason
        assert len(handles.model.invocations) == 3
        failures = _failure_records(handles)
        assert len(failures) == 1
        assert failures[0].artifact_ref is not None
        artifact = json.loads(handles.store.read(failures[0].artifact_ref or ""))
        assert artifact["attempt_id"] == "#7/1"
        assert secret not in json.dumps(artifact, ensure_ascii=False)
        assert "«redacted:api_key»" in artifact["response_excerpt"]
        assert handoff_trace(handles.ledger, "#7/1").count == 0


def test_incomplete_handoff_snapshot_fails_closed(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L3-IMP-21 (incomplete): valid JSON with missing fields, no continuation."""
    r1 = _activate("call-act-1")
    r2 = _ai(content=PAD)
    bad = _ai(content=json.dumps({"version": 1}))
    with _attempt(tmp_path, skills_dir, [r1, r2, bad]) as handles:
        assert handles.result.outcome == "failed"
        assert "handoff-failure" in handles.result.reason
        assert "missing required fields" in handles.result.reason
        assert len(handles.model.invocations) == 3
        failures = _failure_records(handles)
        assert len(failures) == 1
        assert failures[0].artifact_ref is not None
        assert handoff_trace(handles.ledger, "#7/1").count == 0


def test_over_limit_handoff_snapshot_fails_closed(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L3-IMP-21 (over-limit) and C-CTX-1: past 32 KiB after redaction is refused."""
    r1 = _activate("call-act-1")
    r2 = _ai(content=PAD)
    big = _snapshot_response(next_action="y" * (SNAPSHOT_SIZE_LIMIT_BYTES + 1))
    with _attempt(
        tmp_path,
        skills_dir,
        [r1, r2, big],
        handoff_cap=20000,
    ) as handles:
        assert handles.result.outcome == "failed"
        assert "handoff-failure" in handles.result.reason
        assert "32 KiB" in handles.result.reason
        assert len(handles.model.invocations) == 3
        failures = _failure_records(handles)
        assert len(failures) == 1
        assert failures[0].artifact_ref is not None
        assert handoff_trace(handles.ledger, "#7/1").count == 0


def test_snapshot_over_limit_refused_when_composed() -> None:
    """C-CTX-1 as a pure seam: composing past 32 KiB after redaction raises."""
    payload: dict[str, object] = json.loads(
        _snapshot_json(next_action="y" * (SNAPSHOT_SIZE_LIMIT_BYTES + 1))
    )
    try:
        ContinuationSnapshot.from_payload(payload, redactions={})
    except HandoffFailure as exc:
        assert "32 KiB" in str(exc)
    else:
        raise AssertionError("over-limit snapshot composed without refusal")


def test_redaction_applies_before_size_cap() -> None:
    """C-CTX-1 ordering: a huge secret that redacts down composes fine."""
    secret = "z" * (SNAPSHOT_SIZE_LIMIT_BYTES + 1)
    payload: dict[str, object] = json.loads(_snapshot_json(next_action=secret))
    snapshot = ContinuationSnapshot.from_payload(
        payload, redactions={"api_key": secret}
    )
    assert secret not in snapshot.to_json()
    assert "«redacted:api_key»" in snapshot.to_json()


def test_unpersistable_snapshot_fails_closed(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L3-IMP-21 (unpersistable): the snapshot cannot be stored, failure artifact kept."""
    r1 = _activate("call-act-1")
    r2 = _ai(content=PAD)
    handoff = _snapshot_response()
    with _attempt(
        tmp_path,
        skills_dir,
        [r1, r2, handoff],
        store=_SnapshotFailingStore(),
    ) as handles:
        assert handles.result.outcome == "failed"
        assert "handoff-failure" in handles.result.reason
        assert len(handles.model.invocations) == 3
        failures = _failure_records(handles)
        assert len(failures) == 1
        assert failures[0].artifact_ref is not None
        artifact = json.loads(handles.store.read(failures[0].artifact_ref or ""))
        assert artifact["attempt_id"] == "#7/1"
        assert handoff_trace(handles.ledger, "#7/1").count == 0


def test_unverifiable_snapshot_fails_closed(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L3-IMP-21 (unverifiable): a tampered artifact fails digest verification."""
    r1 = _activate("call-act-1")
    r2 = _ai(content=PAD)
    handoff = _snapshot_response()
    with _attempt(
        tmp_path, skills_dir, [r1, r2, handoff], store=_TamperingStore()
    ) as handles:
        assert handles.result.outcome == "failed"
        assert "handoff-failure" in handles.result.reason
        assert len(handles.model.invocations) == 3
        failures = _failure_records(handles)
        assert len(failures) == 1
        assert failures[0].artifact_ref is not None
        assert handoff_trace(handles.ledger, "#7/1").count == 0


def test_persisted_snapshot_is_redacted(
    tmp_path: Path, skills_dir: Path
) -> None:
    """L3-IMP-4 (redaction): the persisted snapshot carries the marker, not the secret."""
    secret = "s3cr3t-handoff-abc"
    r1 = _activate("call-act-1")
    r2 = _ai(content=PAD)
    handoff = _snapshot_response(next_action=f"rotate {secret} then proceed")
    r4 = _activate("call-act-2")
    r5 = _report("call-rep-1")
    r6 = _report("call-rep-2")
    r7 = _report("call-rep-3")
    with _attempt(
        tmp_path,
        skills_dir,
        [r1, r2, handoff, r4, r5, r6, r7],
        redactions={"api_key": secret},
    ) as handles:
        assert handles.result.reason == "claims-unverified"
        records = [
            record
            for record in handles.ledger.records("#7/1")
            if record.kind == "handoff"
        ]
        assert len(records) == 1
        assert records[0].artifact_ref is not None
        stored = handles.store.read(records[0].artifact_ref or "")
        assert secret not in stored
        assert "«redacted:api_key»" in stored
