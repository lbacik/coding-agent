from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from coding_agent.profile import (
    Check,
    Evidence,
    MalformedToolchainMatrix,
    MissingReadinessFacts,
    NeedsClarification,
    ProfileSources,
    Ready,
    Service,
    Toolchain,
    UnknownSchema,
    UnresolvedPlaceholder,
    UnsupportedEnvironment,
    assert_toolchain,
    check_service,
    evaluate_readiness,
    load_toolchain_matrix,
    parse_profile,
    parse_profile_yaml,
    render_command,
)
from coding_agent.profile.schema import ProjectProfile

PYTHON_PROFILE_YAML = """
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "uv sync --frozen"
  test_all: "uv run pytest -q --junit-xml={evidence_dir}/test_all.xml"
  test_targeted: "uv run pytest -q --junit-xml={evidence_dir}/test_targeted.xml {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks:
  - name: types
    command: "uv run mypy src tests"
services: []
"""

PHP_PROFILE_YAML = """
schema: 2
language: php
working_directory: .
toolchain:
  php: "8.4"
  package_manager: composer
commands:
  bootstrap: "composer install --no-interaction --no-progress"
  test_all: "vendor/bin/phpunit --log-junit {evidence_dir}/test_all.xml"
  test_targeted: "vendor/bin/phpunit --log-junit {evidence_dir}/test_targeted.xml {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks:
  - name: static-analysis
    command: "vendor/bin/phpstan analyse --no-progress"
services:
  - name: postgres
    url_env: DATABASE_URL
"""

TYPESCRIPT_PROFILE_YAML = """
schema: 2
language: typescript
working_directory: packages/api
toolchain:
  node: "22"
  package_manager: pnpm
commands:
  bootstrap: "pnpm install --frozen-lockfile"
  test_all: "pnpm vitest run --reporter=junit --outputFile={evidence_dir}/test_all.xml"
  test_targeted: "pnpm vitest run --reporter=junit --outputFile={evidence_dir}/test_targeted.xml {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""

TOOLCHAIN_MATRIX: dict[str, Any] = {
    "schema": 1,
    "toolchains": {
        "python": {"version": "3.13.9", "package_manager": {"name": "uv", "version": "0.5.29"}},
        "php": {"version": "8.4.25", "package_manager": {"name": "composer", "version": "2.8.9"}},
        "node": {"version": "22.23.2", "package_manager": {"name": "pnpm", "version": "9.15.4"}},
    },
}


class FakeProber:
    """A `ServiceProber` fake: reachability keyed by `(host, port)`."""

    def __init__(self, reachable: set[tuple[str, int]]) -> None:
        self._reachable = reachable
        self.calls: list[tuple[str, int]] = []

    def probe(self, host: str, port: int, *, timeout: float) -> bool:
        self.calls.append((host, port))
        return (host, port) in self._reachable


def _sources(text: str | None, **kwargs: object) -> ProfileSources:
    return ProfileSources(profile_yaml_text=text, **kwargs)  # type: ignore[arg-type]


# --- Parsing the three example languages (contract §7) ---------------------


def test_parses_the_python_example_from_the_contract() -> None:
    outcome = parse_profile_yaml(PYTHON_PROFILE_YAML)
    assert isinstance(outcome, ProjectProfile)
    assert outcome.language == "python"
    assert outcome.toolchain == Toolchain(runtime="python", version="3.13", package_manager="uv")
    assert outcome.evidence == Evidence(
        format="junit-xml",
        test_all="{evidence_dir}/test_all.xml",
        test_targeted="{evidence_dir}/test_targeted.xml",
    )
    assert outcome.checks == (Check(name="types", command="uv run mypy src tests"),)
    assert outcome.services == ()


def test_parses_the_php_example_with_a_declared_service() -> None:
    outcome = parse_profile_yaml(PHP_PROFILE_YAML)
    assert isinstance(outcome, ProjectProfile)
    assert outcome.toolchain.runtime == "php"
    assert outcome.services == (Service(name="postgres", url_env="DATABASE_URL"),)


def test_parses_the_typescript_example_with_checks_none_and_a_monorepo_working_directory() -> None:
    outcome = parse_profile_yaml(TYPESCRIPT_PROFILE_YAML)
    assert isinstance(outcome, ProjectProfile)
    # typescript declares its toolchain under "node", not "typescript" (contract §7).
    assert outcome.toolchain.runtime == "node"
    assert outcome.working_directory == "packages/api"
    assert outcome.checks == ()


# --- L2-8..L2-11: checks and evidence -------------------------------------


def test_l2_8_checks_none_is_satisfied_not_skipped() -> None:
    outcome = parse_profile(_profile_dict(checks="none"))
    assert isinstance(outcome, ProjectProfile)
    assert outcome.checks == ()


def test_l2_9_checks_empty_list_is_rejected_as_ambiguous() -> None:
    outcome = parse_profile(_profile_dict(checks=[]))
    assert isinstance(outcome, MissingReadinessFacts)
    assert any("ambiguous" in item for item in outcome.missing)


def test_l2_10_checks_key_absent_is_a_missing_readiness_fact() -> None:
    data = _profile_dict()
    del data["checks"]
    outcome = parse_profile(data)
    assert isinstance(outcome, MissingReadinessFacts)
    assert "checks" in outcome.missing


def test_l2_11_evidence_block_absent_for_a_test_command_is_a_missing_readiness_fact() -> None:
    data = _profile_dict()
    del data["evidence"]
    outcome = parse_profile(data)
    assert isinstance(outcome, MissingReadinessFacts)
    assert "evidence" in outcome.missing


def test_evidence_format_other_than_junit_xml_is_a_missing_readiness_fact() -> None:
    data = _profile_dict()
    data["evidence"]["format"] = "tap"
    outcome = parse_profile(data)
    assert isinstance(outcome, MissingReadinessFacts)
    assert any("evidence.format" in item for item in outcome.missing)


def test_missing_facts_are_collected_together_not_one_at_a_time() -> None:
    data = _profile_dict()
    del data["checks"]
    del data["evidence"]
    del data["commands"]
    outcome = parse_profile(data)
    assert isinstance(outcome, MissingReadinessFacts)
    assert {"checks", "evidence", "commands"} <= set(outcome.missing)


# --- L2-13: unknown schema --------------------------------------------------


def test_l2_13_unknown_schema_is_unknown_schema_not_missing_facts() -> None:
    data = _profile_dict()
    data["schema"] = 99
    outcome = parse_profile(data)
    assert isinstance(outcome, UnknownSchema)
    assert outcome.declared == 99


def test_missing_schema_key_is_a_missing_readiness_fact_not_unknown_schema() -> None:
    """Unlike a *wrong* schema version, an absent one is just another gap a
    human can fill in — not a deployment problem no comment can answer."""
    data = _profile_dict()
    del data["schema"]
    outcome = parse_profile(data)
    assert isinstance(outcome, MissingReadinessFacts)
    assert outcome.missing == ("schema",)


def test_a_missing_schema_does_not_short_circuit_collecting_other_gaps() -> None:
    """The Worker publishes one clarification comment covering every open
    question (contract §9) — a missing `schema` must not hide a missing
    `checks` behind a second round."""
    data = _profile_dict()
    del data["schema"]
    del data["checks"]
    outcome = parse_profile(data)
    assert isinstance(outcome, MissingReadinessFacts)
    assert {"schema", "checks"} <= set(outcome.missing)


# --- L2-12: toolchain assertion ---------------------------------------------


def test_l2_12_toolchain_version_outside_the_matrix_is_a_mismatch() -> None:
    matrix = load_toolchain_matrix(TOOLCHAIN_MATRIX)
    mismatch = assert_toolchain(Toolchain(runtime="python", version="3.11", package_manager="uv"), matrix)
    assert mismatch is not None
    assert "3.11" in mismatch.reason and "3.13.9" in mismatch.reason


def test_toolchain_prefix_match_does_not_false_positive_on_a_short_prefix() -> None:
    # "3.1" must not be satisfied by "3.13.9" just because it's a string prefix.
    matrix = load_toolchain_matrix(TOOLCHAIN_MATRIX)
    mismatch = assert_toolchain(Toolchain(runtime="python", version="3.1", package_manager="uv"), matrix)
    assert mismatch is not None


def test_toolchain_version_within_the_matrix_is_satisfied() -> None:
    matrix = load_toolchain_matrix(TOOLCHAIN_MATRIX)
    assert assert_toolchain(Toolchain(runtime="node", version="22", package_manager="pnpm"), matrix) is None


def test_toolchain_package_manager_mismatch_is_reported() -> None:
    matrix = load_toolchain_matrix(TOOLCHAIN_MATRIX)
    mismatch = assert_toolchain(Toolchain(runtime="python", version="3.13", package_manager="pip"), matrix)
    assert mismatch is not None
    assert "pip" in mismatch.reason


def test_toolchain_absent_from_the_matrix_entirely_is_a_mismatch() -> None:
    matrix = load_toolchain_matrix(
        {"schema": 1, "toolchains": {"python": TOOLCHAIN_MATRIX["toolchains"]["python"]}}
    )
    mismatch = assert_toolchain(Toolchain(runtime="node", version="22", package_manager="pnpm"), matrix)
    assert mismatch is not None
    assert "no node toolchain" in mismatch.reason


# --- L2-14: service reachability --------------------------------------------


def test_l2_14_unreachable_declared_service_is_reported() -> None:
    service = Service(name="postgres", url_env="DATABASE_URL")
    prober = FakeProber(reachable=set())
    result = check_service(service, {"DATABASE_URL": "postgres://user:pw@db.internal:5432/app"}, prober)
    assert result is not None
    assert prober.calls == [("db.internal", 5432)]


def test_reachable_declared_service_passes() -> None:
    service = Service(name="postgres", url_env="DATABASE_URL")
    prober = FakeProber(reachable={("db.internal", 5432)})
    result = check_service(service, {"DATABASE_URL": "postgres://db.internal:5432/app"}, prober)
    assert result is None


def test_service_env_var_not_set_is_unreachable() -> None:
    service = Service(name="postgres", url_env="DATABASE_URL")
    result = check_service(service, {}, FakeProber(reachable=set()))
    assert result is not None
    assert "DATABASE_URL" in result.reason


def test_service_url_without_host_or_port_is_unreachable() -> None:
    service = Service(name="postgres", url_env="DATABASE_URL")
    result = check_service(service, {"DATABASE_URL": "not-a-url"}, FakeProber(reachable=set()))
    assert result is not None


# --- evaluate_readiness: the two failure routes, end to end -----------------


def test_ready_end_to_end_with_a_reachable_service() -> None:
    data = _profile_dict()
    data["services"] = [{"name": "postgres", "url_env": "DATABASE_URL"}]
    outcome = evaluate_readiness(
        _sources(_dump(data)),
        load_toolchain_matrix(TOOLCHAIN_MATRIX),
        {"DATABASE_URL": "postgres://db:5432/app"},
        FakeProber(reachable={("db", 5432)}),
    )
    assert isinstance(outcome, Ready)
    assert outcome.profile.services == (Service(name="postgres", url_env="DATABASE_URL"),)


def test_evaluate_readiness_routes_missing_facts_to_clarification() -> None:
    data = _profile_dict()
    del data["checks"]
    outcome = evaluate_readiness(
        _sources(_dump(data)), load_toolchain_matrix(TOOLCHAIN_MATRIX), {}, FakeProber(set())
    )
    assert isinstance(outcome, NeedsClarification)
    assert "checks" in outcome.missing


def test_evaluate_readiness_routes_unknown_schema_to_unsupported_environment_not_clarification() -> None:
    data = _profile_dict()
    data["schema"] = 7
    outcome = evaluate_readiness(
        _sources(_dump(data)), load_toolchain_matrix(TOOLCHAIN_MATRIX), {}, FakeProber(set())
    )
    assert isinstance(outcome, UnsupportedEnvironment)
    assert outcome.reason == "unknown-schema"


def test_evaluate_readiness_routes_toolchain_mismatch_to_unsupported_environment() -> None:
    data = _profile_dict()
    data["toolchain"]["python"] = "3.9"
    outcome = evaluate_readiness(
        _sources(_dump(data)), load_toolchain_matrix(TOOLCHAIN_MATRIX), {}, FakeProber(set())
    )
    assert isinstance(outcome, UnsupportedEnvironment)
    assert outcome.reason == "toolchain-mismatch"


def test_evaluate_readiness_routes_unreachable_service_to_unsupported_environment_before_implementation() -> None:
    data = _profile_dict()
    data["services"] = [{"name": "postgres", "url_env": "DATABASE_URL"}]
    outcome = evaluate_readiness(
        _sources(_dump(data)),
        load_toolchain_matrix(TOOLCHAIN_MATRIX),
        {"DATABASE_URL": "postgres://db:5432/app"},
        FakeProber(reachable=set()),
    )
    assert isinstance(outcome, UnsupportedEnvironment)
    assert outcome.reason == "unreachable-service"


# --- L2-15, L2-16: the profile-versus-fallback precedence -------------------


def _fallback_profile(**overrides: object) -> ProjectProfile:
    base = dict(
        schema=2,
        language="python",
        working_directory=".",
        toolchain=Toolchain(runtime="python", version="3.13", package_manager="uv"),
        bootstrap="uv sync --frozen",
        test_all="uv run pytest -q",
        test_targeted="uv run pytest -q {path}",
        evidence=Evidence(format="junit-xml", test_all="a.xml", test_targeted="b.xml"),
        checks=(),
        services=(),
    )
    base.update(overrides)
    return ProjectProfile(**base)  # type: ignore[arg-type]


def test_l2_15_profile_file_wins_over_a_disagreeing_fallback() -> None:
    fallback = _fallback_profile(working_directory="from-fallback")
    sources = ProfileSources(profile_yaml_text=PYTHON_PROFILE_YAML, fallback=fallback)
    outcome = evaluate_readiness(sources, load_toolchain_matrix(TOOLCHAIN_MATRIX), {}, FakeProber(set()))
    assert isinstance(outcome, Ready)
    assert outcome.profile.working_directory == "."  # from the profile file, not the fallback


def test_l2_16_no_profile_resolves_from_the_fallback_chain() -> None:
    fallback = _fallback_profile()
    sources = ProfileSources(profile_yaml_text=None, fallback=fallback, sources_searched=("AGENTS.md",))
    outcome = evaluate_readiness(sources, load_toolchain_matrix(TOOLCHAIN_MATRIX), {}, FakeProber(set()))
    assert isinstance(outcome, Ready)
    assert outcome.profile is fallback


def test_no_profile_and_no_fallback_is_clarification_naming_sources_searched() -> None:
    sources = ProfileSources(
        profile_yaml_text=None, fallback=None, sources_searched=("AGENTS.md", "docs/agents/")
    )
    outcome = evaluate_readiness(sources, load_toolchain_matrix(TOOLCHAIN_MATRIX), {}, FakeProber(set()))
    assert isinstance(outcome, NeedsClarification)
    assert outcome.sources_searched == ("AGENTS.md", "docs/agents/")


# --- L2-17: the Validation Contract is pinned, immutable once obtained -----


def test_l2_17_a_later_profile_edit_does_not_reach_back_into_an_already_parsed_contract() -> None:
    pinned_at_claim = parse_profile_yaml(PYTHON_PROFILE_YAML)
    assert isinstance(pinned_at_claim, ProjectProfile)

    edited_text = PYTHON_PROFILE_YAML.replace(
        'command: "uv run mypy src tests"', 'command: "uv run mypy src tests --strict"'
    )
    reparsed = parse_profile_yaml(edited_text)
    assert isinstance(reparsed, ProjectProfile)

    assert pinned_at_claim.checks == (Check(name="types", command="uv run mypy src tests"),)
    assert reparsed.checks == (Check(name="types", command="uv run mypy src tests --strict"),)
    with pytest.raises(AttributeError):
        pinned_at_claim.checks = ()  # type: ignore[misc]


# --- L2-18: one working area, carried through -------------------------------


def test_l2_18_working_directory_names_the_one_declared_working_area() -> None:
    outcome = parse_profile_yaml(TYPESCRIPT_PROFILE_YAML)
    assert isinstance(outcome, ProjectProfile)
    assert outcome.working_directory == "packages/api"


# --- {path} / {evidence_dir} substitution -----------------------------------


def test_render_command_substitutes_both_placeholders() -> None:
    rendered = render_command(
        "uv run pytest -q --junit-xml={evidence_dir}/test_targeted.xml {path}",
        path="tests/test_foo.py",
        evidence_dir=Path("/var/lib/coding-agent/artifacts/attempt-1"),
    )
    assert rendered == (
        "uv run pytest -q --junit-xml=/var/lib/coding-agent/artifacts/attempt-1/test_targeted.xml "
        "tests/test_foo.py"
    )


def test_render_command_leaves_a_template_without_placeholders_untouched() -> None:
    assert render_command("uv sync --frozen") == "uv sync --frozen"


def test_render_command_raises_when_a_placeholder_is_left_unresolved() -> None:
    with pytest.raises(UnresolvedPlaceholder, match=r"\{path\}"):
        render_command("uv run pytest -q {path}")


# --- domain exceptions for malformed inputs the caller controls ------------


def test_load_toolchain_matrix_raises_a_named_exception_on_malformed_input() -> None:
    with pytest.raises(MalformedToolchainMatrix):
        load_toolchain_matrix({"schema": 1, "toolchains": {"python": {"version": "3.13.9"}}})


# --- helpers -----------------------------------------------------------------


def _profile_dict(**overrides: object) -> dict[str, Any]:
    data: dict[str, Any] = copy.deepcopy(yaml.safe_load(PYTHON_PROFILE_YAML))
    for key, value in overrides.items():
        data[key] = value
    return data


def _dump(data: dict[str, Any]) -> str:
    return str(yaml.safe_dump(data))
