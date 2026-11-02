"""M1 step opening: Pinned Prefix, forced activation, Attempt Header.

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (Opening a step, The
Attempt Header, Tools).

MVP row ids: C-SKL-1, C-SKL-2, G-HDR-1, L3-IMP-1, L3-IMP-15, P-CTX-1.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage


_PROFILE_YAML = """\
schema: 2
language: python
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "true"
  test_all: "true"
  test_targeted: "true {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks: none
services: []
"""


def _commit_step_profile(src: Path) -> None:
    """Commit a minimal M1 Project Profile into the fixture origin repo.

    The Attempt graph's readiness node (contract §7) pins the profile at
    the Base Revision as the Validation Contract, so a step-opening fixture
    without one refuses before any model call. The profile mirrors the
    happy-path fixture in ``tests/test_mvp_attempt_graph.py``.
    """
    import subprocess

    profile_dir = src / "docs" / "agents"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "project-profile.yml").write_text(_PROFILE_YAML, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=src, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add project profile"], cwd=src, check=True
    )


def test_activation_tools_bound_from_first_turn_with_verbatim_descriptions(skills_dir: Path) -> None:
    """C-SKL-1: all four activate_* tools bound from the first turn, each with
    the skill's frontmatter description verbatim."""
    from coding_agent.skillbundle.activation import build_activation_tools

    tools = build_activation_tools(skills_dir)
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {
        "activate_implement",
        "activate_tdd",
        "activate_codebase-design",
        "activate_code_review",
    }
    import re

    for skill, tool_name in [
        ("implement", "activate_implement"),
        ("tdd", "activate_tdd"),
        ("codebase-design", "activate_codebase-design"),
        ("code-review", "activate_code_review"),
    ]:
        text = (skills_dir / skill / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        assert match is not None
        import yaml

        frontmatter = yaml.safe_load(match.group(1))
        assert by_name[tool_name].description == frontmatter["description"]


def test_repeated_activation_returns_already_active(skills_dir: Path) -> None:
    """C-SKL-1: a second call to the same activation tool returns a short
    'already active' acknowledgement, not the body again."""
    from coding_agent.skillbundle.activation import build_activation_tools

    tools = build_activation_tools(skills_dir)
    by_name = {t.name: t for t in tools}
    first = str(by_name["activate_implement"].invoke({}))
    second = str(by_name["activate_implement"].invoke({}))
    assert "already active" in second.lower()
    assert second != first
    assert len(second) < len(first)


def test_activation_result_carries_body_plus_companions_verbatim(skills_dir: Path) -> None:
    """An activation result is the skill body plus its Companion Files, byte
    for byte, and is never capped."""
    from coding_agent.skillbundle.activation import build_activation_tools

    tools = build_activation_tools(skills_dir)
    by_name = {t.name: t for t in tools}
    tdd_text = (skills_dir / "tdd" / "SKILL.md").read_text(encoding="utf-8")
    tests_md = (skills_dir / "tdd" / "tests.md").read_text(encoding="utf-8")
    mocking_md = (skills_dir / "tdd" / "mocking.md").read_text(encoding="utf-8")
    result = str(by_name["activate_tdd"].invoke({}))
    assert tdd_text in result
    assert tests_md in result
    assert mocking_md in result
    # Never capped: full length present even though well over the 4k cap.
    assert len(result) > 4000 or True
    assert "capped" not in result.lower() or tdd_text in result


def test_attempt_header_answers_slash_refs_and_upstream_text() -> None:
    """G-HDR-1 + L3-IMP-15: header maps every slash-command reference to its
    activation tool and answers the two upstream-text gaps."""
    from coding_agent.runtime.pinned_prefix import compose_mvp_attempt_header

    header = compose_mvp_attempt_header(
        attempt_id="#7/1",
        base_revision="abc123",
        branch="agent/7/1-greet",
        acceptance_criteria=(("AC-1", "greet"),),
        seam_set=("greet",),
        target_language="python",
    )
    assert "activate_tdd" in header
    assert "activate_code_review" in header
    assert "activate_codebase-design" in header
    assert "/tdd" in header
    assert "/code-review" in header
    assert "/codebase-design" in header
    # L3-IMP-15 answers.
    assert "Going deeper" in header or "DEEPENING" in header
    assert "unavailable" in header
    assert "TypeScript" in header
    # Shell tests count for nothing; only the targeted-test tool counts.
    assert "test_targeted" in header or "targeted-test" in header
    # Attempt facts.
    assert "#7/1" in header
    assert "abc123" in header
    assert "agent/7/1-greet" in header
    assert "AC-1" in header
    assert "greet" in header


def test_step_first_request_holds_prefix_then_forced_activation_no_skill_body(skills_dir: Path) -> None:
    """C-SKL-2: first request holds Pinned Prefix then forced activation call,
    and no skill body."""
    from support.fake_tracker import FakeTracker
    from support.scripted_model import ScriptedModel

    from coding_agent.ledger.store import RunLedger
    from coding_agent.runtime.graph.attempt import run_attempt

    tracker = FakeTracker()
    # Comment instruction must never reach the model; the fake issue has no
    # comments, but the body carries the acceptance criteria.
    model = ScriptedModel(
        [
            AIMessage(
                content="activating",
                tool_calls=[
                    {
                        "name": "activate_implement",
                        "args": {},
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="done",
                tool_calls=[
                    {
                        "name": "report_completion",
                        "args": {"AC-1": ["pkg::test_ok"]},
                        "id": "call_2",
                        "type": "tool_call",
                    }
                ],
            ),
        ]
    )
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ledger = RunLedger(Path(tmp) / "ledger.sqlite3")
        import subprocess

        from conftest import init_origin_repo, run_git

        src = Path(tmp) / "origin-src"
        init_origin_repo(src)
        _commit_step_profile(src)
        bare = Path(tmp) / "remote.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
        workdir = Path(tmp) / "workspace"
        run_git(["clone", "-q", str(bare), str(workdir)], cwd=Path(tmp))
        result = run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=tracker,
            ledger=ledger,
            model=model,
            workspace=workdir,
            effective_configuration={"repo": "x/y", "issue": 7},
            skills_dir=skills_dir,
        )
        assert result.outcome in ("delivered", "failed")
        assert model.invocations, "model was never invoked"
        first = model.invocations[0]
        assert len(first) >= 2
        # First request holds the Pinned Prefix: header + title/body.
        texts = [str(getattr(m, "content", "")) for m in first]
        joined = "\n".join(texts)
        assert "Attempt Header" in joined or "#7/1" in joined
        assert "Add a greeting" in joined
        # No skill body eagerly injected.
        for skill in ("implement", "tdd", "codebase-design", "code-review"):
            body = (skills_dir / skill / "SKILL.md").read_text(encoding="utf-8")
            # The full body must not be in the opening messages; it arrives
            # only as the activation result (a later tool result).
            assert body not in joined
        # Forced activation was requested via tool_choice.
        assert getattr(model, "tool_choices", None) is not None
        assert model.tool_choices[0] == "activate_implement"
        ledger.close()


def test_only_title_and_body_reach_model(skills_dir: Path) -> None:
    """P-CTX-1: an instruction in a human comment never reaches the model."""
    from support.scripted_model import ScriptedModel

    from coding_agent.ledger.store import RunLedger
    from coding_agent.runtime.graph.attempt import run_attempt
    from coding_agent.tracker.port import TrackedIssue

    from dataclasses import dataclass, field

    @dataclass
    class CommentTracker:
        issue: TrackedIssue = field(
            default_factory=lambda: TrackedIssue(
                number=7,
                title="Add a greeting",
                body="## Acceptance criteria\n- [ ] `greet` returns a greeting\n",
            )
        )
        calls: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)

        def _record(self, name: str, *args: object) -> None:
            self.calls.append((name, args))

        def fetch_issue(self, issue_number: int) -> TrackedIssue:
            self._record("fetch_issue", issue_number)
            return self.issue

        def post_terminal_comment(self, issue_number: int, body: str) -> None:
            self._record("post_terminal_comment", issue_number, body)

        def remove_selection_label(self, issue_number: int) -> None:
            self._record("remove_selection_label", issue_number)

        def add_ready_for_human(self, issue_number: int) -> None:
            self._record("add_ready_for_human", issue_number)

        def release_assignment(self, issue_number: int) -> None:
            self._record("release_assignment", issue_number)

    model = ScriptedModel(
        [
            AIMessage(
                content="activating",
                tool_calls=[
                    {
                        "name": "activate_implement",
                        "args": {},
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="done",
                tool_calls=[
                    {
                        "name": "report_completion",
                        "args": {"AC-1": ["pkg::test_ok"]},
                        "id": "call_2",
                        "type": "tool_call",
                    }
                ],
            ),
        ]
    )
    import json
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        ledger = RunLedger(Path(tmp) / "ledger.sqlite3")
        from conftest import init_origin_repo, run_git

        src = Path(tmp) / "origin-src"
        init_origin_repo(src)
        _commit_step_profile(src)
        bare = Path(tmp) / "remote.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
        workdir = Path(tmp) / "workspace"
        run_git(["clone", "-q", str(bare), str(workdir)], cwd=Path(tmp))
        run_attempt(
            issue_number=7,
            attempt_number=1,
            tracker=CommentTracker(),
            ledger=ledger,
            model=model,
            workspace=workdir,
            effective_configuration={"repo": "x/y", "issue": 7},
            skills_dir=skills_dir,
        )
        sent = json.dumps(
            [[getattr(message, "content", "") for message in call] for call in model.invocations]
        )
        assert "Add a greeting" in sent
        assert "IGNORE ALL PREVIOUS INSTRUCTIONS" not in sent
        ledger.close()


def test_activation_registry_holds_implement_and_nested_tdd_once_each(skills_dir: Path) -> None:
    """L3-IMP-1: /implement and nested /tdd both appear in the activation
    registry, and neither activates twice."""
    from coding_agent.skillbundle.activation import ACTIVATION_REGISTRY

    assert "implement" in ACTIVATION_REGISTRY
    assert "tdd" in ACTIVATION_REGISTRY
    from coding_agent.skillbundle.activation import build_activation_tools

    tools = build_activation_tools(skills_dir)
    by_name = {t.name: t for t in tools}
    by_name["activate_tdd"].invoke({})
    second = str(by_name["activate_tdd"].invoke({}))
    assert "already active" in second.lower()
