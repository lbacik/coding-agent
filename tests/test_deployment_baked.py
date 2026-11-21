"""The baked v3 reference-deployment inputs stay coherent offline.

Contract: the `deployment` image target bakes `docker/deployment/` (a
`version: 4` Workflow Configuration, its prompt files, and the reviewed
skill-policy.json) and the build runs `agent config validate` secret-free
(SKV3-20, image), `prepare-skill-bundle --mode install` as the runtime
user, and `verify-skill-bundle` against the final roots after the last
relevant copy. These tests pin the baked inputs' offline half: the
committed files validate with no Worker GitHub/model secrets set, declare
exactly the four reviewed members at the pinned commit, name only prompt
files that exist beside the configuration, and match the policy reviews.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from coding_agent import cli
from coding_agent.skillbundle.policy import load_policy_file
from support.workflow_config import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
BAKED_DIR = REPO_ROOT / "docker" / "deployment"
BAKED_CONFIG = BAKED_DIR / "workflow.yml"
BAKED_POLICY = BAKED_DIR / "skill-policy.json"

PINNED_SOURCE = "git+https://github.com/mattpocock/skills.git"
PINNED_REF = "3cca18b368ae95cdbdebbff572ccafa662551015"
DECLARED_MEMBERS = ("implement", "tdd", "code-review", "codebase-design")


def _without_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run with the credential names unset, isolated from the ambient env.

    A bare ``delenv`` on an already-unset variable records nothing to
    restore, leaking later ``setenv`` calls into other tests; setting a
    placeholder first keeps the isolation hermetic either way.
    """
    for var in (
        "GITHUB_TOKEN",
        "SANDBOX_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "LITELLM_PROXY_API_KEY",
    ):
        monkeypatch.setenv(var, "test-placeholder")
        monkeypatch.delenv(var)


def test_baked_config_validates_secret_free_and_offline(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_secrets(monkeypatch)

    def _refused(*args: object, **kwargs: object) -> object:
        raise AssertionError("no network call is allowed during config validation")

    monkeypatch.setattr(socket, "socket", _refused)
    monkeypatch.setattr(socket, "create_connection", _refused)

    assert cli.main(["config", "validate", "--config", str(BAKED_CONFIG)]) == 0
    assert "config validate: OK" in capsys.readouterr().out


def test_baked_config_declares_the_pinned_four_member_bundle() -> None:
    cfg, faults = load_config(BAKED_CONFIG)
    assert not faults, [f"{f.path}: {f.detail}" for f in faults]
    assert cfg is not None
    assert cfg.version == 4

    ordered: list[tuple[str, str, str | None]] = []
    for entry in cfg.skills:
        for name in entry.include:
            ref = entry.ref if entry.ref is not None else None
            ordered.append((name, entry.source, ref))
    assert [name for name, _, _ in ordered] == list(DECLARED_MEMBERS)
    assert {source for _, source, _ in ordered} == {PINNED_SOURCE}
    assert {ref for _, _, ref in ordered} == {PINNED_REF}


def test_baked_prompts_exist_beside_the_configuration() -> None:
    cfg, faults = load_config(BAKED_CONFIG)
    assert not faults
    assert cfg is not None
    base = BAKED_CONFIG.parent.resolve()
    seen: list[str] = []
    for workflow in cfg.workflows.values():
        for step in workflow.steps.values():
            agent = step.agent
            if agent is None:
                continue
            for instruction in agent.instructions:
                locator = instruction.prompt
                if locator is None or locator in seen:
                    continue
                seen.append(locator)
                candidate = (base / locator).resolve()
                assert candidate.is_file(), f"prompt {locator!r} is missing"
                assert candidate.read_bytes(), f"prompt {locator!r} is empty"
    assert seen == ["prompts/implement-policy.md", "prompts/review-policy.md"]


def test_baked_policy_reviews_match_the_declared_membership() -> None:
    cfg, faults = load_config(BAKED_CONFIG)
    assert not faults
    assert cfg is not None
    declared = {name for entry in cfg.skills for name in entry.include}

    _, policy, policy_faults = load_policy_file(BAKED_POLICY)
    assert not policy_faults, [f"{f.path}: {f.detail}" for f in policy_faults]
    assert policy is not None
    assert policy.schema_version == 1
    assert sorted(review.name for review in policy.reviews) == sorted(declared)
    for review in policy.reviews:
        assert review.source_identity == PINNED_SOURCE
        assert set(review.requires) <= declared, (
            f"skill {review.name!r} requires an undeclared member"
        )
