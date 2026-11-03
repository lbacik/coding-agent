"""One authoritative run of the Validation Contract against the Delivery Snapshot.

Contract: ``docs/contract/mvp-runtime-contract.md`` §12 (``final_checks``
runs only for ``verified completion``, the one Implementation Outcome that
can reach a pull request. It is one authoritative run of ``test_all`` and
every check against the Delivery Snapshot, outside the model's tool loop,
with no remediation round and no retry. Its result is Validation Evidence
in the Run Ledger: the commands, their exit codes, how many tests actually
executed and the identifier of every individual failure. Green means every
command exits zero, ``test_all`` executes at least one test and reports no
failure, and every check passes; ``checks: none`` satisfies the checks.
Exit zero is necessary, not sufficient: a run executing no tests is missing
evidence, and missing evidence is red. There is no Baseline Failure
exclusion. After a green run the tree must be clean: any modified or
untracked file left by the checks is a dirty tree. Validation uses the
Validation Contract pinned at the Base Revision, even where the candidate
edits ``project-profile.yml``; it covers only the profile's declared
working area. Both facts are recorded for disclosure in the pull-request
body) and §7 (the pinned profile).

MVP row ids: C-PUB-1, P-DEL-2, P-DEL-8, L2-3, L2-17, L2-18.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from coding_agent.profile.schema import ProjectProfile
from coding_agent.runtime.git import read_file_at_revision
from coding_agent.runtime.readiness import PROFILE_RELPATH
from coding_agent.validate.harness import CommandContext, run_validation_contract
from coding_agent.validate.results import RawCommandResult
from coding_agent.validate.runner import CommandRunner

FINAL_CHECKS_KIND = "final_checks"
"""Run Ledger event kind for the final-checks Validation Evidence (§12)."""

FINAL_CHECKS_EVIDENCE_SUBDIR = "final-checks"
"""Per-Attempt evidence subdirectory the final run owns (§12, L2-17).

``readiness`` owns ``readiness`` and the step tools own the evidence root;
final checks render every ``{evidence_dir}`` placeholder under this
subdirectory so one authoritative run never reuses another phase's files.
"""


@dataclass(frozen=True)
class FinalChecksReport:
    """What the one authoritative final run did against the Delivery Snapshot.

    ``results`` carry every command's exit code, executed-test count and
    individual failure identifiers (the Validation Evidence of §12);
    ``green`` is the §12 verdict; ``dirty_files`` are the modified or
    untracked paths left behind (a dirty tree, P-DEL-8); ``working_area`` is
    the profile's declared working area the run covered (L2-18);
    ``base_revision`` pins which Validation Contract ran and
    ``profile_changed`` records whether the candidate edited the profile
    anyway (L2-17) — both facts for disclosure in the pull-request body.
    """

    results: tuple[RawCommandResult, ...]
    green: bool
    dirty_files: tuple[str, ...]
    working_area: str
    base_revision: str
    profile_changed: bool


def run_final_checks(
    profile: ProjectProfile,
    runner: CommandRunner,
    working_directory: Path,
    evidence_dir: Path,
) -> tuple[RawCommandResult, ...]:
    """Run ``test_all`` and every check once each against the current tree.

    The caller holds the tree at the Delivery Snapshot (``delivery_snapshot``
    committed and pushed it first, [ADR 0002](../adr/0002-commit-and-push-before-review.md));
    this function only runs the pinned contract's commands and reads the
    evidence they declare. Exactly one run per command: no retry and no
    remediation (C-PUB-1). ``bootstrap`` never runs here: it ran at
    ``baseline``, before any model call.
    """
    evidence_dir.mkdir(parents=True, exist_ok=True)
    context = CommandContext(runner, working_directory, evidence_dir)
    return run_validation_contract(profile, context)


def _command_green(result: RawCommandResult) -> bool:
    """Whether one final command passes (§12).

    Exit zero is necessary but not sufficient: a command whose declared
    evidence names zero executed tests, or whose declared evidence could not
    be read at all, is missing evidence and therefore red (L2-3) — for
    ``test_all`` and for every check alike. A check declaring no evidence is
    judged by its exit code alone (``checks: none`` satisfies the checks).
    """
    if result.exit_code != 0:
        return False
    if not result.evidence_declared:
        return True
    if result.junit is None:
        return False
    if result.junit.executed == 0:
        return False
    if result.junit.failure_ids:
        return False
    return True


def final_checks_green(results: tuple[RawCommandResult, ...]) -> bool:
    """The §12 verdict over one final run: green only with no red anywhere.

    Every command must pass, and ``test_all`` must be among them. There is
    no Baseline Failure exclusion: any failure is red.
    """
    if not any(result.name == "test_all" for result in results):
        return False
    return all(_command_green(result) for result in results)


def dirty_working_tree_files(workspace_path: Path) -> tuple[str, ...]:
    """Modified or untracked paths left in the tree after the final run.

    Contract: ``docs/contract/mvp-runtime-contract.md`` §12 (after a green
    run the tree must be clean; any modified or untracked file left by the
    checks is a dirty tree, P-DEL-8). Best effort like the handoff facts: an
    unreadable tree contributes no files rather than failing the Attempt.
    """
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ()
    if completed.returncode != 0:
        return ()
    files: list[str] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        rest = line[3:] if len(line) > 3 else line.strip()
        for part in rest.split(" -> "):
            candidate = part.strip().strip('"')
            if candidate:
                files.append(candidate)
    return tuple(sorted(set(files)))


def candidate_changed_profile(
    workspace_path: Path, *, base_revision: str, head_revision: str
) -> bool:
    """Whether the candidate changed the profile after it was pinned (L2-17).

    Compares the profile bytes at the Delivery Snapshot's commit against the
    pinned Base Revision: any difference — edited, added or removed — counts
    as changed and is recorded for disclosure, while validation itself
    always ran the pinned contract.
    """
    base_text = read_file_at_revision(workspace_path, base_revision, PROFILE_RELPATH)
    head_text = read_file_at_revision(workspace_path, head_revision, PROFILE_RELPATH)
    return base_text != head_text


def final_checks_evidence_payload(report: FinalChecksReport) -> dict[str, object]:
    """Validation Evidence for the Run Ledger (§12, L2-1 by analogy).

    Every command with its exit code, executed-test count and individual
    failure identifiers, plus the green verdict, the dirty-tree files, the
    covered working area, the pinned Base Revision and whether the
    candidate edited the profile — the facts the pull-request body
    discloses (rendering is a later M4 ticket).
    """

    def entry(result: RawCommandResult) -> dict[str, object]:
        return {
            "name": result.name,
            "command": result.command,
            "exit_code": result.exit_code,
            "executed": result.junit.executed if result.junit is not None else None,
            "failure_ids": (
                sorted(result.junit.failure_ids)
                if result.evidence_declared and result.junit is not None
                else None
            ),
        }

    return {
        "green": report.green,
        "contract_pinned_at_base_revision": report.base_revision,
        "profile_changed_in_candidate": report.profile_changed,
        "working_area": report.working_area,
        "dirty_files": list(report.dirty_files),
        "commands": [entry(result) for result in report.results],
    }
