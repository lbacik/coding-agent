"""Advisor pins in the Effective Configuration identity core (§2.10).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.10 (the
identity core records every Advisor pin that differs from the Pinned Model,
in full as ``pinned_model`` is recorded, and the digest covers it;
``advisor.prompt`` is digested and archived before Claim like every operator
prompt) and ADR 0036 (the digest covers the Advisor pin and the prompt
bytes; records pinned before this change still recover).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from coding_agent.ledger.store import digest_effective_configuration
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.provider.price_table import TokenPrices
from coding_agent.skillbundle.deployment import _prompt_locators
from coding_agent.workflow.configuration import LoadedWorkflowConfiguration
from coding_agent.workflow.effective import (
    build_effective_configuration,
    is_full_effective_configuration,
    prompt_texts_from_run_inputs,
)
from coding_agent.workflow.schema import (
    Advisor,
    Agent,
    AttemptCeiling,
    InputDecl,
    Instruction,
    ModelDecl,
    ModelPrices,
    RouteTarget,
    TargetRepository,
    WorkerSettings,
    Workflow,
    WorkflowConfiguration,
    WorkflowStep,
)
from support.effective_config import TEST_PIN, TEST_PRICES, _model_decl
from support.run_inputs import make_run_inputs

ADVISOR_PROMPT_LOCATOR = "advisor.md"
ADVISOR_PROMPT_TEXT = "You are the triage advisor. Recommend the cheapest correct fix.\n"
INSTRUCTION_PROMPT_LOCATOR = "prompts/greet.md"
INSTRUCTION_PROMPT_TEXT = "Greet the worker.\n"

SAGE_PIN = PinnedModel(
    provider="anthropic",
    model="sage-model",
    endpoint="messages",
    effort="high",
    output_cap_tokens=8000,
    context_threshold_tokens=100000,
    context_window_tokens=200000,
)
SAGE_PRICES = TokenPrices(input=100.0, output=200.0, cache_read=10.0, cache_write=100.0)


def _advisor_workflow(*, advisor_model: str | None = "sage") -> Workflow:
    step = WorkflowStep(
        id="process",
        agent=Agent(
            instructions=(Instruction(prompt=INSTRUCTION_PROMPT_LOCATOR),),
            tools=("workspace-read", "advisor"),
            results={"done": {}},
            advisor=Advisor(prompt=ADVISOR_PROMPT_LOCATOR, model=advisor_model),
        ),
        max_executions=1,
        limits={},
        routes={
            "done": RouteTarget(end="completed"),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
    )
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(
            cost_usd=100000.0,
            effective_tokens=100000.0,
            tool_calls=10000.0,
            wall_clock_minutes=600.0,
        ),
        start="process",
        steps={"process": step},
    )


def _two_model_config(workflow: Workflow) -> WorkflowConfiguration:
    return WorkflowConfiguration(
        version=4,
        target=TargetRepository(
            kind="github",
            repository="example-owner/example-repo",
            base_branch="main",
            token_env="GITHUB_TOKEN",
        ),
        models={
            "sonnet": _model_decl(
                model_id="sonnet",
                pin=TEST_PIN,
                prices=TEST_PRICES,
                api_key_env="ANTHROPIC_API_KEY",
                gateway_url_env=None,
            ),
            "sage": _model_decl(
                model_id="sage",
                pin=SAGE_PIN,
                prices=SAGE_PRICES,
                api_key_env="ANTHROPIC_API_KEY",
                gateway_url_env=None,
            ),
        },
        worker=WorkerSettings(),
        workflows={workflow.id: workflow},
        skills=(),
        source=Path("workflows.yaml"),
    )


def _advisor_record(
    workflow: Workflow,
    config: WorkflowConfiguration,
    *,
    prompt_text: str = ADVISOR_PROMPT_TEXT,
    advisor_pins: Mapping[str, PinnedModel] | None = None,
    advisor_prices: Mapping[str, TokenPrices] | None = None,
) -> dict[str, object]:
    loaded = LoadedWorkflowConfiguration(
        path=config.source, raw=b"", config=config
    )
    run_inputs = make_run_inputs(
        loaded,
        prompt_raws={
            INSTRUCTION_PROMPT_LOCATOR: INSTRUCTION_PROMPT_TEXT.encode("utf-8"),
            ADVISOR_PROMPT_LOCATOR: prompt_text.encode("utf-8"),
        },
    )
    pins = {"sage": SAGE_PIN} if advisor_pins is None else dict(advisor_pins)
    prices = {"sage": SAGE_PRICES} if advisor_prices is None else dict(advisor_prices)
    return build_effective_configuration(
        run_inputs,
        workflow=workflow,
        pin=TEST_PIN,
        prices=TEST_PRICES,
        advisor_pins=pins,
        advisor_prices=prices,
    )


def test_digest_changes_when_the_advisor_pin_differs() -> None:
    """A different Advisor pin moves the identity digest."""
    workflow = _advisor_workflow()
    config = _two_model_config(workflow)
    base = _advisor_record(workflow, config)
    assert is_full_effective_configuration(base) is True
    assert "advisor_pins" in base

    other_pin = PinnedModel(
        provider="anthropic",
        model="sage-model-v2",
        endpoint="messages",
        effort="high",
        output_cap_tokens=8000,
        context_threshold_tokens=100000,
        context_window_tokens=200000,
    )
    other_decl = _model_decl(
        model_id="sage",
        pin=other_pin,
        prices=SAGE_PRICES,
        api_key_env="ANTHROPIC_API_KEY",
        gateway_url_env=None,
    )
    other_config = WorkflowConfiguration(
        version=4,
        target=config.target,
        models={"sonnet": config.models["sonnet"], "sage": other_decl},
        worker=WorkerSettings(),
        workflows={workflow.id: workflow},
        skills=(),
        source=Path("workflows.yaml"),
    )
    other = _advisor_record(
        workflow,
        other_config,
        advisor_pins={"sage": other_pin},
        advisor_prices={"sage": SAGE_PRICES},
    )
    assert digest_effective_configuration(other) != digest_effective_configuration(base)


def test_digest_changes_when_the_advisor_prompt_bytes_change() -> None:
    """Different ``advisor.prompt`` bytes move the digest."""
    workflow = _advisor_workflow()
    config = _two_model_config(workflow)
    base = _advisor_record(workflow, config)
    changed = _advisor_record(
        workflow, config, prompt_text=ADVISOR_PROMPT_TEXT + "Be brief.\n"
    )
    assert digest_effective_configuration(changed) != digest_effective_configuration(base)


def test_advisor_prompt_is_collected_and_decoded() -> None:
    """The advisor prompt joins the locator set and the decoded texts."""
    workflow = _advisor_workflow()
    config = _two_model_config(workflow)
    locators = _prompt_locators(config)
    assert INSTRUCTION_PROMPT_LOCATOR in locators
    assert ADVISOR_PROMPT_LOCATOR in locators

    loaded = LoadedWorkflowConfiguration(
        path=config.source, raw=b"", config=config
    )
    run_inputs = make_run_inputs(
        loaded,
        prompt_raws={
            INSTRUCTION_PROMPT_LOCATOR: INSTRUCTION_PROMPT_TEXT.encode("utf-8"),
            ADVISOR_PROMPT_LOCATOR: ADVISOR_PROMPT_TEXT.encode("utf-8"),
        },
    )
    texts = prompt_texts_from_run_inputs(run_inputs, workflow)
    assert texts[ADVISOR_PROMPT_LOCATOR] == ADVISOR_PROMPT_TEXT


def test_record_without_advisor_pins_stays_valid_and_byte_identical() -> None:
    """Attempts recorded before the Advisor slice still recover (§16.4)."""
    workflow = _advisor_workflow(advisor_model=None)
    config = _two_model_config(workflow)
    loaded = LoadedWorkflowConfiguration(
        path=config.source, raw=b"", config=config
    )
    run_inputs = make_run_inputs(
        loaded,
        prompt_raws={
            INSTRUCTION_PROMPT_LOCATOR: INSTRUCTION_PROMPT_TEXT.encode("utf-8"),
            ADVISOR_PROMPT_LOCATOR: ADVISOR_PROMPT_TEXT.encode("utf-8"),
        },
    )
    # An Advisor that uses the Pinned Model records no separate pin: the
    # record keeps the pre-Advisor shape, byte-identical.
    record = build_effective_configuration(
        run_inputs,
        workflow=workflow,
        pin=TEST_PIN,
        prices=TEST_PRICES,
        advisor_pins={},
        advisor_prices={},
    )
    assert "advisor_pins" not in record
    assert is_full_effective_configuration(record) is True
    assert digest_effective_configuration(record) == digest_effective_configuration(
        copy.deepcopy(record)
    )


def test_agent_record_omits_advisor_when_absent() -> None:
    """A step without ``advisor`` carries no ``advisor`` key (byte-identical)."""
    step = WorkflowStep(
        id="process",
        agent=Agent(
            instructions=(Instruction(prompt=INSTRUCTION_PROMPT_LOCATOR),),
            tools=("workspace-read",),
            results={"done": {}},
        ),
        max_executions=1,
        limits={},
        routes={
            "done": RouteTarget(end="completed"),
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
    )
    workflow = Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:test"),
        ceiling=AttemptCeiling(
            cost_usd=100000.0,
            effective_tokens=100000.0,
            tool_calls=10000.0,
            wall_clock_minutes=600.0,
        ),
        start="process",
        steps={"process": step},
    )
    config = _two_model_config(workflow)
    record = _advisor_record(workflow, config)
    steps = record["workflow"]
    assert isinstance(steps, dict)
    compiled = steps["steps"]
    assert isinstance(compiled, dict)
    agent = compiled["process"]["agent"]
    assert isinstance(agent, dict)
    assert "advisor" not in agent
    assert is_full_effective_configuration(record) is True


def test_advisor_pins_section_validation() -> None:
    """A well-shaped ``advisor_pins`` section validates; malformed ones refuse."""
    workflow = _advisor_workflow()
    config = _two_model_config(workflow)
    record = _advisor_record(workflow, config)
    assert is_full_effective_configuration(record) is True

    malformed: dict[str, Any] = copy.deepcopy(record)
    assert isinstance(malformed["advisor_pins"], dict)
    malformed["advisor_pins"] = {"sage": {"bogus": 1}}
    assert is_full_effective_configuration(malformed) is False

    not_a_mapping: dict[str, Any] = copy.deepcopy(record)
    not_a_mapping["advisor_pins"] = []
    assert is_full_effective_configuration(not_a_mapping) is False


def test_unknown_advisor_pin_and_missing_prices_are_refused() -> None:
    """Unknown pins and pins without prices raise instead of half-records."""
    workflow = _advisor_workflow()
    config = _two_model_config(workflow)
    with pytest.raises(ValueError, match="unknown Advisor pin"):
        _advisor_record(
            workflow,
            config,
            advisor_pins={"nope": SAGE_PIN},
            advisor_prices={"nope": SAGE_PRICES},
        )
    with pytest.raises(ValueError, match="no prices for Advisor pin"):
        _advisor_record(
            workflow,
            config,
            advisor_pins={"sage": SAGE_PIN},
            advisor_prices={},
        )
