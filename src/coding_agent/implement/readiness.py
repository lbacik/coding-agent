from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from coding_agent.profile.readiness import (
    NeedsClarification,
    ProfileSources,
    Ready,
    UnsupportedEnvironment,
    evaluate_readiness,
)
from coding_agent.profile.schema import ProjectProfile
from coding_agent.profile.services import ServiceProber
from coding_agent.profile.substitution import render_command
from coding_agent.profile.toolchain import SupportedToolchainMatrix
from coding_agent.validate.harness import CommandContext, run_test_all
from coding_agent.validate.results import RawCommandResult, classify
from coding_agent.validate.runner import CommandRunner

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

    @property
    def runnable(self) -> bool:
        return self.classification in {"ready", "runnable-red"}


def prepare_environment(
    workspace_path: Path,
    evidence_dir: Path,
    matrix: SupportedToolchainMatrix,
    service_env: Mapping[str, str],
    service_prober: ServiceProber,
    runner: CommandRunner,
) -> ReadinessReport:
    """Resolve and observe one Attempt's environment before a model opens.

    A missing `docs/agents/project-profile.yml` is not, on its own, an
    environment failure: contract §7 backs the canonical file with a prose
    fallback chain (`AGENTS.md`/`CLAUDE.md` -> `docs/agents/*` -> project
    configuration -> issue body), and `evaluate_readiness` is the single
    place that precedence between the two is decided (L2-15, L2-16). So an
    absent file is passed through to it as `profile_yaml_text=None` rather
    than being classified here — only a read failure for a file that does
    exist (permissions, a directory in its place, ...) is a readiness
    failure this function reports on its own.
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
    if isinstance(resolution, NeedsClarification):
        return ReadinessReport("unsupported-environment", "; ".join(resolution.missing))

    assert isinstance(resolution, Ready)
    profile = resolution.profile
    artifact_dir = evidence_dir / "readiness"
    if artifact_dir.exists():
        shutil.rmtree(artifact_dir)
    artifact_dir.mkdir(parents=True)
    working_directory = workspace_path / profile.working_directory

    bootstrap_command = render_command(profile.bootstrap, evidence_dir=artifact_dir)
    bootstrap_executed = runner.run(bootstrap_command, cwd=working_directory)
    bootstrap = RawCommandResult(
        name="bootstrap",
        command=bootstrap_executed.command,
        exit_code=bootstrap_executed.exit_code,
        junit=None,
        evidence_declared=False,
    )
    if bootstrap.exit_code != 0:
        return ReadinessReport(
            "bootstrap-failed",
            f"bootstrap exited {bootstrap.exit_code}",
            profile=profile,
            bootstrap=bootstrap,
            artifact_dir=artifact_dir,
        )

    test_all = run_test_all(profile, CommandContext(runner, working_directory, artifact_dir))
    if classify(test_all) == "missing-evidence":
        return ReadinessReport(
            "invalid-readiness-evidence",
            "test_all did not produce valid JUnit XML with at least one executed test",
            profile=profile,
            bootstrap=bootstrap,
            test_all=test_all,
            artifact_dir=artifact_dir,
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
        )
    return ReadinessReport(
        "ready",
        "bootstrap and test_all passed",
        profile=profile,
        bootstrap=bootstrap,
        test_all=test_all,
        artifact_dir=artifact_dir,
    )
