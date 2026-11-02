"""L1 provider-contract rows: live, marked, never in the default run.

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (the step's model
loop carries the assistant turn back verbatim) and §11 (the Pinned Model,
endpoint included, rules the Attempt).

MVP row ids: L1-1, L1-6.

Every test here needs a real credential, so each skips without one — the
default run never pays. ``SANDBOX_ISSUE`` names the small real task the
L1-6 run claims on ``lbacik/coding-agent-sandbox``; ``SANDBOX_LANGUAGE``
(defaults to ``python``) feeds the Attempt Header the same way
``--target-language`` does.
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
from coding_agent.tracker.local import LocalTracker
from coding_agent.validate.junit import parse_junit_xml

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
    tracker = LocalTracker(
        owner=SANDBOX_OWNER, repo=SANDBOX_REPO, client=GitHubClient(token)
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
