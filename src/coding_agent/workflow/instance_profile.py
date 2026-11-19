"""The Workflow Configuration's optional instance-supplied Project Profile.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.1 (the
``target`` block), §2.8 (trust and paths: relative paths resolve against the
YAML file's directory and must stay inside it) and §10.2 (the two Project
Profile sources), plus [ADR 0030](../adr/0030-the-workflow-configuration-may-supply-the-project-profile.md).

``target.profile`` names a file beside the Workflow Configuration that carries
the deployment's Project Profile. Its path resolves by the same rules as
``prompt:`` locators — anything escaping the configuration directory is
refused, a symlink is accepted only while it stays inside, and the file is
decoded as strict UTF-8 — through the shared helper in
:mod:`coding_agent.workflow.prompt_files`, never a second set of path rules.
Where the key is absent the Target Repository's
``docs/agents/project-profile.yml`` stays the only source and nothing here
is consulted.
"""

from __future__ import annotations

from pathlib import Path

from coding_agent.profile.parser import (
    InvalidProfile,
    MissingReadinessFacts,
    UnknownSchema,
    parse_profile_yaml,
)
from coding_agent.profile.schema import ProjectProfile
from coding_agent.profile.toolchain import (
    SupportedToolchainMatrix,
    assert_toolchain,
)
from coding_agent.workflow.prompt_files import PromptFileError, read_locator_text

PROFILE_SOURCE_TARGET_REPOSITORY = "target-repository"
"""The ``profile_source`` fact where the contract comes from the repository."""

PROFILE_SOURCE_WORKFLOW_CONFIGURATION = "workflow-configuration"
"""The ``profile_source`` fact where the deployment supplies the contract."""


class InstanceProfileError(Exception):
    """A ``target.profile`` file that cannot be used.

    The message names the ``target.profile`` YAML path but never the file's
    contents, so it is safe to print in a refusal diagnostic.
    """


def read_instance_profile_text(base: Path, locator: str) -> str:
    """Resolve and decode the ``target.profile`` locator (§2.8).

    The same resolver and rules as ``prompt:`` locators: relative to the
    configuration file's directory, refused when it escapes, decoded as
    strict UTF-8. Any failure raises :class:`InstanceProfileError` naming
    the ``target.profile`` YAML path, never the file's contents.
    """
    try:
        return read_locator_text(base, locator, owner="target.profile")
    except PromptFileError as exc:
        raise InstanceProfileError(str(exc)) from exc


def parse_instance_profile(text: str) -> ProjectProfile:
    """Parse the instance-supplied profile with the schema-2 parser.

    Returns the validated :class:`ProjectProfile`. A file that does not
    parse, violates the schema, names an unknown schema or leaves a
    Readiness Fact missing raises :class:`InstanceProfileError` naming the
    ``target.profile`` YAML path.
    """
    outcome = parse_profile_yaml(text)
    if isinstance(outcome, ProjectProfile):
        return outcome
    if isinstance(outcome, InvalidProfile):
        raise InstanceProfileError(
            f"target.profile: invalid Project Profile: {outcome.detail}"
        )
    if isinstance(outcome, UnknownSchema):
        raise InstanceProfileError(
            f"target.profile: unsupported schema {outcome.declared!r}"
        )
    if isinstance(outcome, MissingReadinessFacts):
        raise InstanceProfileError(
            "target.profile: missing Readiness Facts: "
            f"{'; '.join(outcome.missing)}"
        )
    raise AssertionError(
        f"unreachable: unexpected profile outcome {type(outcome).__name__}"
    )


def assert_instance_profile_toolchain(
    profile: ProjectProfile, matrix: SupportedToolchainMatrix
) -> None:
    """Assert the supplied profile's toolchain against the matrix (ADR 0030).

    A toolchain outside the image's Supported Toolchain Matrix raises
    :class:`InstanceProfileError` naming the ``target.profile`` YAML path:
    with the profile supplied by the deployment a Toolchain Gap is a fact
    about the deployment, never a per-Attempt ``not-ready`` result.
    """
    mismatch = assert_toolchain(profile.toolchain, matrix)
    if mismatch is not None:
        raise InstanceProfileError(
            f"target.profile: unsupported toolchain: {mismatch.reason}"
        )
