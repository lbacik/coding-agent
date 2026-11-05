"""The declared-completion experiment's frozen fixtures and hidden oracles.

Issue #201, part of #69. These tests hold the properties the fixtures must
keep before any paid run, against the vendored Base Revision tree in
``fixtures/exp-69/base`` (the tree tagged in the sandbox Target Repository):

- every Target Issue passes readiness: its Acceptance Criteria enumerate and
  a Seam Set derives, against the Base Revision's Project Profile;
- the Base Revision's own suite is green, so no run ends ``red-baseline``;
- each change fixture's oracle fails at the Base Revision and against a
  deliberately partial solution, and passes against the hand-written
  reference solution;
- the already-satisfied fixture's oracle passes at the Base Revision and
  fails against each deliberately unwanted edit;
- oracle files live outside the Base Revision tree, so no Attempt workspace
  ever contains them;
- the recorded issue-body digests match the frozen bodies, and the rendered
  protocol ``fixtures`` section validates with the harness's own parser.

Each oracle is run exactly as the harness runs it: the tree is copied without
``.git``, the oracle files are materialised into the copy, and the oracle
command runs there (``coding_agent.experiment.harness``).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from coding_agent.experiment.harness import copy_tree_without_git, run_oracle
from coding_agent.experiment.protocol import issue_body_digest, parse_protocol
from coding_agent.runtime.acceptance import parse_acceptance_criteria
from coding_agent.runtime.readiness import (
    ReadinessReady,
    default_toolchain_matrix,
    resolve_readiness,
)
from coding_agent.runtime.seam import derive_seam_set
from conftest import run_git

REPO_ROOT = Path(__file__).resolve().parents[1]
EXP_DIR = REPO_ROOT / "fixtures" / "exp-69"
BASE_DIR = EXP_DIR / "base"
MANIFEST_PATH = EXP_DIR / "manifest.yaml"
RENDER_SCRIPT = REPO_ROOT / "scripts" / "exp-69-protocol-fixtures.py"


def _manifest() -> dict[str, Any]:
    loaded = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _fixtures() -> list[dict[str, Any]]:
    fixtures = _manifest()["fixtures"]
    assert isinstance(fixtures, list)
    return fixtures


def _fixture(fixture_id: str) -> dict[str, Any]:
    for fixture in _fixtures():
        if fixture["id"] == fixture_id:
            return fixture
    raise KeyError(fixture_id)


def _ids(kind: str | None = None) -> list[str]:
    return [
        str(fixture["id"])
        for fixture in _fixtures()
        if kind is None or fixture["kind"] == kind
    ]


def _overlay(destination: Path, overlay: Path) -> None:
    for source in sorted(path for path in overlay.rglob("*") if path.is_file()):
        target = destination / source.relative_to(overlay)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _tree(tmp_path: Path, *overlays: Path) -> Path:
    """A final working tree: the Base Revision plus each overlay in turn."""
    tree = tmp_path / "tree"
    shutil.copytree(BASE_DIR, tree)
    for overlay in overlays:
        _overlay(tree, overlay)
    return tree


def _run_fixture_oracle(fixture_id: str, tree: Path, tmp_path: Path) -> int | None:
    """Run the fixture's oracle the way the harness does, returning its exit."""
    fixture = _fixture(fixture_id)
    snapshot = tmp_path / "snapshot"
    copy_tree_without_git(tree, snapshot)
    for published, source in fixture["oracle"]["files"].items():
        destination = snapshot / published
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            (EXP_DIR / source).read_text(encoding="utf-8"), encoding="utf-8"
        )
    result = run_oracle(tuple(fixture["oracle"]["command"]), snapshot, 120.0)
    assert not result.timed_out, result
    print(result.stdout, result.stderr)
    return result.exit_code


def _body(fixture_id: str) -> str:
    return (EXP_DIR / str(_fixture(fixture_id)["body"])).read_text(encoding="utf-8")


class _NoServices:
    def probe(self, host: str, port: int, *, timeout: float) -> bool:
        raise AssertionError("the Base Revision declares no services")


def test_manifest_declares_three_or_four_fixtures_of_the_required_kinds() -> None:
    kinds = [fixture["kind"] for fixture in _fixtures()]
    assert 3 <= len(kinds) <= 4
    assert kinds.count("silent-stop") == 1
    assert kinds.count("already-satisfied") == 1
    assert 1 <= kinds.count("change") <= 2


@pytest.mark.parametrize("fixture_id", _ids())
def test_target_issue_passes_readiness_at_the_base_revision(
    fixture_id: str, tmp_path: Path
) -> None:
    fixture = _fixture(fixture_id)
    body = _body(fixture_id)
    workspace = _tree(tmp_path)
    run_git(["init", "-q", "-b", "main"], workspace)
    run_git(["add", "-A"], workspace)
    run_git(
        ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "base"],
        workspace,
    )

    decision = resolve_readiness(
        workspace,
        fixture["title"],
        body,
        default_toolchain_matrix(),
        {},
        _NoServices(),
    )

    assert isinstance(decision, ReadinessReady), decision
    assert len(decision.acceptance_criteria) == fixture["criteria_count"]
    assert len(parse_acceptance_criteria(body)) == fixture["criteria_count"]
    assert derive_seam_set(fixture["title"], body)


def test_base_revision_suite_is_green(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=tree,
        env={"PYTHONPATH": str(tree / "src"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


@pytest.mark.parametrize("fixture_id", _ids("silent-stop") + _ids("change"))
def test_change_oracle_fails_at_the_base_revision(
    fixture_id: str, tmp_path: Path
) -> None:
    assert _run_fixture_oracle(fixture_id, _tree(tmp_path), tmp_path) == 1


@pytest.mark.parametrize("fixture_id", _ids("silent-stop") + _ids("change"))
def test_change_oracle_passes_against_the_reference_solution(
    fixture_id: str, tmp_path: Path
) -> None:
    reference = EXP_DIR / _fixture(fixture_id)["reference"]
    assert _run_fixture_oracle(fixture_id, _tree(tmp_path, reference), tmp_path) == 0


def _overlay_cases(key: str) -> list[tuple[str, str]]:
    """``(fixture id, overlay path)`` for every overlay listed under ``key``."""
    return [
        (str(fixture["id"]), str(overlay))
        for fixture in _fixtures()
        for overlay in fixture.get(key, [])
    ]


@pytest.mark.parametrize(("fixture_id", "partial"), _overlay_cases("partial"))
def test_change_oracle_fails_against_a_partial_solution(
    fixture_id: str, partial: str, tmp_path: Path
) -> None:
    assert _run_fixture_oracle(fixture_id, _tree(tmp_path, EXP_DIR / partial), tmp_path) == 1


@pytest.mark.parametrize("fixture_id", _ids("already-satisfied"))
def test_already_satisfied_oracle_passes_at_the_base_revision(
    fixture_id: str, tmp_path: Path
) -> None:
    assert _run_fixture_oracle(fixture_id, _tree(tmp_path), tmp_path) == 0


@pytest.mark.parametrize(("fixture_id", "unwanted"), _overlay_cases("unwanted"))
def test_already_satisfied_oracle_fails_against_an_unwanted_edit(
    fixture_id: str, unwanted: str, tmp_path: Path
) -> None:
    tree = _tree(tmp_path, EXP_DIR / unwanted)
    assert _run_fixture_oracle(fixture_id, tree, tmp_path) == 1


def test_already_satisfied_oracle_fails_when_a_base_file_is_deleted(
    tmp_path: Path,
) -> None:
    (fixture_id,) = _ids("already-satisfied")
    tree = _tree(tmp_path)
    (tree / "tests" / "test_extra.py").unlink()
    assert _run_fixture_oracle(fixture_id, tree, tmp_path) == 1


def test_already_satisfied_oracle_ignores_bootstrap_and_cache_artifacts(
    tmp_path: Path,
) -> None:
    (fixture_id,) = _ids("already-satisfied")
    tree = _tree(tmp_path)
    for relative in (
        ".venv/pyvenv.cfg",
        "src/l2_fixture_py/__pycache__/greeting.cpython-313.pyc",
        ".pytest_cache/README.md",
    ):
        (tree / relative).parent.mkdir(parents=True, exist_ok=True)
        (tree / relative).write_text("artifact\n", encoding="utf-8")
    assert _run_fixture_oracle(fixture_id, tree, tmp_path) == 0


def test_oracle_files_live_outside_the_base_revision_tree() -> None:
    for fixture in _fixtures():
        for published, source in fixture["oracle"]["files"].items():
            source_path = (EXP_DIR / source).resolve()
            assert BASE_DIR.resolve() not in source_path.parents
            assert not (BASE_DIR / published).exists()
    assert not any("oracle" in path.name for path in BASE_DIR.rglob("*"))


def test_recorded_base_tree_object_is_the_vendored_tree(tmp_path: Path) -> None:
    """The tagged commit's tree, recorded from the sandbox, is ``base/``."""
    tree = _tree(tmp_path)
    run_git(["init", "-q"], tree)
    run_git(["add", "-A"], tree)
    written = run_git(["write-tree"], tree)
    assert _manifest()["base"]["tree_object"] == written


def test_recorded_body_digests_match_the_frozen_bodies() -> None:
    for fixture in _fixtures():
        assert fixture["issue_body_digest"] == issue_body_digest(
            _body(str(fixture["id"]))
        ), fixture["id"]


def test_rendered_protocol_fixtures_validate_with_the_harness_parser() -> None:
    rendered = subprocess.run(
        [sys.executable, str(RENDER_SCRIPT)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    fixtures = yaml.safe_load(rendered)["fixtures"]
    protocol = parse_protocol(
        {
            "arms": ["treatment", "control"],
            "fixtures": fixtures,
            "runs_per_cell": 1,
            "order": [
                {"arm": arm, "fixture": fixture["id"], "run": 1}
                for fixture in fixtures
                for arm in ("treatment", "control")
            ],
            "per_attempt_cost_usd": 5,
            "overall_cap_usd": 150,
            "pinned_model": {"provider": "p", "model": "m", "effort": "e"},
        }
    )
    base_commit = _manifest()["base"]["commit"]
    assert [fixture.id for fixture in protocol.fixtures] == _ids()
    for parsed in protocol.fixtures:
        declared = _fixture(parsed.id)
        assert parsed.issue_number == declared["issue_number"]
        assert parsed.base_revision == base_commit
        assert parsed.oracle == tuple(declared["oracle"]["command"])
        for published, source in declared["oracle"]["files"].items():
            assert parsed.oracle_files[published] == (EXP_DIR / source).read_text(
                encoding="utf-8"
            )
