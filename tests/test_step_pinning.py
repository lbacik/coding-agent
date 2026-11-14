"""Full Effective Configuration pinning at Claim (contract §2.10, §10, §16).

Matrix row ids: CFG-41 (the Claim pins the full Effective Configuration —
the canonical compiled Workflow, file-level settings with worker defaults,
every operator prompt file's bytes, the text of every statically named
skill, the Skill Bundle's identity and digest, and the Pinned Model with its
price table, output cap and full context-window config — covered by one
SHA-256 digest carried in the provenance footer; recovery uses pinned values
only) and CFG-42 (an unfinished Attempt whose pinned record is missing,
unreadable or from an older release refuses startup, naming the Attempt).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from coding_agent.inputs.claim import ClaimConfig
from coding_agent.inputs.claim import sweep_and_claim
from coding_agent.inputs.port import LabeledEvent, TrackedInput
from coding_agent.ledger.store import RunLedger, digest_effective_configuration
from coding_agent.provider.config import (
    DEFAULT_CONTEXT_WINDOWS,
    DEFAULT_OUTPUT_CAPS,
    policy_for_pin,
)
from coding_agent.runtime.context_handoff import ContextWindowConfig
from coding_agent.runtime.step_publication import (
    render_comment_body,
    render_open_pull_request_body,
    render_review_body,
)
from coding_agent.worker.run import (
    PinnedConfigurationRefused,
    WorkerLoopConfig,
    recover_unfinished_step_attempts,
    run_worker_loop,
)
from coding_agent.workflow.effective import (
    build_effective_configuration,
    bundle_identity_from_spec,
    is_full_effective_configuration,
)
from coding_agent.workflow.v2_schema import (
    V2Agent,
    V2Ceiling,
    V2Input,
    V2RouteTarget,
    V2Step,
    V2Workflow,
)
from support.effective_config import (
    TEST_PIN,
    TEST_PRICES,
    full_effective_configuration,
)
from support.fake_step_tracker import FakeStepTracker
from support.scripted_model import ScriptedModel
from support.skill_bundle import write_skill_bundle

LABEL = "agent:implement"
EDITED_LABEL = "agent:edited"
BODY = "## Acceptance criteria\n- [ ] `greet` returns a greeting\n"


def _issue_input(number: int, created_at: str, label: str = LABEL) -> TrackedInput:
    return TrackedInput(
        number=number,
        input_type="issue",
        title=f"Add a greeting {number}",
        body=BODY,
        is_open=True,
        labels=(label, EDITED_LABEL),
        assignees=(),
        created_at=created_at,
        author_login="human",
        author_id=7,
    )


def _human_event(label: str = LABEL) -> LabeledEvent:
    return LabeledEvent(
        label=label,
        actor_login="human",
        actor_id=7,
        actor_type="User",
        created_at="2026-10-01T00:00:00Z",
    )


def _tracker_with(*numbers_and_dates: tuple[int, str]) -> FakeStepTracker:
    tracker = FakeStepTracker(agent_login="coding-agent", agent_id=1001)
    for number, created_at in numbers_and_dates:
        tracker.inputs[number] = _issue_input(number, created_at)
        tracker.labeled_events[(number, LABEL)] = _human_event(LABEL)
        tracker.labeled_events[(number, EDITED_LABEL)] = _human_event(EDITED_LABEL)
    return tracker


def _workflow(
    workflow_id: str = "implement",
    label: str = LABEL,
    end_labels: tuple[str, ...] = ("pinned-done",),
) -> V2Workflow:
    return V2Workflow(
        id=workflow_id,
        model="sonnet",
        input=V2Input(type="issue", label=label),
        ceiling=V2Ceiling(cost_usd=20, tokens=1000, tool_calls=10, wall_clock_minutes=5),
        outcomes={"completed": end_labels},
        start="work",
        defaults_routes={
            "error": V2RouteTarget(end="failed"),
            "limit-exceeded": V2RouteTarget(end="unfinished"),
        },
        steps={
            "work": V2Step(
                id="work",
                agent=V2Agent(results={"done": {}}),
                max_executions=1,
                routes={"done": V2RouteTarget(end="completed")},
            ),
        },
    )


def _claim_config(workflow: V2Workflow) -> ClaimConfig:
    return ClaimConfig(
        workflow_id=workflow.id,
        input_type="issue",
        selection_label=workflow.input.label,
        base_branch="main",
        agent_login="coding-agent",
        agent_id=1001,
        effective_configuration=full_effective_configuration(workflow),
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[RunLedger]:
    handle = RunLedger(tmp_path / "state" / "run-ledger.sqlite3")
    yield handle
    handle.close()


# --- CFG-41: the direct pin's record ------------------------------------------


def test_cfg41_direct_pin_record_has_prices_cap_and_full_window() -> None:
    """CFG-41 (§2.10): a direct pin records its prices, output cap and the
    full context-window config."""
    record = full_effective_configuration(_workflow())
    assert is_full_effective_configuration(record) is True
    pinned = record["pinned_model"]
    assert isinstance(pinned, dict)
    assert pinned["key"] == TEST_PIN.key
    assert pinned["gateway_kind"] is None
    assert pinned["prices"] == {
        "input": TEST_PRICES.input,
        "output": TEST_PRICES.output,
        "cache_read": TEST_PRICES.cache_read,
        "cache_write": TEST_PRICES.cache_write,
    }
    assert pinned["output_cap_tokens"] == DEFAULT_OUTPUT_CAPS[TEST_PIN.key]
    expected = DEFAULT_CONTEXT_WINDOWS[TEST_PIN.key]
    assert pinned["context_threshold_tokens"] == expected.threshold_tokens
    assert pinned["context_window"] == {
        "threshold_tokens": expected.threshold_tokens,
        "safety_margin_tokens": expected.safety_margin_tokens,
        "request_overhead_tokens": expected.request_overhead_tokens,
        "handoff_token_cap": expected.handoff_token_cap,
    }


def test_cfg41_patched_output_cap_moves_the_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CFG-41 (§2.10, §11.4): a code-owned output-cap change moves the one
    Effective Configuration digest."""
    before = digest_effective_configuration(full_effective_configuration(_workflow()))
    monkeypatch.setitem(DEFAULT_OUTPUT_CAPS, TEST_PIN.key, 12345)
    after = digest_effective_configuration(full_effective_configuration(_workflow()))
    assert before != after


def test_cfg41_patched_context_window_moves_the_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CFG-41 (§2.10): a code-owned context-window change moves the digest."""
    before = digest_effective_configuration(full_effective_configuration(_workflow()))
    monkeypatch.setitem(
        DEFAULT_CONTEXT_WINDOWS,
        TEST_PIN.key,
        ContextWindowConfig(
            threshold_tokens=77_000,
            safety_margin_tokens=1_000,
            request_overhead_tokens=1_000,
            handoff_token_cap=2_000,
        ),
    )
    after = digest_effective_configuration(full_effective_configuration(_workflow()))
    assert before != after


def test_cfg41_gateway_pin_record_has_identity_and_prices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CFG-41 (§2.10): a gateway pin keeps its full identity and gains its
    prices."""
    from coding_agent.worker import supervisor
    from coding_agent.workflow.v2_validate import validate_v2_file

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("LITELLM_PROXY_API_KEY", raising=False)
    from test_worker_run import _GATEWAY_RUN_CONFIG, _write_run_config

    config = _write_run_config(tmp_path, _GATEWAY_RUN_CONFIG)
    cfg, faults = validate_v2_file(config)
    assert not faults
    assert cfg is not None
    workflow = cfg.workflows["implement"]
    pin, prices = supervisor.configured_pin(cfg.models[workflow.model])
    record = build_effective_configuration(
        config=cfg,
        workflow=workflow,
        prompt_texts={},
        skill_texts={},
        bundle=bundle_identity_from_spec(),
        pin=pin,
        prices=prices,
        policy=policy_for_pin(pin),
        api_key_env="LITELLM_PROXY_API_KEY",
    )
    assert is_full_effective_configuration(record) is True
    pinned = record["pinned_model"]
    assert isinstance(pinned, dict)
    assert pinned["key"] == "litellm:muse-spark-1.3-contributor@responses"
    assert pinned["gateway_kind"] == "litellm"
    assert pinned["gateway_url"] == "http://pi.lan:4000"
    assert pinned["output_cap_tokens"] == 32000
    assert pinned["context_threshold_tokens"] == 100000
    assert pinned["prices"] == {
        "input": 2.00,
        "output": 10.00,
        "cache_read": 0.20,
        "cache_write": 2.50,
    }


def test_cfg41_no_environment_value_appears_in_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CFG-41 (§2.10): environment-variable names may appear; values never
    do."""
    secret = "sk-ant-test-secret-value"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    record = full_effective_configuration(_workflow(), api_key_env="ANTHROPIC_API_KEY")
    raw = json.dumps(record)
    assert secret not in raw
    pinned = record["pinned_model"]
    assert isinstance(pinned, dict)
    assert pinned["api_key_env"] == "ANTHROPIC_API_KEY"


# --- CFG-41: recovery uses pinned values only ---------------------------------


def test_cfg41_restart_uses_pinned_labels_after_an_edit(ledger: RunLedger) -> None:
    """CFG-41 (§2.10): after an operator edit, recovery still finishes the
    Attempt with the pinned selection label and end labels, leaving the
    pinned record and its digest unchanged."""
    workflow = _workflow()
    record = full_effective_configuration(workflow)
    ledger.open_attempt("implement#7/1", effective_configuration=record)
    ledger.set_attempt_state("implement#7/1", "claim-confirmed")
    from coding_agent.runtime.step_finalisation import terminal_writes_for_outcome

    ledger.record_terminal_outcome(
        "implement#7/1",
        outcome="completed",
        reason="verified-completion",
        terminal_comment_body="verified completion",
        writes=list(terminal_writes_for_outcome()),
    )
    digest_before = ledger.get_attempt("implement#7/1").effective_configuration_digest
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))

    edited = _workflow(label=EDITED_LABEL, end_labels=("edited-done",))
    summary = recover_unfinished_step_attempts(
        tracker=tracker, ledger=ledger, workflow=edited
    )

    assert summary.settled == ("implement#7/1",)
    stored = ledger.get_attempt("implement#7/1")
    assert stored.effective_configuration == record
    assert stored.effective_configuration_digest == digest_before
    remove_calls = [
        call for call in tracker.calls if call[0] == "remove_label"
    ]
    assert ("remove_label", (7, "issue", LABEL)) in remove_calls
    assert ("remove_label", (7, "issue", EDITED_LABEL)) not in remove_calls
    add_calls = [call for call in tracker.calls if call[0] == "add_labels"]
    assert add_calls, "recovery applies the pinned end labels"
    assert any("pinned-done" in str(call) for call in add_calls)
    assert not any("edited-done" in str(call) for call in add_calls)


def test_cfg41_open_attempt_uses_pinned_selection_label_after_an_edit(
    ledger: RunLedger,
) -> None:
    """CFG-41 (§2.10): an unconfirmed Claim released at restart resolves the
    input through the pinned record, not the edited file."""
    workflow = _workflow()
    ledger.open_attempt(
        "implement#7/1",
        effective_configuration=full_effective_configuration(workflow),
    )
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))

    edited = _workflow(label=EDITED_LABEL, end_labels=("edited-done",))
    summary = recover_unfinished_step_attempts(
        tracker=tracker, ledger=ledger, workflow=edited
    )

    assert summary.settled == ("implement#7/1",)
    assert ledger.get_attempt("implement#7/1").state == "released"
    from coding_agent.inputs.claim import ConfirmedClaim

    claimed = sweep_and_claim(tracker, ledger, _claim_config(workflow))
    assert isinstance(claimed, ConfirmedClaim)
    assert (claimed.attempt_id, claimed.n) == ("implement#7/2", 2)


# --- CFG-41: the provenance footer carries the Attempt's digest ---------------


def test_cfg41_publication_bodies_carry_the_attempt_digest(ledger: RunLedger) -> None:
    """CFG-41 (§10): every publication body carries the Attempt's own ledger
    Effective Configuration digest."""
    workflow = _workflow()
    ledger.open_attempt(
        "implement#7/1",
        effective_configuration=full_effective_configuration(workflow),
    )
    digest = ledger.get_attempt("implement#7/1").effective_configuration_digest

    pr_body = render_open_pull_request_body(
        document="doc",
        artifact_ref="artifact",
        attempt_id="implement#7/1",
        workflow_id="implement",
        step_id="work",
        config_digest=digest,
        issue_number=7,
        draft=False,
        base_revision="abc123",
        effect_marker="marker",
    )
    review_body = render_review_body(
        document="doc",
        artifact_ref="artifact",
        attempt_id="implement#7/1",
        workflow_id="implement",
        step_id="work",
        config_digest=digest,
        input_head="def456",
        effect_marker="marker",
    )
    comment_body = render_comment_body(
        document="doc",
        artifact_ref="artifact",
        attempt_id="implement#7/1",
        workflow_id="implement",
        step_id="work",
        config_digest=digest,
        effect_marker="marker",
    )
    assert f"Configuration: {digest}" in pr_body
    assert f"Configuration: {digest}" in review_body
    assert f"Configuration: {digest}" in comment_body


def test_cfg41_two_workflows_in_one_file_get_different_digests() -> None:
    """CFG-41 (§10): two Workflows from the same file get different
    digests."""
    first = digest_effective_configuration(
        full_effective_configuration(_workflow("implement"))
    )
    second = digest_effective_configuration(
        full_effective_configuration(_workflow("review"))
    )
    assert first != second


# --- CFG-42: old-shape records refuse startup ---------------------------------


def test_cfg42_old_shape_record_refuses_recovery(ledger: RunLedger) -> None:
    """CFG-42 (§16.4): an Attempt opened by a build with the minimal record
    refuses recovery, naming the Attempt."""
    ledger.open_attempt("implement#7/1", effective_configuration={"workflow": "x"})
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))

    with pytest.raises(PinnedConfigurationRefused, match="implement#7/1"):
        recover_unfinished_step_attempts(
            tracker=tracker, ledger=ledger, workflow=_workflow()
        )

    assert ledger.get_attempt("implement#7/1").state == "open"


def test_cfg42_missing_shape_record_refuses_recovery(ledger: RunLedger) -> None:
    """CFG-42 (§16.4): a missing-shape record refuses recovery, naming the
    Attempt."""
    ledger.open_attempt("implement#7/1", effective_configuration={})
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))

    with pytest.raises(PinnedConfigurationRefused, match="implement#7/1"):
        recover_unfinished_step_attempts(
            tracker=tracker, ledger=ledger, workflow=_workflow()
        )


def test_cfg42_open_state_old_shape_refuses_recovery(ledger: RunLedger) -> None:
    """CFG-42 (§16.4): the refusal covers ``open`` (unconfirmed-Claim)
    Attempts too."""
    ledger.open_attempt("implement#7/1", effective_configuration={"workflow": "x"})
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))

    with pytest.raises(PinnedConfigurationRefused, match="implement#7/1"):
        recover_unfinished_step_attempts(
            tracker=tracker, ledger=ledger, workflow=_workflow()
        )

    assert tracker.calls == []


def test_cfg42_refusal_halts_the_poll_loop_without_a_new_attempt(
    ledger: RunLedger,
) -> None:
    """CFG-42 (§16.4): through the poll loop the refusal halts non-zero with
    ``recovery-refused`` and opens no new Attempt."""
    ledger.open_attempt("implement#7/1", effective_configuration={"workflow": "x"})
    tracker = _tracker_with((7, "2026-10-01T00:00:00Z"))
    workflow = _workflow()

    def _never_run(claimed: Any) -> Any:
        raise AssertionError(f"no Attempt should run (got {claimed})")

    exit_code = run_worker_loop(
        tracker=tracker,
        ledger=ledger,
        loop_config=WorkerLoopConfig(
            claim_config=_claim_config(workflow),
            workflow=workflow,
            poll_interval_seconds=60.0,
        ),
        run_one_attempt=_never_run,
        sleep=lambda seconds: None,
        max_sweeps=5,
    )

    assert exit_code == 1
    assert tracker.calls == []
    assert ledger.get_attempt("implement#7/1").state == "open"
    assert ledger.has_unfinished_attempt() is True
    import sqlite3 as _sqlite3

    connection = _sqlite3.connect(str(ledger.path))
    try:
        row = connection.execute(
            "SELECT reason FROM operator_halt WHERE id = 1"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None and str(row[0]) == "recovery-refused"


def test_cfg42_supervisor_refuses_an_old_shape_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CFG-42 (§16.4): ``supervisor.run_worker`` refuses an old-shape
    unfinished Attempt, naming it, and opens no new Attempt."""
    import sqlite3 as _sqlite3

    from coding_agent.worker import supervisor
    from support.scripted_model import ScriptedModel
    from test_worker_recovery import (
        _write_run_config as _write_recovery_config,
    )
    from test_worker_recovery import (
        _write_run_matrix as _write_recovery_matrix,
    )

    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_testtoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    config = _write_recovery_config(tmp_path)
    matrix = _write_recovery_matrix(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    seed = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        seed.open_attempt(
            "implement#7/1", effective_configuration={"workflow": "x"}
        )
    finally:
        seed.close()
    tracker = FakeStepTracker(agent_login="coding-agent", agent_id=1001)

    def factory(**kwargs: object) -> FakeStepTracker:
        _ = kwargs
        return tracker

    from coding_agent.skillbundle.verify import VerificationReport

    report = VerificationReport()
    report.add("bundle", True, "verified")
    monkeypatch.setattr(
        supervisor, "verify_installed_skill_bundle", lambda check: report
    )
    from coding_agent.identity.startup import Identity, StartupCheckResult

    monkeypatch.setattr(
        supervisor,
        "run_startup_checks",
        lambda *args, **kwargs: StartupCheckResult(
            identity=Identity(account_id=999, login="coding-agent"),
            push_allowed=True,
            rate_limit_remaining=4999,
            rate_limit_limit=5000,
        ),
    )
    monkeypatch.setattr(
        supervisor, "build_chat_model", lambda pin, **kwargs: ScriptedModel([])
    )
    monkeypatch.setattr(
        supervisor, "assert_provider_capability", lambda *args, **kwargs: None
    )

    write_skill_bundle(tmp_path)
    exit_code = supervisor.run_worker(
        config=config,
        workflow="implement",
        state_dir=state_dir,
        skills_home=tmp_path,
        skill_bundle=supervisor.SkillBundleCheck(
            install_report=tmp_path / "install.json",
            list_report=tmp_path / "list.json",
            home=tmp_path,
            ignore_file=tmp_path / "ignore.txt",
        ),
        toolchain_matrix_path=matrix,
        tracker_factory=factory,
        sleep=lambda seconds: None,
        max_sweeps=1,
    )

    assert exit_code == 1
    # The refusal lands after the tracker exists (the repository-owner read
    # precedes recovery), but no Claim write ever happens.
    assert tracker.calls == [("fetch_repository_owner_id", ())]
    connection = _sqlite3.connect(state_dir / "run-ledger.sqlite3")
    try:
        count = connection.execute("SELECT COUNT(*) FROM attempts").fetchone()
    finally:
        connection.close()
    assert count is not None and int(count[0]) == 1
