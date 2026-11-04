"""L1 provider-contract rows: live, marked, never in the default run.

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (the step's model
loop carries the assistant turn back verbatim) and §11 (the Pinned Model,
endpoint included, rules the Attempt).

MVP row ids: L1-1, L1-3, L1-4a, L1-6.

Every test here needs a real credential, so each skips without one — the
default run never pays. ``SANDBOX_ISSUE`` names the small real task the
L1-6 run claims on ``lbacik/coding-agent-sandbox``; ``SANDBOX_LANGUAGE``
(defaults to ``python``) is recorded as the Attempt's ``target_language``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from coding_agent.github.client import GitHubClient
from coding_agent.ledger.store import RunLedger
from coding_agent.provider.config import (
    DEFAULT_COMPACTION_THRESHOLDS,
    DEFAULT_EFFECTIVE_TOKEN_CEILINGS,
    DEFAULT_PRICE_TABLE,
    PINNED_MODELS,
)
from coding_agent.provider.effective_token_ceiling import effective_token_ceiling_for
from coding_agent.provider.pinned_model import build_chat_model
from coding_agent.provider.price_table import price_for
from coding_agent.runtime.git import checkout_workspace, ensure_mirror, remote_url
from coding_agent.runtime.graph.attempt import run_attempt
from coding_agent.runtime.pinned_prefix import context_window_for
from coding_agent.runtime.result_capping import DEFAULT_RESULT_CAP_LIMIT
from coding_agent.github.issues import fetch_issue
from coding_agent.tracker.port import TrackedIssue
from coding_agent.validate.junit import parse_junit_xml
from support.fake_tracker import FakeTracker

_LIVE_CONFIG = """\
version: 1
target:
  kind: github
  repository: lbacik/coding-agent-sandbox
  base_branch: main
  token_env: GITHUB_TOKEN
models:
  anthropic:
    provider: anthropic
    model: claude-sonnet-5
    endpoint: messages
    effort: medium
    api_key_env: ANTHROPIC_API_KEY
    prices_usd_per_mtok: {input: 2.00, output: 10.00, cache_read: 0.20, cache_write: 2.50}
  openai:
    provider: openai
    model: gpt-5.6-luna
    endpoint: responses
    effort: medium
    api_key_env: OPENAI_API_KEY
    prices_usd_per_mtok: {input: 0.20, output: 1.20, cache_read: 0.02, cache_write: 0.25}
workflows:
  anthropic:
    model: anthropic
    selector: {label: ready-for-agent}
    steps: [{id: implement, skill: implement}]
  openai:
    model: openai
    selector: {label: ready-for-agent}
    steps: [{id: implement, skill: implement}]
"""


def _assert_configured_capability_live(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L1-4a and L1-3 through the Effective Configuration: the pin, its
    credential and its prices come from a Workflow Configuration file; the
    assertion attempts tool calling, usage and the effort pin live; and the
    real response's cost is derived per bucket from the configured prices."""
    from coding_agent.worker.supervisor import configured_pin
    from coding_agent.provider.capability import assert_provider_capability
    from coding_agent.runtime.ceilings import response_usage, usage_breakdown
    from coding_agent.workflow import load_workflow_config, resolve_effective_configuration

    monkeypatch.setenv("GITHUB_TOKEN", os.environ.get("GITHUB_TOKEN") or "unused-by-this-row")
    config_path = tmp_path / "workflow.yml"
    config_path.write_text(_LIVE_CONFIG, encoding="utf-8")
    effective = resolve_effective_configuration(load_workflow_config(config_path), provider)
    pin, price = configured_pin(effective)
    model = build_chat_model(pin, api_key=os.environ[effective.model.api_key_env])

    result = assert_provider_capability(pin, model, {pin.key: price})

    assert result.response.tool_calls
    assert "400" in result.effort_refusal and "effort" in result.effort_refusal
    breakdown = usage_breakdown(result.response.usage_metadata)
    assert breakdown.total_tokens > 0
    assert breakdown.input_tokens > 0 and breakdown.output_tokens > 0
    cost = response_usage(result.response, price).cost_usd
    assert cost > 0
    print(
        f"\n[{pin.key}] tool call: {result.response.tool_calls[0]['name']}"
        f"\n[{pin.key}] usage: {result.response.usage_metadata}"
        f"\n[{pin.key}] cost from configured prices: ${cost:.6f}"
        f"\n[{pin.key}] invalid effort refused: {result.effort_refusal[:160]}"
    )


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY is not set")
def test_l1_4a_configured_capability_assertion_live_anthropic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_configured_capability_live("anthropic", tmp_path, monkeypatch)


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY is not set")
def test_l1_4a_configured_capability_assertion_live_openai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_configured_capability_live("openai", tmp_path, monkeypatch)


SANDBOX_OWNER = "lbacik"
SANDBOX_REPO = "coding-agent-sandbox"


@tool
def record_probe(items: list[str], options: dict[str, str]) -> str:
    """Record a probe call: a list arrives as a list, a mapping as a mapping."""
    return f"items={len(items)} options={sorted(options)}"


def _assert_tool_call_round_trip(provider: str) -> None:
    """L1-1: on the endpoint the adapter pins, the toolset binds, a tool
    call arrives with a well-formed argument object, and the provider
    accepts the verbatim assistant turn back."""
    pin = PINNED_MODELS[provider]
    model = build_chat_model(pin)
    bound = model.bind_tools([record_probe])
    request = "Call record_probe with items [\"a\", \"b\"] and options {\"k\": \"v\"}."
    first = bound.invoke([HumanMessage(content=request)])
    assert isinstance(first, AIMessage)
    assert first.tool_calls, "the provider made no tool call"
    call = first.tool_calls[0]
    assert isinstance(call["args"].get("items"), list)
    assert isinstance(call["args"].get("options"), dict)
    history = [
        HumanMessage(content=request),
        first,
        ToolMessage(
            content="items=2 options=['k']",
            tool_call_id=call["id"],
        ),
    ]
    second = bound.invoke(history)
    assert isinstance(second, AIMessage)


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY is not set"
)
def test_l1_1_tool_call_round_trip_on_the_pinned_anthropic_endpoint() -> None:
    _assert_tool_call_round_trip("anthropic")


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY is not set"
)
def test_l1_1_tool_call_round_trip_on_the_pinned_openai_endpoint() -> None:
    _assert_tool_call_round_trip("openai")


def _run_sandbox_task(provider: str, tmp_path: Path) -> None:
    """L1-6: a small real task on the sandbox repository, verified from the
    test runner's own evidence — never from the model's account."""
    token = os.environ["GITHUB_TOKEN"]
    issue_number = int(os.environ["SANDBOX_ISSUE"])
    language = os.environ.get("SANDBOX_LANGUAGE", "python")
    state_dir = tmp_path / "state"
    # The Target Issue is read from GitHub (a ``GET``); every write lands in
    # the fake, so this row never writes to the sandbox (L1-6 is about the
    # model's real task, not delivery — that is L4-1 through ``agent run``).
    fetched = fetch_issue(GitHubClient(token), SANDBOX_OWNER, SANDBOX_REPO, issue_number)
    tracker = FakeTracker(
        issue=TrackedIssue(number=fetched.number, title=fetched.title, body=fetched.body)
    )
    pin = PINNED_MODELS[provider]
    assert price_for(DEFAULT_PRICE_TABLE, pin) is not None
    ceiling = effective_token_ceiling_for(DEFAULT_EFFECTIVE_TOKEN_CEILINGS, pin)
    assert ceiling is not None
    context_window = context_window_for(pin, DEFAULT_COMPACTION_THRESHOLDS)
    assert context_window is not None

    mirror_dir = state_dir / "mirrors" / SANDBOX_OWNER / f"{SANDBOX_REPO}.git"
    workspace_dir = state_dir / "workspaces" / SANDBOX_OWNER / SANDBOX_REPO
    evidence_dir = state_dir / "evidence" / SANDBOX_OWNER / SANDBOX_REPO / "live"
    ensure_mirror(mirror_dir, remote_url(SANDBOX_OWNER, SANDBOX_REPO, token), redact=token)
    workspace = checkout_workspace(mirror_dir, workspace_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    ledger = RunLedger(state_dir / "run-ledger.sqlite3")
    try:
        result = run_attempt(
            issue_number=issue_number,
            attempt_number=1,
            tracker=tracker,
            ledger=ledger,
            model=build_chat_model(pin),
            workspace=workspace.path,
            effective_configuration={
                "repo": f"{SANDBOX_OWNER}/{SANDBOX_REPO}",
                "issue": issue_number,
                "provider": pin.key,
                "model": pin.model,
                "endpoint": pin.endpoint,
                "effort": pin.effort,
                "target_language": language,
            },
            skills_dir=Path.home() / ".agents" / "skills",
            toolchain_matrix=None,
            evidence_dir=evidence_dir,
            result_cap_limit=DEFAULT_RESULT_CAP_LIMIT,
            context_window=context_window,
            cache_breakpoints=(pin.provider == "anthropic"),
        )
        assert result.outcome == "delivered", result.reason
        outcome = ledger.get_outcome(result.attempt_id)
        assert outcome is not None
        assert (outcome.outcome, outcome.reason) == ("delivered", "verified-completion")
        attempt = ledger.get_attempt(result.attempt_id)
        assert attempt.effective_configuration["endpoint"] == pin.endpoint
        assert attempt.fingerprint, "the Run Ledger holds no Fingerprint"
        reports = sorted(evidence_dir.rglob("test_targeted.xml"))
        assert reports, f"no test-targeted evidence under {evidence_dir}"
        failures: list[str] = []
        executed = 0
        for report in reports:
            junit = parse_junit_xml(report.read_text(encoding="utf-8"))
            executed += junit.executed
            failures.extend(sorted(junit.failure_ids))
        assert executed > 0, "the test runner executed no test"
        assert failures == [], f"the test runner named failures: {failures}"
    finally:
        ledger.close()


def _live_env(provider_key: str) -> bool:
    return bool(
        os.environ.get("GITHUB_TOKEN")
        and os.environ.get(provider_key)
        and os.environ.get("SANDBOX_ISSUE")
    )


@pytest.mark.skipif(
    not _live_env("ANTHROPIC_API_KEY"),
    reason="GITHUB_TOKEN, ANTHROPIC_API_KEY and SANDBOX_ISSUE are not all set",
)
def test_l1_6_small_real_task_on_the_sandbox_anthropic(tmp_path: Path) -> None:
    _run_sandbox_task("anthropic", tmp_path)


@pytest.mark.skipif(
    not _live_env("OPENAI_API_KEY"),
    reason="GITHUB_TOKEN, OPENAI_API_KEY and SANDBOX_ISSUE are not all set",
)
def test_l1_6_small_real_task_on_the_sandbox_openai(tmp_path: Path) -> None:
    _run_sandbox_task("openai", tmp_path)
