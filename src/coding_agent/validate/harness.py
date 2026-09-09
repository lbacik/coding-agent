from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from coding_agent.profile.schema import ProjectProfile
from coding_agent.profile.substitution import render_command
from coding_agent.validate.baseline import BaseRevisionRunner, ValidationEvidence, evaluate_validation
from coding_agent.validate.junit import MalformedJUnitReport, parse_junit_xml
from coding_agent.validate.results import RawCommandResult
from coding_agent.validate.runner import CommandRunner


@dataclass(frozen=True)
class _ValidationCommand:
    """One entry of the Validation Contract, as the harness needs it: the
    command template and, where declared, the evidence-path template that
    names its individual results (contract §7)."""

    command_template: str
    evidence_template: str | None

    @property
    def evidence_declared(self) -> bool:
        return self.evidence_template is not None


def _validation_contract_commands(profile: ProjectProfile) -> dict[str, _ValidationCommand]:
    """Every command the `validate` harness node covers (contract §8):
    `test_all` and every named check. `test_targeted` is the model's own
    tool during implementation (contract §4), never run by this node."""
    commands: dict[str, _ValidationCommand] = {
        "test_all": _ValidationCommand(profile.test_all, profile.evidence.test_all),
    }
    for check in profile.checks:
        commands[check.name] = _ValidationCommand(check.command, check.evidence)
    return commands


@dataclass(frozen=True)
class CommandContext:
    """Where and how one Validation Contract command run happens: the
    `CommandRunner` adapter, the tree it executes against, and the
    per-Attempt directory its evidence is written under (contract §7's
    `{evidence_dir}`). Checking out the tree `working_directory` names —
    the Delivery Snapshot, or the Base Revision — is the caller's concern;
    this harness only runs commands and reads the evidence they declare."""

    runner: CommandRunner
    working_directory: Path
    evidence_dir: Path


def _run_one(name: str, spec: _ValidationCommand, context: CommandContext) -> RawCommandResult:
    command = render_command(spec.command_template, evidence_dir=context.evidence_dir)
    executed = context.runner.run(command, cwd=context.working_directory)

    junit = None
    if spec.evidence_template is not None:
        evidence_path = Path(render_command(spec.evidence_template, evidence_dir=context.evidence_dir))
        try:
            junit = parse_junit_xml(evidence_path.read_text(encoding="utf-8"))
        except (OSError, MalformedJUnitReport):
            junit = None

    return RawCommandResult(
        name=name,
        command=command,
        exit_code=executed.exit_code,
        junit=junit,
        evidence_declared=spec.evidence_declared,
    )


def run_validation_contract(profile: ProjectProfile, context: CommandContext) -> tuple[RawCommandResult, ...]:
    """Run every Validation Contract command against whatever tree
    `context.working_directory` currently holds (contract §8)."""
    return tuple(
        _run_one(name, spec, context) for name, spec in _validation_contract_commands(profile).items()
    )


class CommandBaseRevisionRunner:
    """The production `BaseRevisionRunner`: re-runs the same named command
    via a `CommandRunner`, in whatever tree `context.working_directory`
    holds when it is called. The caller checks out the Base Revision into
    that tree before invoking it — this class knows nothing about git."""

    def __init__(self, profile: ProjectProfile, context: CommandContext) -> None:
        self._commands = _validation_contract_commands(profile)
        self._context = context

    def run_at_base(self, command_name: str) -> RawCommandResult:
        return _run_one(command_name, self._commands[command_name], self._context)


def validate(
    profile: ProjectProfile, context: CommandContext, base_runner: BaseRevisionRunner
) -> ValidationEvidence:
    """The `validate` harness node (contract §4, §8), run outside the
    model's tool loop: run the Validation Contract against the Delivery
    Snapshot, then lazily against the Base Revision for whatever failed,
    and produce Validation Evidence."""
    delivery = run_validation_contract(profile, context)
    return evaluate_validation(delivery, base_runner)
