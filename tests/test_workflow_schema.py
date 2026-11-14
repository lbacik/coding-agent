"""The ``agent config schema`` JSON Schema for `version: 3` files.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2 (the file)
and §3 (static validation) stay the authority; the schema is an editor aid
only. The schema is built from the validator's own vocabulary, committed at
``schemas/workflow-v3.schema.json``, and checked here. The `version: 2`
schema at ``schemas/workflow-v2.schema.json`` is archived historical and
unsupported (SKV3-01).
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from unittest import mock

import pytest
import yaml
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from coding_agent import cli
from coding_agent.workflow import v2_validate
from coding_agent.workflow.v3_jsonschema import (
    build_v3_json_schema,
    dump_v3_json_schema,
)
from coding_agent.workflow.v2_schema import (
    ALWAYS_PRESENT_TOOLS,
    EFFORTS,
    INPUT_DRAFTS,
    INPUT_TYPES,
    TARGET_KINDS,
    TOOL_GROUPS,
)

FIXTURES = Path(__file__).parent / "fixtures" / "workflows" / "invalid"
EXAMPLES = Path(__file__).parent.parent / "examples" / "workflows.yaml"
COMMITTED = Path(__file__).parent.parent / "schemas" / "workflow-v3.schema.json"
COMMITTED_V2 = Path(__file__).parent.parent / "schemas" / "workflow-v2.schema.json"

REGENERATE_HINT = "agent config schema > schemas/workflow-v3.schema.json"

# Every negative fixture whose refusal is purely structural: the schema must
# reject each of these.
STRUCTURAL_FIXTURES: tuple[str, ...] = (
    "02-unknown-field",
    "10-fan-out-list",
    "11-per-step-model",
    "12-condition-expression",
    "13-wildcard-route",
    "18-unknown-end",
    "19-runtime-only-end",
    "21-limits-on-operation",
    "22-incomplete-reserve",
    "27-github-tool-for-model",
    "28-declared-report-tool",
    "31-reserved-result-declared",
    "33-unknown-outcome-label",
    "34-facts-mode-comment",
    "35-pr-title-from-payload",
    "41-canceled-outcome",
)

# Every other invalid fixture, with a one-line reason why its refusal is not
# structural (stays with `agent config validate`).
NOT_STRUCTURAL: dict[str, str] = {
    "01-duplicate-key": "YAML-level: duplicate keys are refused by the strict loader",
    "03-missing-start": "cross-reference: `start` must name a real step",
    "04-unknown-target": "cross-reference: route target must name a real step",
    "05-route-outside-results": "cross-reference: route key must be a possible result",
    "06-unrouted-business-result": "graph: every business result must be routed",
    "07-default-business-result": "graph: defaults may route only reserved results",
    "08-requirement-unmet-without-requires": "cross-reference: conditional result needs `with.requires`",
    "09-stale-head-wrong-operation": "cross-reference: conditional result needs a producing operation",
    "14-unreachable-step": "graph: every step must be reachable from `start`",
    "15-limit-exceeded-cycle": "graph: `limit-exceeded` routes must be acyclic",
    "16-reserved-to-completed": "graph: reserved results may end only as unfinished/failed",
    "17-reserved-to-blocked": "graph: reserved results may end only as unfinished/failed",
    "20-required-input-unproven": "cross-reference: required inputs must be proven on every path",
    "23-reserves-exceed-ceiling": "limit arithmetic: reserves must stay below the Attempt Ceiling",
    "24-allowance-exceeds-work-pool": "limit arithmetic: allowances must fit the Work Pool",
    "25-publish-review-in-issue-workflow": "input-type/operation pairing: publish-review needs a pull-request Workflow",
    "26-open-pr-in-pr-workflow": "input-type/operation pairing: open-pull-request needs an issue Workflow",
    "29-publish-non-document": "cross-reference: publication bodies come only from `document` fields",
    "30-expression-reference": "cross-reference: inputs must resolve to payload fields",
    "32-anchor-alias": "YAML-level: anchors/aliases are refused by the strict loader",
    "36-inline-secret": "environment: token-env must be a variable name, never a value",
    "37-prompt-escapes-directory": "files: prompt paths must stay inside the configuration directory",
    "38-skill-outside-bundle": "files: skills must be declared in `skills`",
    "39-end-label-equals-own-selection": "Selection Label disjointness: end labels never equal a Selection Label",
    "40-end-label-equals-other-selection": "Selection Label disjointness: end labels never equal a Selection Label",
}


def _load_yaml(path: Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _validator() -> Draft202012Validator:
    return Draft202012Validator(build_v3_json_schema())


def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_schema_is_draft_2020_12_and_meta_valid() -> None:
    schema = build_v3_json_schema()

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


def test_schema_command_is_offline_and_deterministic(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _offline(monkeypatch)

    with mock.patch.dict(os.environ, clear=True):
        assert cli.main(["config", "schema"]) == 0
        first = capsys.readouterr().out

        assert cli.main(["config", "schema"]) == 0
        second = capsys.readouterr().out

    assert first == second
    assert json.loads(first)["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_committed_schema_matches_command_output() -> None:
    committed = COMMITTED.read_text(encoding="utf-8")
    assert committed == dump_v3_json_schema(), (
        f"schemas/workflow-v3.schema.json differs from the generator; "
        f"regenerate with {REGENERATE_HINT}"
    )


def test_shared_enum_constants_are_used_by_validator_and_generator() -> None:
    assert EFFORTS == ("low", "medium", "high")
    assert INPUT_DRAFTS == ("include", "exclude")
    assert INPUT_TYPES == ("issue", "pull-request")
    assert TARGET_KINDS == ("github",)

    # Both modules read the same objects: the validator imports the names.
    assert v2_validate.EFFORTS is EFFORTS
    assert v2_validate.INPUT_DRAFTS is INPUT_DRAFTS
    assert v2_validate.INPUT_TYPES is INPUT_TYPES
    assert v2_validate.TARGET_KINDS is TARGET_KINDS

    schema = build_v3_json_schema()
    text = json.dumps(schema)
    for value in (*EFFORTS, *INPUT_DRAFTS, *INPUT_TYPES, *TARGET_KINDS):
        assert value in text

    assert not (set(TOOL_GROUPS) & set(ALWAYS_PRESENT_TOOLS))


def test_examples_workflows_yaml_passes_the_schema() -> None:
    assert _validator().is_valid(_load_yaml(EXAMPLES))


@pytest.mark.parametrize("name", STRUCTURAL_FIXTURES)
def test_structural_fixture_is_rejected_by_the_schema(name: str) -> None:
    data = _load_yaml(FIXTURES / f"{name}.yaml")

    assert not _validator().is_valid(data), f"{name} passed but must be rejected"


def test_every_fixture_is_sorted_structural_or_not() -> None:
    names = sorted(path.stem for path in FIXTURES.glob("*.yaml"))
    structural = set(STRUCTURAL_FIXTURES)
    assert len(structural) == len(STRUCTURAL_FIXTURES), "duplicate structural entry"
    for name in names:
        in_structural = name in structural
        in_other = name in NOT_STRUCTURAL
        assert in_structural != in_other, (
            f"{name} must appear in exactly one list: STRUCTURAL_FIXTURES "
            "or NOT_STRUCTURAL; add every new fixture to one of them"
        )


def test_both_executors_on_one_step_is_rejected() -> None:
    data = copy.deepcopy(_load_yaml(EXAMPLES))
    assert isinstance(data, dict)
    review = data["workflows"]["review"]["steps"]["review"]
    review["operation"] = "check-readiness"

    assert not _validator().is_valid(data)


def test_neither_executor_on_one_step_is_rejected() -> None:
    data = copy.deepcopy(_load_yaml(EXAMPLES))
    assert isinstance(data, dict)
    review = data["workflows"]["review"]["steps"]["review"]
    del review["agent"]

    assert not _validator().is_valid(data)


def test_gateway_pin_shape_passes_the_schema() -> None:
    data = copy.deepcopy(_load_yaml(EXAMPLES))
    assert isinstance(data, dict)
    models = data["models"]
    assert isinstance(models, dict)
    name = next(iter(models))
    models[name] = {
        "gateway": {"kind": "litellm", "url": "http://pi.lan:4000"},
        "model": "muse-spark-1.3-contributor",
        "endpoint": "responses",
        "effort": "medium",
        "api-key-env": "LITELLM_PROXY_API_KEY",
        "prices-usd-per-mtok": {
            "input": 2.0,
            "output": 10.0,
            "cache-read": 0.2,
            "cache-write": 2.5,
        },
        "output-cap-tokens": 32000,
        "context-threshold-tokens": 100000,
    }

    assert _validator().is_valid(data)


def test_direct_pin_naming_token_keys_fails_the_schema() -> None:
    data = copy.deepcopy(_load_yaml(EXAMPLES))
    assert isinstance(data, dict)
    models = data["models"]
    assert isinstance(models, dict)
    name = next(iter(models))
    assert isinstance(models[name], dict)
    models[name]["output-cap-tokens"] = 32000

    assert not _validator().is_valid(data)


def test_config_help_lists_schema_next_to_validate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["config", "--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "schema" in out
    assert "validate" in out


def test_schema_version_const_is_3() -> None:
    schema = build_v3_json_schema()
    assert schema["properties"]["version"]["const"] == 3
    assert schema["title"] == "Workflow Configuration (version: 3)"
    assert "skills" in schema["properties"]


def test_archived_v2_schema_is_marked_historical() -> None:
    text = COMMITTED_V2.read_text(encoding="utf-8")
    assert "HISTORICAL" in text
    assert "no longer supported" in text
    assert '"const": 2' in text or '"const":2' in text or "const" in text
