"""A per-pin ``max-rounds`` bound for Advisor and Sub-agent runs (#691).

Contract: the Workflow Configuration's ``models.<id>.max-rounds`` (optional
plain integer 1–32, omitted means 8) bounds every Advisor call and
Sub-agent run the pin answers; the Step Execution loop itself has no round
bound.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from coding_agent.provider.pinned_model import (
    DEFAULT_MAX_ROUNDS,
    MAX_ROUNDS_CAP,
    PinnedModel,
)
from coding_agent.runtime.graph.advisor import advisor_tool_result_text
from coding_agent.runtime.graph.subagent import (
    SubagentResult,
    run_subagent,
    subagent_tool_result_text,
)
from coding_agent.runtime.graph.subagent_member import (
    pin_violation_subagent_ended_payload,
    refused_subagent_ended_payload,
    subagent_ended_payload,
)
from coding_agent.runtime.ceilings import OriginCell
from support.workflow_config import load_config


def _minimal_config() -> str:
    """A minimal valid `version: 4` file, mirroring the config suite."""
    return """\
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
workflows:
  review:
    model: sonnet
    input: {type: pull-request, label: agent:review}
    limits: {cost-usd: 6, effective-tokens: 1500000, tool-calls: 300, wall-clock-minutes: 30}
    start: review
    defaults:
      max-executions: 1
      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}
    steps:
      review:
        agent:
          tools: [workspace-read]
          results:
            reviewed: {body: document}
        routes: {reviewed: publish}
      publish:
        operation: publish-review
        with: {body: review.reviewed.body}
        routes: {published: {end: completed}, stale-head: {end: failed}}
"""


def _write_config(tmp_path: Path, text: str) -> Path:
    config = tmp_path / "workflows.yaml"
    config.write_text(text, encoding="utf-8")
    return config


def _with_max_rounds(text: str, insertion: str) -> str:
    anchor = "    output-cap-tokens: 32000\n"
    assert anchor in text
    return text.replace(anchor, anchor + insertion, 1)


def test_omitted_max_rounds_means_eight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg, faults = load_config(_write_config(tmp_path, _minimal_config()))

    assert not faults
    assert cfg is not None
    assert cfg.models["sonnet"].max_rounds == DEFAULT_MAX_ROUNDS == 8


@pytest.mark.parametrize("value", ["1", "8", "16", "32"])
def test_max_rounds_edges_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    text = _with_max_rounds(_minimal_config(), f"    max-rounds: {value}\n")
    cfg, faults = load_config(_write_config(tmp_path, text))

    assert not faults
    assert cfg is not None
    assert cfg.models["sonnet"].max_rounds == int(value)


@pytest.mark.parametrize("value", ["0", "-1", "33", "10000"])
def test_max_rounds_range_violations_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Out-of-range bounds are refused, naming the path and the 1–32 range."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    text = _with_max_rounds(_minimal_config(), f"    max-rounds: {value}\n")
    cfg, faults = load_config(_write_config(tmp_path, text))

    assert cfg is None
    assert faults
    assert any("models.sonnet.max-rounds" in str(fault) for fault in faults)
    if int(value) < 1:
        assert any(">= 1" in str(fault) for fault in faults)
    else:
        assert any("between 1 and 32" in str(fault) for fault in faults)


@pytest.mark.parametrize("value", ["true", "1.5", '"16"'])
def test_max_rounds_non_integers_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Bools, floats and strings are refused as type errors with the range."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    text = _with_max_rounds(_minimal_config(), f"    max-rounds: {value}\n")
    cfg, faults = load_config(_write_config(tmp_path, text))

    assert cfg is None
    assert faults
    assert any("models.sonnet.max-rounds" in str(fault) for fault in faults)
    assert any("between 1 and 32" in str(fault) for fault in faults)


def test_configured_pin_carries_max_rounds() -> None:
    """The startup pin keeps the declared bound; omitted means the default."""
    from coding_agent.worker.startup import configured_pin
    from coding_agent.workflow.schema import ModelDecl, ModelPrices

    def _decl(max_rounds: int | None) -> ModelDecl:
        return ModelDecl(
            id="sonnet",
            provider="anthropic",
            model="claude-sonnet-5",
            endpoint="messages",
            effort="medium",
            api_key_env="ANTHROPIC_API_KEY",
            prices=ModelPrices(
                input=2.0, output=10.0, cache_read=0.2, cache_write=2.5
            ),
            gateway_kind=None,
            gateway_url=None,
            gateway_url_env=None,
            output_cap_tokens=32000,
            context_threshold_tokens=80000,
            context_window_tokens=1000000,
            max_rounds=max_rounds,
        )

    pin, _ = configured_pin(_decl(16))
    assert pin.max_rounds == 16

    default_pin, _ = configured_pin(_decl(None))
    assert default_pin.max_rounds == DEFAULT_MAX_ROUNDS == 8


def test_pinned_model_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        PinnedModel(
            provider="anthropic",
            model="claude-sonnet-5",
            endpoint="messages",
            effort="medium",
            max_rounds=0,
        )
    with pytest.raises(ValueError):
        PinnedModel(
            provider="anthropic",
            model="claude-sonnet-5",
            endpoint="messages",
            effort="medium",
            max_rounds=MAX_ROUNDS_CAP + 1,
        )


def _ai_with_call(
    name: str, args: dict[str, object], call_id: str
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": args, "id": call_id, "type": "tool_call"}
        ],
        usage_metadata={"input_tokens": 20, "output_tokens": 4, "total_tokens": 24},
    )


def _read_script(rounds: int) -> list[AIMessage]:
    return [
        _ai_with_call("read_file", {"path": "README.md"}, f"r{index}")
        for index in range(rounds)
    ]


def _do_report(script: list[AIMessage], answer: str = "findings") -> None:
    script.append(
        _ai_with_call("report_subagent", {"report": answer}, "rep-1")
    )


def test_subagent_default_bound_is_eight(tmp_path: Path) -> None:
    """Eight read rounds without a report end `round-limit`, naming 8."""
    from support.scripted_model import ScriptedModel

    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "README.md").write_text("hello\n", encoding="utf-8")
    script = _read_script(8)
    model = ScriptedModel(script)

    result = run_subagent(
        model,
        description="look",
        instructions="read the workspace files",
        call_id="s1",
        workspace=workspace,
    )

    assert result.status == "no-report"
    assert result.reason == "round-limit"
    assert result.max_rounds == 8
    text = subagent_tool_result_text(result)
    assert "no-report (round-limit)" in text
    assert "8-round bound" in text


def test_subagent_raised_bound_reports_after_eight(tmp_path: Path) -> None:
    """A pin with `max-rounds: 16` reports on round 11 instead of stopping."""
    from support.scripted_model import ScriptedModel

    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "README.md").write_text("hello\n", encoding="utf-8")
    script = _read_script(10)
    _do_report(script)
    model = ScriptedModel(script)

    result = run_subagent(
        model,
        description="look",
        instructions="read the workspace files",
        call_id="s1",
        workspace=workspace,
        max_rounds=16,
    )

    assert result.status == "reported"
    assert result.reason == "reported"
    assert result.report == "findings"
    assert result.max_rounds == 16


def test_advisor_raised_bound_reports_after_eight(tmp_path: Path) -> None:
    """An Advisor pin with a raised bound reports instead of `round-limit`."""
    from support.scripted_model import ScriptedModel

    from coding_agent.runtime.graph.advisor import (
        REPORT_ADVICE_TOOL,
        build_advisor_tools,
    )
    from coding_agent.runtime.graph.subagent import SubagentRecord

    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "README.md").write_text("hello\n", encoding="utf-8")
    script = _read_script(10)
    script.append(
        _ai_with_call(REPORT_ADVICE_TOOL, {"answer": "advice"}, "adv-1")
    )
    model = ScriptedModel(script)
    record = SubagentRecord(description="advisor")
    tools = build_advisor_tools(workspace, record)

    result = run_subagent(
        model,
        description="advisor",
        instructions="a question",
        call_id="advisor-0",
        workspace=workspace,
        opening_text="advisor opening",
        tool_set=tools,
        shared_record=record,
        report_tool_name=REPORT_ADVICE_TOOL,
        max_rounds=16,
    )

    assert result.status == "reported"
    assert result.max_rounds == 16

    limited = SubagentResult(
        call_id="advisor-0",
        description="advisor",
        status="no-report",
        reason="round-limit",
        report=None,
        messages=[],
        max_rounds=16,
    )
    assert "16-round bound" in advisor_tool_result_text(limited, "triage")


def test_ended_payloads_carry_the_bound_in_force() -> None:
    result = SubagentResult(
        call_id="s1",
        description="look",
        status="no-report",
        reason="round-limit",
        report=None,
        messages=[],
        max_rounds=16,
    )
    cell = OriginCell(input_tokens=100, output_tokens=10, responses=1)
    payload = subagent_ended_payload(
        attempt_id="implement#7/1",
        step_id="process",
        seq=1,
        call_id="s1",
        result=result,
        response_models=["test-model"],
        member_cell=cell,
        usable_threshold=1000,
    )
    assert payload["ending"] == "round-limit"
    assert payload["max_rounds"] == 16

    refused = refused_subagent_ended_payload(
        attempt_id="implement#7/1",
        step_id="process",
        seq=2,
        call_id="spawn-2",
        max_rounds=16,
    )
    assert refused["ending"] == "refused"
    assert refused["max_rounds"] == 16
    assert (
        refused_subagent_ended_payload(
            attempt_id="a", step_id="s", seq=1, call_id="c"
        )["max_rounds"]
        == 8
    )

    violation = pin_violation_subagent_ended_payload(
        attempt_id="implement#7/1",
        step_id="process",
        seq=1,
        call_id="s1",
        observed="model-other",
        member_cell=cell,
        duration_seconds=1.0,
        peak=None,
        max_rounds=16,
    )
    assert violation["ending"] == "pin-violation"
    assert violation["max_rounds"] == 16


def test_effective_record_pins_the_bound() -> None:
    """The Effective Configuration carries the resolved bound; legacy stays valid."""
    from coding_agent.provider.price_table import TokenPrices
    from coding_agent.workflow.effective import (
        _is_valid_pin_section,
        _pinned_model_record,
        make_test_record,
    )
    from coding_agent.provider.config import policy_for_pin

    pin = PinnedModel(
        provider="anthropic",
        model="claude-sonnet-5",
        endpoint="messages",
        effort="medium",
        output_cap_tokens=32000,
        context_threshold_tokens=80000,
        context_window_tokens=1000000,
        max_rounds=16,
    )
    prices = TokenPrices(input=2.0, output=10.0, cache_read=0.2, cache_write=2.5)
    record = _pinned_model_record(pin, prices, policy_for_pin(pin), "ANTHROPIC_API_KEY")
    assert record["max_rounds"] == 16
    assert _is_valid_pin_section(record)

    legacy = dict(record)
    del legacy["max_rounds"]
    assert _is_valid_pin_section(legacy)

    bad = dict(record)
    bad["max_rounds"] = 33
    assert not _is_valid_pin_section(bad)

    test_record = make_test_record()
    pinned_section = test_record["pinned_model"]
    assert isinstance(pinned_section, dict)
    assert pinned_section["max_rounds"] == 8
