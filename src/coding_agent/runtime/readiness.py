"""Readiness: workspace rebuild plus Readiness Facts, before any model call.

Contract: ``docs/contract/mvp-runtime-contract.md`` §7 (every Attempt
builds its workspace deterministically — ``reset --hard`` to its start
commit, then ``clean -fdx`` — reads the Project Profile at the Base
Revision, pins it as the Validation Contract, and resolves every Readiness
Fact from the profile and the Target Issue alone; no prose fallback; a
missing or invalid fact ends the Attempt ``failed`` with no model call),
§11 (every setup command runs in its own process group with a per-command
timeout; a timeout kills the whole group and is classified the way this
node already classifies a failure) and
[ADR 0008](../../docs/adr/0008-the-tdd-seam-gate-is-a-readiness-fact.md)
(the Seam Set is a Readiness Fact derived from the Target Issue alone).

MVP row ids: P-RDY-1, P-RDY-2, P-RDY-3, C-RDY-1, C-RDY-2, C-WSP-1, L2-12,
L2-13, L2-14, L3-CLR-20, L3-IMP-11, P-CMD-1.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from coding_agent.profile.parser import InvalidProfile, MissingReadinessFacts
from coding_agent.profile.readiness import (
    ProfileSources,
    Ready,
    UnsupportedEnvironment,
    evaluate_readiness,
)
from coding_agent.profile.schema import ProjectProfile
from coding_agent.profile.services import ServiceProber
from coding_agent.profile.substitution import render_command
from coding_agent.profile.toolchain import SupportedToolchainMatrix, load_toolchain_matrix
from coding_agent.runtime.acceptance import AcceptanceCriterion, parse_acceptance_criteria
from coding_agent.runtime.git import (
    current_branch,
    head_revision,
    read_file_at_revision,
    rebuild_workspace,
)
from coding_agent.runtime.seam import derive_seam_set
from coding_agent.validate.harness import CommandContext, run_test_all
from coding_agent.validate.junit import MalformedJUnitReport, parse_junit_xml
from coding_agent.validate.results import RawCommandResult, classify
from coding_agent.validate.runner import CommandRunner

PROFILE_RELPATH = "docs/agents/project-profile.yml"


def default_toolchain_matrix() -> SupportedToolchainMatrix:
    """The image's Supported Toolchain Matrix for Attempts without explicit
    configuration (contract §7, [ADR 0001](../../docs/adr/0001-pinned-toolchain-matrix.md)).

    MVP row ids: L2-12.
    """
    return load_toolchain_matrix(
        {
            "schema": 1,
            "toolchains": {
                "python": {
                    "version": "3.13.9",
                    "package_manager": {"name": "uv", "version": "0.5.29"},
                },
                "php": {
                    "version": "8.4.25",
                    "package_manager": {"name": "composer", "version": "2.8.9"},
                },
                "node": {
                    "version": "22.23.2",
                    "package_manager": {"name": "pnpm", "version": "9.15.4"},
                },
            },
        }
    )

ReadinessClassification = Literal[
    "ready",
    "runnable-red",
    "unsupported-environment",
    "bootstrap-failed",
    "invalid-readiness-evidence",
]


@dataclass(frozen=True)
class ReadinessReport:
    """Observed pre-model state for one isolated Attempt checkout.

    This records preparation evidence separately from later Validation
    Evidence. A later diagnostic boundary can consume the command artifacts
    and baseline failures without mistaking this one-time proof for a
    Delivery Snapshot validation run.
    """

    classification: ReadinessClassification
    detail: str
    profile: ProjectProfile | None = None
    bootstrap: RawCommandResult | None = None
    test_all: RawCommandResult | None = None
    artifact_dir: Path | None = None
    baseline_failures: frozenset[str] = frozenset()
    bootstrap_stdout_path: Path | None = None
    bootstrap_stderr_path: Path | None = None
    test_all_stdout_path: Path | None = None
    test_all_stderr_path: Path | None = None

    @property
    def runnable(self) -> bool:
        return self.classification in {"ready", "runnable-red"}


@dataclass(frozen=True)
class ReadinessReady:
    """Every Readiness Fact resolved: the pinned Validation Contract, the
    Acceptance Criteria, the Seam Set, and the revision they were read at."""

    profile: ProjectProfile
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    seam_set: tuple[str, ...]
    base_revision: str
    base_branch: str


@dataclass(frozen=True)
class ReadinessRefusal:
    """A terminal Readiness refusal: ``failed`` with the reason that names
    exactly what is missing or wrong, routed straight to ``terminal_writes``
    with no model call."""

    reason: str


def _refuse_missing(missing: tuple[str, ...]) -> ReadinessRefusal:
    return ReadinessRefusal(reason=f"missing-readiness-fact: {'; '.join(missing)}")


def resolve_readiness(
    workspace_path: Path,
    issue_title: str,
    issue_body: str,
    matrix: SupportedToolchainMatrix,
    service_env: Mapping[str, str],
    service_prober: ServiceProber,
) -> ReadinessReady | ReadinessRefusal:
    """Rebuild the workspace and resolve every Readiness Fact, model-free.

    Builds the workspace deterministically from the local history
    (``reset --hard`` to the start commit, then ``clean -fdx``), reads the
    Project Profile at the Base Revision, pins it as the Validation
    Contract, and resolves the Acceptance Criteria and the Seam Set from
    the Target Issue alone. ``AGENTS.md``, ``CLAUDE.md`` and
    ``docs/agents/*`` are never read for a Readiness Fact.
    """
    base_revision = head_revision(workspace_path)
    rebuild_workspace(workspace_path, ref=base_revision)
    base_branch = current_branch(workspace_path)

    profile_text = read_file_at_revision(workspace_path, base_revision, PROFILE_RELPATH)
    resolution = evaluate_readiness(
        ProfileSources(
            profile_yaml_text=profile_text,
            sources_searched=(f"{base_revision}:{PROFILE_RELPATH}",),
        ),
        matrix,
        service_env,
        service_prober,
    )
    if isinstance(resolution, UnsupportedEnvironment):
        return ReadinessRefusal(reason=f"unsupported-environment: {resolution.detail}")
    if isinstance(resolution, InvalidProfile):
        return ReadinessRefusal(reason=f"invalid-profile: {resolution.detail}")
    if isinstance(resolution, MissingReadinessFacts):
        return _refuse_missing(resolution.missing)
    assert isinstance(resolution, Ready)
    profile = resolution.profile

    criteria = parse_acceptance_criteria(issue_body)
    if not criteria:
        return _refuse_missing(
            ("acceptance-criteria: the Target Issue declares no Acceptance Criteria",)
        )

    seam_set = derive_seam_set(issue_title, issue_body)
    if not seam_set:
        return _refuse_missing(
            ("seam-set: the Target Issue names no public symbol, path or endpoint under test",)
        )

    return ReadinessReady(
        profile=profile,
        acceptance_criteria=criteria,
        seam_set=seam_set,
        base_revision=base_revision,
        base_branch=base_branch,
    )


def prepare_environment(
    workspace_path: Path,
    evidence_dir: Path,
    matrix: SupportedToolchainMatrix,
    service_env: Mapping[str, str],
    service_prober: ServiceProber,
    runner: CommandRunner,
) -> ReadinessReport:
    """Resolve and observe one Attempt's environment before a model opens.

    There is no prose fallback (contract §7, C-RDY-2): only
    `docs/agents/project-profile.yml` in the workspace is read. An absent
    file is passed through to `evaluate_readiness` as
    `profile_yaml_text=None` rather than being classified here — only a read
    failure for a file that does exist (permissions, a directory in its
    place, ...) is a readiness failure this function reports on its own.
    """
    profile_path = workspace_path / "docs" / "agents" / "project-profile.yml"
    try:
        profile_text: str | None = profile_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        profile_text = None
    except OSError as exc:
        return ReadinessReport(
            "unsupported-environment",
            f"could not read Project Profile at {profile_path}: {exc}",
        )

    resolution = evaluate_readiness(
        ProfileSources(profile_yaml_text=profile_text, sources_searched=(str(profile_path),)),
        matrix,
        service_env,
        service_prober,
    )
    if isinstance(resolution, UnsupportedEnvironment):
        return ReadinessReport("unsupported-environment", resolution.detail)
    if isinstance(resolution, (MissingReadinessFacts, InvalidProfile)):
        detail = (
            "; ".join(resolution.missing)
            if isinstance(resolution, MissingReadinessFacts)
            else resolution.detail
        )
        return ReadinessReport("unsupported-environment", detail)

    assert isinstance(resolution, Ready)
    profile = resolution.profile
    artifact_dir = evidence_dir / "readiness"
    if artifact_dir.exists():
        shutil.rmtree(artifact_dir)
    artifact_dir.mkdir(parents=True)
    working_directory = workspace_path / profile.working_directory

    bootstrap_command = render_command(profile.bootstrap, evidence_dir=artifact_dir)
    bootstrap_executed = runner.run(bootstrap_command, cwd=working_directory)

    # Persist bootstrap artifacts
    bootstrap_stdout_path = artifact_dir / "bootstrap.stdout"
    bootstrap_stderr_path = artifact_dir / "bootstrap.stderr"
    bootstrap_stdout_path.write_text(bootstrap_executed.stdout, encoding="utf-8")
    bootstrap_stderr_path.write_text(bootstrap_executed.stderr, encoding="utf-8")

    bootstrap = RawCommandResult(
        name="bootstrap",
        command=bootstrap_executed.command,
        exit_code=bootstrap_executed.exit_code,
        junit=None,
        evidence_declared=False,
        stdout=bootstrap_executed.stdout,
        stderr=bootstrap_executed.stderr,
        timed_out=bootstrap_executed.timed_out,
    )
    if bootstrap.exit_code != 0:
        return ReadinessReport(
            "bootstrap-failed",
            f"bootstrap exited {bootstrap.exit_code}",
            profile=profile,
            bootstrap=bootstrap,
            artifact_dir=artifact_dir,
            bootstrap_stdout_path=bootstrap_stdout_path,
            bootstrap_stderr_path=bootstrap_stderr_path,
        )

    # Run test_all and capture stdout/stderr artifacts
    test_all_command = render_command(profile.test_all, evidence_dir=artifact_dir)
    test_all_executed = runner.run(test_all_command, cwd=working_directory)

    # Persist test_all artifacts
    test_all_stdout_path = artifact_dir / "test_all.stdout"
    test_all_stderr_path = artifact_dir / "test_all.stderr"
    test_all_stdout_path.write_text(test_all_executed.stdout, encoding="utf-8")
    test_all_stderr_path.write_text(test_all_executed.stderr, encoding="utf-8")

    # Parse JUnit evidence if declared
    junit = None
    if profile.evidence.test_all is not None:
        evidence_path = Path(render_command(profile.evidence.test_all, evidence_dir=artifact_dir))
        try:
            junit = parse_junit_xml(evidence_path.read_text(encoding="utf-8"))
        except (OSError, MalformedJUnitReport):
            junit = None

    test_all = RawCommandResult(
        name="test_all",
        command=test_all_executed.command,
        exit_code=test_all_executed.exit_code,
        junit=junit,
        evidence_declared=profile.evidence.test_all is not None,
        stdout=test_all_executed.stdout,
        stderr=test_all_executed.stderr,
        timed_out=test_all_executed.timed_out,
    )

    if classify(test_all) == "missing-evidence":
        return ReadinessReport(
            "invalid-readiness-evidence",
            "test_all did not produce valid JUnit XML with at least one executed test",
            profile=profile,
            bootstrap=bootstrap,
            test_all=test_all,
            artifact_dir=artifact_dir,
            bootstrap_stdout_path=bootstrap_stdout_path,
            bootstrap_stderr_path=bootstrap_stderr_path,
            test_all_stdout_path=test_all_stdout_path,
            test_all_stderr_path=test_all_stderr_path,
        )

    failures = test_all.junit.failure_ids if test_all.junit is not None else frozenset()
    if test_all.exit_code != 0:
        return ReadinessReport(
            "runnable-red",
            f"test_all exited {test_all.exit_code} with {len(failures)} named baseline failure(s)",
            profile=profile,
            bootstrap=bootstrap,
            test_all=test_all,
            artifact_dir=artifact_dir,
            baseline_failures=failures,
            bootstrap_stdout_path=bootstrap_stdout_path,
            bootstrap_stderr_path=bootstrap_stderr_path,
            test_all_stdout_path=test_all_stdout_path,
            test_all_stderr_path=test_all_stderr_path,
        )
    return ReadinessReport(
        "ready",
        "bootstrap and test_all passed",
        profile=profile,
        bootstrap=bootstrap,
        test_all=test_all,
        artifact_dir=artifact_dir,
        bootstrap_stdout_path=bootstrap_stdout_path,
        bootstrap_stderr_path=bootstrap_stderr_path,
        test_all_stdout_path=test_all_stdout_path,
        test_all_stderr_path=test_all_stderr_path,
    )
