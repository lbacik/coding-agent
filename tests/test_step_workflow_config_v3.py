"""Static validation for `version: 3` Workflow Configuration files.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.7 (strict
parsing), §2.8 (trust and paths), §2.9 (``agent config validate`` is static
and offline; every fault names the YAML path) and §3 (static validation),
plus the skills-declaration resolution behind issue #372: a
configuration-wide ``skills`` declaration names the Skill Bundle membership
(exact skill name -> source entry and optional ref) that static
``instructions`` and on-demand availability consume.

Matrix row ids: SKV3-01 … SKV3-04. Only ``version: 3`` is accepted (the
``tests/test_step_workflow_config.py`` suite now covers the ported v3
fixtures plus the dedicated `version: 2` refusal tests); the runtime keeps
using the legacy fixed bundle until a later ticket switches it.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from coding_agent import cli
from coding_agent.workflow import (
    normalize_skill_source,
    skill_membership,
    validate_v3_file,
)
from coding_agent.workflow.v3_jsonschema import (
    build_v3_json_schema,
    dump_v3_json_schema,
)

COMMITTED = Path(__file__).parent.parent / "schemas" / "workflow-v3.schema.json"


def _write_config(tmp_path: Path, text: str) -> Path:
    config = tmp_path / "workflows.yaml"
    config.write_text(text, encoding="utf-8")
    return config


def _minimal_v3_config(**overrides: str) -> str:
    """A minimal valid `version: 3` file; ``overrides`` replaces whole blocks."""
    blocks = {
        "version": "version: 3\n",
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
        ),
        "skills": "",
        "workflows": (
            "workflows:\n"
            "  review:\n"
            "    model: sonnet\n"
            "    input: {type: pull-request, label: agent:review}\n"
            "    limits: {cost-usd: 6, tokens: 1500000,"
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
    config = _write_config(tmp_path, _minimal_v3_config())

    cfg, faults = validate_v3_file(config)

    assert not faults
    assert cfg is not None
    assert cfg.version == 3
    assert cfg.skills == ()
    assert skill_membership(cfg) == {}


def test_empty_skills_list_validates_with_an_empty_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_v3_config(skills="skills: []\n"))

    cfg, faults = validate_v3_file(config)

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
    text = _minimal_v3_config().replace(
        "          tools: [workspace-read]\n",
        "          instructions:\n            - skill: implement\n          tools: [workspace-read]\n",
    )
    config = _write_config(tmp_path, text)

    _, faults = validate_v3_file(config)

    assert faults
    assert any(
        "implement" in str(fault) and "instructions[0]" in fault.path for fault in faults
    ), [str(fault) for fault in faults]


# --- SKV3-02: sources and names --------------------------------------------------


def test_ticket_example_validates_with_normalised_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    text = _minimal_v3_config(skills=SKILLS_TICKET_EXAMPLE).replace(
        "          tools: [workspace-read]\n",
        "          instructions:\n            - skill: security-review\n"
        "          tools: [workspace-read]\n",
    )
    config = _write_config(tmp_path, text)

    cfg, faults = validate_v3_file(config)

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
    text = _minimal_v3_config(
        skills=(
            "skills:\n"
            "  - source: https://github.com/mattpocock/skills.git\n"
            "    include: [implement]\n"
        )
    )
    config = _write_config(tmp_path, text)

    cfg, faults = validate_v3_file(config)

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
    text = _minimal_v3_config(
        skills=(
            "skills:\n"
            "  - source: https://github.com/example/team-skills.git\n"
            "    include: [review-a]\n"
            "  - source: git+https://github.com/example/team-skills.git\n"
            "    include: [review-b]\n"
        )
    )
    config = _write_config(tmp_path, text)

    _, faults = validate_v3_file(config)

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
    text = _minimal_v3_config(
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

    cfg, faults = validate_v3_file(config)

    assert not faults
    assert cfg is not None
    assert set(skill_membership(cfg)) == {"review-a", "review-b", "review-c"}


def test_skill_names_are_exact_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case, spaces and underscores are preserved: near-identical names are
    distinct members, and no Workflow-id regex applies."""
    _without_env(monkeypatch)
    text = _minimal_v3_config(
        skills=(
            "skills:\n"
            "  - source: https://github.com/example/team-skills.git\n"
            "    include: [Implement, implement, 'my skill', my_skill]\n"
        )
    )
    config = _write_config(tmp_path, text)

    cfg, faults = validate_v3_file(config)

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
    text = _minimal_v3_config(
        skills=(
            "skills:\n"
            "  - source: https://github.com/example/first.git\n"
            f"    include: [{first}]\n"
            "  - source: https://github.com/example/second.git\n"
            f"    include: [{second}]\n"
        )
    )
    config = _write_config(tmp_path, text)

    _, faults = validate_v3_file(config)

    assert faults
    assert any("already declared" in str(fault) for fault in faults), [
        str(fault) for fault in faults
    ]


def test_duplicate_skill_names_within_one_entry_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    text = _minimal_v3_config(
        skills=(
            "skills:\n"
            "  - source: https://github.com/example/team-skills.git\n"
            "    include: [implement, implement]\n"
        )
    )
    config = _write_config(tmp_path, text)

    _, faults = validate_v3_file(config)

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
    text = _minimal_v3_config(
        skills=f"skills:\n  - source: {source!r}\n    include: [implement]\n"
        if source
        else "skills:\n  - source: ''\n    include: [implement]\n"
    )
    config = _write_config(tmp_path, text)

    _, faults = validate_v3_file(config)

    assert faults, f"source {source!r} validated but must be refused"
    assert any(fault.path == "skills[0].source" for fault in faults), [
        str(fault) for fault in faults
    ]


@pytest.mark.parametrize("ref_block", ["ref:", "ref: ''", "ref: null", "ref: 123"])
def test_empty_or_missing_refs_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ref_block: str
) -> None:
    _without_env(monkeypatch)
    text = _minimal_v3_config(
        skills=(
            "skills:\n"
            "  - source: https://github.com/example/team-skills.git\n"
            f"    {ref_block}\n"
            "    include: [implement]\n"
        )
    )
    config = _write_config(tmp_path, text)

    _, faults = validate_v3_file(config)

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
    text = _minimal_v3_config(
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
    text = _minimal_v3_config(
        skills=(
            "skills:\n"
            "  - source: https://github.com/example/team-skills.git\n"
            "    branch: main\n"
            "    include: [implement]\n"
        )
    )
    config = _write_config(tmp_path, text)

    _, faults = validate_v3_file(config)

    assert faults
    assert any(
        "unsupported field 'branch'" in str(fault) and fault.path == "skills[0]"
        for fault in faults
    ), [str(fault) for fault in faults]


def test_duplicate_keys_in_a_skill_entry_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    text = _minimal_v3_config(
        skills=(
            "skills:\n"
            "  - source: https://github.com/example/team-skills.git\n"
            "    source: https://github.com/example/other.git\n"
            "    include: [implement]\n"
        )
    )
    config = _write_config(tmp_path, text)

    _, faults = validate_v3_file(config)

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
    config = _write_config(tmp_path, _minimal_v3_config(skills=skills_block))

    _, faults = validate_v3_file(config)

    assert faults
    assert any(fault.path == expect_path for fault in faults), [
        str(fault) for fault in faults
    ]


def test_instruction_naming_an_undeclared_skill_names_its_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    text = _minimal_v3_config(skills=SKILLS_TICKET_EXAMPLE).replace(
        "          tools: [workspace-read]\n",
        "          instructions:\n            - skill: undisclosed\n"
        "          tools: [workspace-read]\n",
    )
    config = _write_config(tmp_path, text)

    _, faults = validate_v3_file(config)

    assert faults
    assert any(
        "undisclosed" in str(fault)
        and fault.path == "workflows.review.steps.review.agent.instructions[0]"
        for fault in faults
    ), [str(fault) for fault in faults]


# --- Phase-API seam: cli.main(argv) ----------------------------------------------


def test_minimal_v3_file_validates_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_v3_config(skills=SKILLS_TICKET_EXAMPLE))

    assert cli.main(["config", "validate", "--config", str(config)]) == 0
    assert "static only; no secrets checked" in capsys.readouterr().out


def test_v3_refusal_through_the_cli_names_the_yaml_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    text = _minimal_v3_config(skills=SKILLS_TICKET_EXAMPLE).replace(
        "          tools: [workspace-read]\n",
        "          instructions:\n            - skill: undisclosed\n"
        "          tools: [workspace-read]\n",
    )
    config = _write_config(tmp_path, text)

    assert cli.main(["config", "validate", "--config", str(config)]) == 1
    err = capsys.readouterr().err

    assert "undisclosed" in err
    assert "workflows.review.steps.review.agent.instructions[0]" in err


def test_v3_validate_makes_no_network_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_v3_config(skills=SKILLS_TICKET_EXAMPLE))

    def _refused(*args: object, **kwargs: object) -> object:
        raise AssertionError("no network call is allowed during config validation")

    monkeypatch.setattr(socket, "socket", _refused)
    monkeypatch.setattr(socket, "create_connection", _refused)

    assert cli.main(["config", "validate", "--config", str(config)]) == 0
    assert "config validate: OK" in capsys.readouterr().out


# --- `agent run` accepts a `version: 3` file like `agent config validate` does --


def test_agent_run_passes_version_parsing_for_a_version_3_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the skills-declaration ticket the Worker still starts the same
    way: a `version: 3` file passes version parsing (the refusal, if any, is
    a later startup step — here the missing toolchain matrix). The runtime
    keeps using the legacy fixed Skill Bundle."""
    _without_env(monkeypatch)
    config = _write_config(tmp_path, _minimal_v3_config(skills=SKILLS_TICKET_EXAMPLE))

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
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "unsupported version" not in err
    assert "toolchain-matrix" in err


# --- The v3 JSON Schema ----------------------------------------------------------


def _v3_validator() -> Draft202012Validator:
    return Draft202012Validator(build_v3_json_schema())


def test_v3_schema_is_draft_2020_12_and_meta_valid() -> None:
    schema = build_v3_json_schema()

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


def test_committed_v3_schema_matches_command_output() -> None:
    committed = COMMITTED.read_text(encoding="utf-8")
    assert committed == dump_v3_json_schema(), (
        "schemas/workflow-v3.schema.json differs from the generator; "
        "regenerate with `agent config schema > schemas/workflow-v3.schema.json`"
    )


def test_config_schema_command_describes_v3(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert cli.main(["config", "schema"]) == 0
    schema = json.loads(capsys.readouterr().out)

    assert schema["properties"]["version"] == build_v3_json_schema()["properties"]["version"]
    assert schema["properties"]["version"]["const"] == 3
    assert "skills" in schema["properties"]


def test_v3_schema_accepts_the_ticket_declaration() -> None:
    import yaml

    doc = yaml.safe_load(_minimal_v3_config(skills=SKILLS_TICKET_EXAMPLE))
    assert isinstance(doc, dict)

    assert _v3_validator().is_valid(doc)


def test_v3_schema_rejects_structural_skills_faults() -> None:
    import yaml

    base = yaml.safe_load(_minimal_v3_config(skills=SKILLS_TICKET_EXAMPLE))
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
        assert not _v3_validator().is_valid(doc), label
