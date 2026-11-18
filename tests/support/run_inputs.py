"""Verified run inputs for tests that stub run verification (issue #433).

Tests that stub run verification build the stub's return value through
``passing_run_inputs`` (config file on disk) or ``make_run_inputs`` (an
already-loaded configuration) here; no test builds ``VerifiedRunInputs``
by hand anywhere else. The empty defaults mirror an empty deployment: no
prompt bytes, no members, an empty registry and contents, and a zero
manifest digest.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from coding_agent.skillbundle.activation import ActivationRegistry
from coding_agent.skillbundle.delivery import DELIVERY_FRAMING_VERSION
from coding_agent.skillbundle.deployment import (
    RunVerificationRefusal,
    VerifiedRunInputs,
)
from coding_agent.skillbundle.member_identity import BundleMember
from coding_agent.workflow.configuration import (
    LoadedWorkflowConfiguration,
    load_workflow_configuration,
)


def make_run_inputs(
    loaded: LoadedWorkflowConfiguration,
    *,
    prompt_raws: Mapping[str, bytes] | None = None,
    members: Sequence[BundleMember] = (),
    bodies: Mapping[str, str] | None = None,
    contents: Mapping[str, Mapping[str, bytes]] | None = None,
    manifest_sha256: str = "0" * 64,
    framing_version: int = DELIVERY_FRAMING_VERSION,
) -> VerifiedRunInputs:
    """A ``VerifiedRunInputs`` for an already-loaded configuration.

    Deliveries default to one empty delivery per member so the
    member/delivery coverage check inside the Effective Configuration
    builder holds for synthetic members.
    """
    decided_bodies = (
        dict(bodies) if bodies is not None else {member.name: "" for member in members}
    )
    return VerifiedRunInputs(
        loaded_config=loaded,
        prompt_raws=dict(prompt_raws) if prompt_raws is not None else {},
        members=tuple(members),
        registry=ActivationRegistry(bodies=decided_bodies),
        contents={name: dict(paths) for name, paths in (contents or {}).items()},
        manifest_sha256=manifest_sha256,
        framing_version=framing_version,
    )


def passing_run_inputs(
    config: Path,
    *,
    workflow: str | None = None,
    prompt_raws: Mapping[str, bytes] | None = None,
    members: Sequence[BundleMember] = (),
    bodies: Mapping[str, str] | None = None,
    contents: Mapping[str, Mapping[str, bytes]] | None = None,
    manifest_sha256: str = "0" * 64,
    framing_version: int = DELIVERY_FRAMING_VERSION,
) -> VerifiedRunInputs:
    """A passing run-verification value for the config file at ``config``.

    The configuration loads through the single loader, so the stub carries
    the same validated config startup validated. Prompt bytes default to
    empty; tests whose workflow names ``prompt:`` locators pass them
    explicitly.
    """
    loaded, faults = load_workflow_configuration(config, workflow)
    assert loaded is not None, [str(fault) for fault in faults]
    return make_run_inputs(
        loaded,
        prompt_raws=prompt_raws,
        members=members,
        bodies=bodies,
        contents=contents,
        manifest_sha256=manifest_sha256,
        framing_version=framing_version,
    )
