"""The Workflow Configuration: single entry point and version gate.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2 (the
``version: 4`` configuration file), §2.7 (strict parsing), §2.8 (trust and
paths) and §3 (static validation), plus the skills-declaration resolution
behind issue #372.

``load_workflow_configuration`` is the only public entry point:
``version: 4`` validates, ``version: 3`` is refused with the migration
diagnostic, ``version: 2`` with its own migration refusal, ``version: 1``
with the §16.4 drain note, and anything else with the generic ``version:
4`` refusal — every version refusal naming the ``version`` path.
``validate`` means calling this and checking the faults.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from coding_agent.workflow._checks import Problems, validate_configuration_text
from coding_agent.workflow.instance_profile import (
    InstanceProfileError,
    read_instance_profile_text,
)
from coding_agent.workflow.parser import WorkflowConfigError
from coding_agent.workflow.schema import WorkflowConfiguration


def _v3_migration_detail(source: Path | str) -> str:
    """The ``version: 3`` migration refusal; the CLI wording wins."""
    return (
        "unsupported version 3: must be 4; rewrite "
        f"{source} as `version: 4`: declare `context-window-tokens`, "
        "`context-threshold-tokens` and `output-cap-tokens` on every pin "
        "(direct pins newly), and rename `tokens` to `effective-tokens` in "
        "every Workflow `limits` and step `limits`; "
        "the file is left untouched"
    )


def _v2_migration_detail(source: Path | str) -> str:
    """The ``version: 2`` migration refusal (SKV3-01); the CLI wording wins."""
    return (
        "unsupported version 2: must be 4; `version: 2` is no longer "
        f"supported — rewrite {source} as `version: 4` with a `skills` "
        "declaration (see docs/ops/workflow-configuration.md); "
        "the file is left untouched"
    )


def _v1_drain_detail(source: Path | str) -> str:
    """The ``version: 1`` §16.4 drain note; the CLI wording wins."""
    return (
        f"{source} does not declare 'version: 4'; "
        "this release runs only version: 4 step workflows (§16.4). "
        "Drain MVP Attempts with the last pre-change release before upgrading."
    )


def _generic_version_detail(source: Path | str) -> str:
    """The generic refusal for anything that is not ``version: 4``."""
    return (
        f"{source} does not declare 'version: 4'; "
        "this release runs only version: 4 step workflows."
    )


@dataclass(frozen=True)
class LoadedWorkflowConfiguration:
    """A Workflow Configuration read once: raw bytes plus validated config."""

    path: Path
    """The configuration file the bytes were read from."""

    raw: bytes
    """The exact bytes validation ran on."""

    config: WorkflowConfiguration
    """The validated configuration."""

    profile_text: str | None = None
    """The instance-supplied Project Profile text (``target.profile``).

    ``None`` where the profile comes from the Target Repository. Where the
    configuration names ``target.profile`` this is the file's strict-UTF-8
    text, resolved by the same rules as ``prompt:`` locators (ADR 0030);
    parsing and the toolchain check belong to ``config validate`` and the
    startup ``project-profile`` phase, not to the load.
    """


def _peek_version_from_text(text: str) -> int | None:
    """Read the ``version`` field from already-read text, without strictness."""
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if isinstance(data, dict):
        version = data.get("version")
        if type(version) is int:
            return version
    return None


def load_workflow_configuration(
    config_path: Path | str,
    workflow_id: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[LoadedWorkflowConfiguration | None, list[WorkflowConfigError]]:
    """Read the Workflow Configuration file once and validate it.

    The single public entry point: ``version: 4`` is validated,
    ``version: 3`` is refused with the migration diagnostic, ``version: 2``
    with its own migration refusal, ``version: 1`` with the §16.4 drain
    note, and anything else with the generic ``version: 4`` refusal —
    every version refusal naming the ``version`` path. Returns
    ``(loaded, faults)``: on success ``loaded`` carries the exact bytes
    that were validated plus the validated config and ``faults`` is empty;
    on failure ``loaded`` is ``None``. An unreadable file is a fault; a
    file that is not valid UTF-8 propagates :class:`UnicodeDecodeError`.
    ``env`` is ignored and kept only for backward compatibility.
    """
    _ = env
    source = Path(config_path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        return (
            None,
            [WorkflowConfigError(str(source), f"cannot read workflow configuration: {exc}")],
        )
    # Propagates UnicodeDecodeError, mirroring a file read.
    text = raw.decode("utf-8")
    peeked = _peek_version_from_text(text)
    if peeked == 4:
        config, faults = validate_configuration_text(text, source, workflow_id)
        if config is None:
            return None, faults
        profile_text: str | None = None
        if config.target.profile is not None:
            try:
                profile_text = read_instance_profile_text(
                    source.parent.resolve(), config.target.profile
                )
            except InstanceProfileError as exc:
                return (
                    None,
                    [WorkflowConfigError("target.profile", str(exc))],
                )
        return (
            LoadedWorkflowConfiguration(
                path=source, raw=raw, config=config, profile_text=profile_text
            ),
            [],
        )
    problems = Problems()
    if peeked == 3:
        problems.add("version", _v3_migration_detail(source))
    elif peeked == 2:
        problems.add("version", _v2_migration_detail(source))
    elif peeked == 1:
        problems.add("version", _v1_drain_detail(source))
    else:
        problems.add("version", _generic_version_detail(source))
    return None, problems.items
