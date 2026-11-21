"""`version: 3` refusal plus the skills-declaration suite on v4 configs.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.7 (strict
parsing), §2.8 (trust and paths), §2.9 (``agent config validate`` is static
and offline; every fault names the YAML path) and §3 (static validation),
plus the skills-declaration resolution behind issue #372: a
configuration-wide ``skills`` declaration names the Skill Bundle membership
(exact skill name -> source entry and optional ref) that static
``instructions`` and on-demand availability consume.

``version: 3`` is no longer accepted: ``agent config validate`` and
``agent run`` refuse it with a migration diagnostic before any install,
network call, Claim or GitHub write, naming the ``version`` path and leaving
the file untouched. The skills behaviours (SKV3-01 … SKV3-04) are
version-independent and are tested on ``version: 4`` files here; the v4 core
suite lives in ``tests/test_step_workflow_config.py``.

Matrix row ids: SKV3-01 … SKV3-04.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from coding_agent import cli
from coding_agent.workflow import (
    normalize_skill_source,
    skill_membership,
)
from coding_agent.workflow.jsonschema import (
    build_workflow_json_schema,
)
from support.workflow_config import load_config

COMMITTED = Path(__file__).parent.parent / "schemas" / "workflow-v4.schema.json"


def _write_config(tmp_path: Path, text: str) -> Path:
    config = tmp_path / "workflows.yaml"
    config.write_text(text, encoding="utf-8")
    return config


def _minimal_config(**overrides: str) -> str:
    """A minimal valid `version: 4` file; ``overrides`` replaces whole blocks."""
    blocks = {
        "version": "version: 4\n",
        "target": (
            "target:\n"
            "  kind: github\n"
            "  repository: OWNER/REPO\n"
            "  base-branch: main\n"
            "  token-env: GITHUB_TOKEN\n"
        ),
        "models": (
            "models:\n"
            "  sonnet:\n"
            "    provider: anthropic\n"
            "    model: claude-sonnet-5\n"
            "    endpoint: messages\n"
            "    effort: medium\n"
            "    api-key-env: ANTHROPIC_API_KEY\n"
            "    prices-usd-per-mtok: {input: 2.00, output: 10.00,"
            " cache-read: 0.20, cache-write: 2.50}\n"
            "    context-window-tokens: 1000000\n"
            "    context-threshold-tokens: 80000\n"
            "    output-cap-tokens: 32000\n"
        ),
        "skills": "",
        "workflows": (
            "workflows:\n"
            "  review:\n"
            "    model: sonnet\n"
            "    input: {type: pull-request, label: agent:review}\n"
            "    limits: {cost-usd: 6, effective-tokens: 1500000,"
            " tool-calls: 300, wall-clock-minutes: 30}\n"
            "    start: review\n"
            "    defaults:\n"
            "      max-executions: 1\n"
            "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
            "    steps:\n"
            "      review:\n"
            "        agent:\n"
            "          tools: [workspace-read]\n"
            "          results:\n"
            "            reviewed: {body: document}\n"
            "        routes: {reviewed: publish}\n"
            "      publish:\n"
            "        operation: publish-review\n"
            "        with: {body: review.reviewed.body}\n"
            "        routes: {published: {end: completed}, stale-head: {end: failed}}\n"
        ),
    }
    blocks.update(overrides)
    return "".join(blocks[key] for key in ("version", "target", "models", "skills", "workflows"))


SKILLS_TICKET_EXAMPLE = """\
skills:
  - source: git+https://github.com/mattpocock/skills.git
    include: [implement, tdd, code-review, codebase-design]
  - source: git+https://github.com/example/team-skills.git
    ref: v1.2.0
    include: [security-review]
"""


def _without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build validation reads no secrets: run with the credential names unset."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_PROXY_API_KEY", raising=False)


# --- SKV3-04 (static part): the empty bundle -------------------------------------


def test_missing_skills_field_validates_with_an_empty_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `skills` field and `skills: []` both mean an empty Skill Bundle."""
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config())

    cfg, faults = load_config(config)

    assert not faults
    assert cfg is not None
    assert cfg.version == 4
    assert cfg.skills == ()
    assert skill_membership(cfg) == {}


def test_empty_skills_list_validates_with_an_empty_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config(skills="skills: []\n"))

    cfg, faults = load_config(config)

    assert not faults
    assert cfg is not None
    assert cfg.skills == ()
    assert skill_membership(cfg) == {}


def test_undeclared_instruction_is_refused_without_an_implicit_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no implicit four-skill default: with no declaration, even
    `implement` is undeclared."""
    _without_env(monkeypatch)
    text = _minimal_config().replace(
        "          tools: [workspace-read]\n",
        "          instructions:\n            - skill: implement\n          tools: [workspace-read]\n",
    )
    config = _write_config(tmp_path, text)

    _, faults = load_config(config)

    assert faults
    assert any(
        "implement" in str(fault) and "instructions[0]" in fault.path for fault in faults
    ), [str(fault) for fault in faults]


# --- SKV3-02: sources and names --------------------------------------------------


def test_ticket_example_validates_with_normalised_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    text = _minimal_config(skills=SKILLS_TICKET_EXAMPLE).replace(
        "          tools: [workspace-read]\n",
        "          instructions:\n            - skill: security-review\n"
        "          tools: [workspace-read]\n",
    )
    config = _write_config(tmp_path, text)

    cfg, faults = load_config(config)

    assert not faults
    assert cfg is not None
    assert len(cfg.skills) == 2
    membership = skill_membership(cfg)
    assert membership["implement"] == (
        "git+https://github.com/mattpocock/skills.git",
        None,
    )
    assert membership["security-review"] == (
        "git+https://github.com/example/team-skills.git",
        "v1.2.0",
    )


def test_https_spelling_normalises_to_the_installer_source_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`https://` and `git+https://` spellings name one Source Identity."""
    _without_env(monkeypatch)
    text = _minimal_config(
        skills=(
            "skills:\n"
            "  - source: https://github.com/mattpocock/skills.git\n"
            "    include: [implement]\n"
        )
    )
    config = _write_config(tmp_path, text)

    cfg, faults = load_config(config)

    assert not faults
    assert cfg is not None
    assert skill_membership(cfg) == {
        "implement": ("git+https://github.com/mattpocock/skills.git", None)
    }


def test_normalize_skill_source_adds_the_git_prefix() -> None:
    assert (
        normalize_skill_source("https://github.com/example/team-skills.git")
        == "git+https://github.com/example/team-skills.git"
    )
    assert (
        normalize_skill_source("git+https://github.com/example/team-skills.git")
        == "git+https://github.com/example/team-skills.git"
    )


def test_both_spellings_of_one_url_are_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    text = _minimal_config(
        skills=(
            "skills:\n"
            "  - source: https://github.com/example/team-skills.git\n"
            "    include: [review-a]\n"
            "  - source: git+https://github.com/example/team-skills.git\n"
            "    include: [review-b]\n"
        )
    )
    config = _write_config(tmp_path, text)

    _, faults = load_config(config)

    assert faults
    assert any("duplicate" in str(fault) and "skills[1]" in fault.path for fault in faults), [
        str(fault) for fault in faults
    ]


def test_dotgit_suffix_and_case_are_not_equated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No equivalence by redirect, `.git` suffix or case: neighbouring
    spellings are distinct sources and coexist."""
    _without_env(monkeypatch)
    text = _minimal_config(
        skills=(
            "skills:\n"
            "  - source: https://github.com/example/team-skills.git\n"
            "    include: [review-a]\n"
            "  - source: https://github.com/example/team-skills\n"
            "    include: [review-b]\n"
            "  - source: https://github.com/Example/Team-Skills.git\n"
            "    include: [review-c]\n"
        )
    )
    config = _write_config(tmp_path, text)

    cfg, faults = load_config(config)

    assert not faults
    assert cfg is not None
    assert set(skill_membership(cfg)) == {"review-a", "review-b", "review-c"}


def test_skill_names_are_exact_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case, spaces and underscores are preserved: near-identical names are
    distinct members, and no Workflow-id regex applies."""
    _without_env(monkeypatch)
    text = _minimal_config(
        skills=(
            "skills:\n"
            "  - source: https://github.com/example/team-skills.git\n"
            "    include: [Implement, implement, 'my skill', my_skill]\n"
        )
    )
    config = _write_config(tmp_path, text)

    cfg, faults = load_config(config)

    assert not faults
    assert cfg is not None
    assert set(skill_membership(cfg)) == {"Implement", "implement", "my skill", "my_skill"}


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("implement", "implement"),
        ("Implement", "Implement"),
        ("my skill", "my skill"),
    ],
)
def test_exact_duplicate_skill_names_are_refused_across_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str, second: str
) -> None:
    _without_env(monkeypatch)
    text = _minimal_config(
        skills=(
            "skills:\n"
            "  - source: https://github.com/example/first.git\n"
            f"    include: [{first}]\n"
            "  - source: https://github.com/example/second.git\n"
            f"    include: [{second}]\n"
        )
    )
    config = _write_config(tmp_path, text)

    _, faults = load_config(config)

    assert faults
    assert any("already declared" in str(fault) for fault in faults), [
        str(fault) for fault in faults
    ]


def test_duplicate_skill_names_within_one_entry_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    text = _minimal_config(
        skills=(
            "skills:\n"
            "  - source: https://github.com/example/team-skills.git\n"
            "    include: [implement, implement]\n"
        )
    )
    config = _write_config(tmp_path, text)

    _, faults = load_config(config)

    assert faults
    assert any("already declared" in str(fault) for fault in faults), [
        str(fault) for fault in faults
    ]


# --- SKV3-03: refused before any subprocess, never echoed -------------------------


@pytest.mark.parametrize(
    "source",
    [
        "https://user:secret@example.com/team-skills.git",
        "https://user@example.com/team-skills.git",
        "https://@example.com/team-skills.git",
        "https://example.com/team-skills.git?token=secret",
        "https://example.com/team-skills.git?",
        "https://example.com/team-skills.git#fragment",
        "https://example.com/team-skills.git#",
        "git+https://user:secret@example.com/team-skills.git",
        "git+https://example.com/team-skills.git?",
        "git+https://example.com/team-skills.git#",
        "ssh://git@example.com/team-skills.git",
        "git+ssh://git@example.com/team-skills.git",
        "git@example.com:team-skills.git",
        "git://example.com/team-skills.git",
        "http://example.com/team-skills.git",
        "file:///opt/team-skills",
        "./team-skills",
        "../team-skills",
        "/opt/team-skills",
        "team-skills",
        "",
    ],
)
def test_unacceptable_sources_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    _without_env(monkeypatch)
    text = _minimal_config(
        skills=f"skills:\n  - source: {source!r}\n    include: [implement]\n"
        if source
        else "skills:\n  - source: ''\n    include: [implement]\n"
    )
    config = _write_config(tmp_path, text)

    _, faults = load_config(config)

    assert faults, f"source {source!r} validated but must be refused"
    assert any(fault.path == "skills[0].source" for fault in faults), [
        str(fault) for fault in faults
    ]


@pytest.mark.parametrize("ref_block", ["ref:", "ref: ''", "ref: null", "ref: 123"])
def test_empty_or_missing_refs_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ref_block: str
) -> None:
    _without_env(monkeypatch)
    text = _minimal_config(
        skills=(
            "skills:\n"
            "  - source: https://github.com/example/team-skills.git\n"
            f"    {ref_block}\n"
            "    include: [implement]\n"
        )
    )
    config = _write_config(tmp_path, text)

    _, faults = load_config(config)

    assert faults
    assert any(fault.path == "skills[0].ref" for fault in faults), [
        str(fault) for fault in faults
    ]


def test_credential_bearing_sources_are_never_echoed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user-info source is refused before any subprocess runs or anything
    is logged, and the diagnostic never echoes the credential."""
    _without_env(monkeypatch)
    secret = "s3cr3t-pr0be-t0ken"
    text = _minimal_config(
        skills=(
            "skills:\n"
            f"  - source: 'https://operator:{secret}@example.com/team-skills.git'\n"
            "    include: [implement]\n"
        )
    )
    config = _write_config(tmp_path, text)

    def _refused(*args: object, **kwargs: object) -> object:
        raise AssertionError("no subprocess or socket may run during validation")

    monkeypatch.setattr(socket, "socket", _refused)
    monkeypatch.setattr(socket, "create_connection", _refused)

    assert cli.main(["config", "validate", "--config", str(config)]) == 1
    err = capsys.readouterr().err

    assert "skills[0].source" in err
    assert secret not in err
    assert "operator" not in err


# --- SKV3-01: strict YAML rules ---------------------------------------------------


def test_unknown_fields_in_a_skill_entry_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    text = _minimal_config(
        skills=(
            "skills:\n"
            "  - source: https://github.com/example/team-skills.git\n"
            "    branch: main\n"
            "    include: [implement]\n"
        )
    )
    config = _write_config(tmp_path, text)

    _, faults = load_config(config)

    assert faults
    assert any(
        "unsupported field 'branch'" in str(fault) and fault.path == "skills[0]"
        for fault in faults
    ), [str(fault) for fault in faults]


def test_duplicate_keys_in_a_skill_entry_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    text = _minimal_config(
        skills=(
            "skills:\n"
            "  - source: https://github.com/example/team-skills.git\n"
            "    source: https://github.com/example/other.git\n"
            "    include: [implement]\n"
        )
    )
    config = _write_config(tmp_path, text)

    _, faults = load_config(config)

    assert faults
    assert any("duplicate key" in str(fault) for fault in faults), [
        str(fault) for fault in faults
    ]


@pytest.mark.parametrize(
    ("skills_block", "expect_path"),
    [
        ("skills: {}\n", "skills"),
        ("skills: implement\n", "skills"),
        (
            "skills:\n  - source: 123\n    include: [implement]\n",
            "skills[0].source",
        ),
        (
            "skills:\n  - source: https://github.com/example/t.git\n    include: implement\n",
            "skills[0].include",
        ),
        (
            "skills:\n  - source: https://github.com/example/t.git\n    include: []\n",
            "skills[0].include",
        ),
        (
            "skills:\n  - source: https://github.com/example/t.git\n    include: [123]\n",
            "skills[0].include[0]",
        ),
        (
            "skills:\n  - source: https://github.com/example/t.git\n    include: ['']\n",
            "skills[0].include[0]",
        ),
        (
            "skills:\n  - source: https://github.com/example/t.git\n    include: [implement]\n    ref: 123\n",
            "skills[0].ref",
        ),
        (
            "skills: [implement]\n",
            "skills[0]",
        ),
        (
            "skills:\n  - https://github.com/example/t.git\n",
            "skills[0]",
        ),
    ],
)
def test_wrong_types_in_skills_are_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skills_block: str,
    expect_path: str,
) -> None:
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config(skills=skills_block))

    _, faults = load_config(config)

    assert faults
    assert any(fault.path == expect_path for fault in faults), [
        str(fault) for fault in faults
    ]


def test_instruction_naming_an_undeclared_skill_names_its_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    text = _minimal_config(skills=SKILLS_TICKET_EXAMPLE).replace(
        "          tools: [workspace-read]\n",
        "          instructions:\n            - skill: undisclosed\n"
        "          tools: [workspace-read]\n",
    )
    config = _write_config(tmp_path, text)

    _, faults = load_config(config)

    assert faults
    assert any(
        "undisclosed" in str(fault)
        and fault.path == "workflows.review.steps.review.agent.instructions[0]"
        for fault in faults
    ), [str(fault) for fault in faults]


# --- Phase-API seam: cli.main(argv) ----------------------------------------------


def test_minimal_v4_file_validates_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config(skills=SKILLS_TICKET_EXAMPLE))

    assert cli.main(["config", "validate", "--config", str(config)]) == 0
    assert "static only; no secrets checked" in capsys.readouterr().out


def test_v4_refusal_through_the_cli_names_the_yaml_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    text = _minimal_config(skills=SKILLS_TICKET_EXAMPLE).replace(
        "          tools: [workspace-read]\n",
        "          instructions:\n            - skill: undisclosed\n"
        "          tools: [workspace-read]\n",
    )
    config = _write_config(tmp_path, text)

    assert cli.main(["config", "validate", "--config", str(config)]) == 1
    err = capsys.readouterr().err

    assert "undisclosed" in err
    assert "workflows.review.steps.review.agent.instructions[0]" in err


def test_v4_validate_makes_no_network_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config(skills=SKILLS_TICKET_EXAMPLE))

    def _refused(*args: object, **kwargs: object) -> object:
        raise AssertionError("no network call is allowed during config validation")

    monkeypatch.setattr(socket, "socket", _refused)
    monkeypatch.setattr(socket, "create_connection", _refused)

    assert cli.main(["config", "validate", "--config", str(config)]) == 0
    assert "config validate: OK" in capsys.readouterr().out


# --- `agent run` accepts a `version: 4` file like `agent config validate` does --


def test_agent_run_passes_version_parsing_for_a_version_4_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Worker still starts the same way: a `version: 4` file passes
    version parsing (the refusal, if any, is a later startup step — here the
    missing toolchain matrix). The runtime verifies the prepared Skill Bundle
    deployment."""
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_config(skills=SKILLS_TICKET_EXAMPLE))

    assert (
        cli.main(
            [
                "run",
                "--config",
                str(config),
                "--workflow",
                "review",
                "--state-dir",
                str(tmp_path / "state"),
                "--policy",
                str(tmp_path / "skill-policy.json"),
                "--bundle-dir",
                str(tmp_path / "bundle"),
                "--skill-home",
                str(tmp_path / "home"),
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "unsupported version" not in err
    assert "toolchain-matrix" in err


# --- `version: 3` is refused with a migration diagnostic --------------------------


_VERSION_3_MINIMAL = """\
version: 3
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
workflows:
  review:
    model: sonnet
    input: {type: pull-request, label: agent:review}
    limits: {cost-usd: 6, tokens: 1500000, tool-calls: 300, wall-clock-minutes: 30}
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


def test_version_3_file_is_refused_with_a_migration_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`validate` refuses `version: 3` at the version gate, naming the
    `version` path and every change the migration needs, leaving the file
    untouched."""
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _VERSION_3_MINIMAL)
    before = config.read_text(encoding="utf-8")

    cfg, faults = load_config(config)

    assert cfg is None
    assert faults
    assert any(
        fault.path == "version"
        and "unsupported version 3" in str(fault)
        and "must be 4" in str(fault)
        and "context-window-tokens" in str(fault)
        and "context-threshold-tokens" in str(fault)
        and "output-cap-tokens" in str(fault)
        and "effective-tokens" in str(fault)
        for fault in faults
    ), [str(fault) for fault in faults]
    assert config.read_text(encoding="utf-8") == before


def test_version_3_refusal_wins_over_content_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The version gate runs before content validation: a `version: 3` file
    carrying further faults still reports only the migration diagnostic."""
    _without_env(monkeypatch)
    text = _VERSION_3_MINIMAL.replace("version: 3\n", "version: 3\nunknown-root: true\n")
    config = _write_config(tmp_path, text)

    cfg, faults = load_config(config)

    assert cfg is None
    assert len(faults) == 1
    assert faults[0].path == "version"
    assert "unsupported version 3" in str(faults[0])


def test_version_3_refusal_through_the_cli_names_version_and_leaves_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _VERSION_3_MINIMAL)
    before = config.read_text(encoding="utf-8")

    assert cli.main(["config", "validate", "--config", str(config)]) == 1
    err = capsys.readouterr().err

    assert "version" in err
    assert "unsupported version 3" in err
    assert "must be 4" in err
    assert "effective-tokens" in err
    assert config.read_text(encoding="utf-8") == before


def test_version_3_validate_makes_no_network_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _VERSION_3_MINIMAL)

    def _refused(*args: object, **kwargs: object) -> object:
        raise AssertionError("no network call is allowed during config validation")

    monkeypatch.setattr(socket, "socket", _refused)
    monkeypatch.setattr(socket, "create_connection", _refused)

    assert cli.main(["config", "validate", "--config", str(config)]) == 1
    assert "unsupported version 3" in capsys.readouterr().err


def test_version_3_run_refuses_before_install_network_claim_or_github_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`agent run` on a `version: 3` file refuses at step 1 with the migration
    diagnostic, before installation, live calls, Claim or GitHub writes."""
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _VERSION_3_MINIMAL)
    before = config.read_text(encoding="utf-8")

    def _refused(*args: object, **kwargs: object) -> object:
        raise AssertionError("no network call is allowed before the refusal")

    monkeypatch.setattr(socket, "socket", _refused)
    monkeypatch.setattr(socket, "create_connection", _refused)

    assert (
        cli.main(
            [
                "run",
                "--config",
                str(config),
                "--workflow",
                "review",
                "--state-dir",
                str(tmp_path / "state"),
                "--policy",
                str(tmp_path / "skill-policy.json"),
                "--bundle-dir",
                str(tmp_path / "bundle"),
                "--skill-home",
                str(tmp_path / "home"),
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "unsupported version 3" in err
    assert "must be 4" in err
    assert config.read_text(encoding="utf-8") == before


# --- The v4 JSON Schema ----------------------------------------------------------


def _v4_validator() -> Draft202012Validator:
    return Draft202012Validator(build_workflow_json_schema())


def test_workflow_schema_is_draft_2020_12_and_meta_valid() -> None:
    schema = build_workflow_json_schema()

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


def test_committed_v4_schema_matches_command_output() -> None:
    committed = COMMITTED.read_text(encoding="utf-8")
    from coding_agent.workflow.jsonschema import dump_workflow_json_schema

    assert committed == dump_workflow_json_schema(), (
        "schemas/workflow-v4.schema.json differs from the generator; "
        "regenerate with `agent config schema > schemas/workflow-v4.schema.json`"
    )


def test_config_schema_command_describes_v4(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert cli.main(["config", "schema"]) == 0
    schema = json.loads(capsys.readouterr().out)

    assert schema["properties"]["version"]["const"] == 4
    assert "skills" in schema["properties"]
    assert (
        schema["properties"]["models"]["additionalProperties"]["required"]
        == sorted(
            [
                "api-key-env",
                "context-threshold-tokens",
                "context-window-tokens",
                "effort",
                "endpoint",
                "model",
                "output-cap-tokens",
                "prices-usd-per-mtok",
            ]
        )
    )


def test_v4_schema_accepts_the_ticket_declaration() -> None:
    doc = yaml.safe_load(_minimal_config(skills=SKILLS_TICKET_EXAMPLE))
    assert isinstance(doc, dict)

    assert _v4_validator().is_valid(doc)


def test_v4_schema_rejects_a_version_3_document() -> None:
    """The old shape — `version: 3`, token keys missing, `tokens` ceiling —
    is not a valid v4 document."""
    doc = yaml.safe_load(_VERSION_3_MINIMAL)
    assert isinstance(doc, dict)

    assert not _v4_validator().is_valid(doc)


def test_v4_schema_rejects_structural_skills_faults() -> None:
    base = yaml.safe_load(_minimal_config(skills=SKILLS_TICKET_EXAMPLE))
    assert isinstance(base, dict)

    cases = [
        ("unknown field", {"source": "https://example.com/t.git", "include": ["a"], "branch": "main"}),
        ("missing source", {"include": ["a"]}),
        ("missing include", {"source": "https://example.com/t.git"}),
        ("empty ref", {"source": "https://example.com/t.git", "include": ["a"], "ref": ""}),
        ("string include", {"source": "https://example.com/t.git", "include": "a"}),
    ]
    for label, entry in cases:
        doc = dict(base)
        doc["skills"] = [entry]
        assert not _v4_validator().is_valid(doc), label
