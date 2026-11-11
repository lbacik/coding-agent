"""Pinned input state: Fingerprint, Base Revision and Input Head (§7.1, §7.2).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §7.1 (an
issue input pins the Fingerprint — the digest of the title and body taken
from the Claim read-back, the same content the model sees — and the Base
Revision, the base-branch tip at Claim; the workspace is checked out
exactly at it) and §7.2 (a pull-request input pins the title/body digest,
the head SHA, the base branch name and the base-branch tip; the Base
Revision is the merge base of the pinned head and that tip; the Input Head
is the pinned head, which the Attempt reviews or changes; the Attempt's
own compare-and-swap push advances the Input Head and is not Drift).

Row ids: INP-1, INP-2, INP-14, INP-15.

The merge base itself is computed by the caller through git: this module
takes it as an argument and records it, so the pinning stays pure.
"""

from __future__ import annotations

from dataclasses import dataclass

from coding_agent.inputs.authorisation import IdentityMode
from coding_agent.inputs.port import InputType, TrackedInput
from coding_agent.runtime.fingerprint import compute_fingerprint


@dataclass(frozen=True)
class PinnedInput:
    """The input state pinned at Claim, from the Claim read-back (§7).

    The Fingerprint is the read-back's title and body digest (INP-14: a
    body change between listing and read-back is caught because the pin
    comes from the read-back, never the listing). For a pull-request input
    ``input_head`` is the pinned head, ``base_branch`` its base branch
    name, and ``base_revision`` the merge base of the two. New commits on
    the base branch are not Drift; they are only disclosed (INP-18).
    """

    workflow_id: str
    input_type: InputType
    input_number: int
    title: str
    body: str
    fingerprint: str
    base_revision: str
    base_tip: str | None = None
    input_head: str | None = None
    head_branch: str | None = None
    base_branch: str | None = None
    selection_actor: str | None = None
    selection_actor_id: int | None = None
    selection_actor_type: str | None = None
    selection_actor_at: str | None = None
    identity_mode: IdentityMode | None = None


def pin_from_readback(
    readback: TrackedInput,
    *,
    workflow_id: str,
    base_tip: str,
    merge_base: str | None = None,
    selection_actor: str | None = None,
    selection_actor_id: int | None = None,
    selection_actor_type: str | None = None,
    selection_actor_at: str | None = None,
    identity_mode: IdentityMode | None = None,
) -> PinnedInput:
    """Pin the Claim read-back (§7.1, §7.2).

    ``base_tip`` is the base-branch tip at Claim. For a pull-request
    input the caller also passes the merge base of the pinned head and
    that tip as ``merge_base`` — computed through git, recorded here.
    """
    fingerprint = compute_fingerprint(readback.title, readback.body)
    if readback.input_type == "pull-request":
        if merge_base is None:
            raise ValueError("a pull-request pin needs its merge base")
        if readback.head_sha is None:
            raise ValueError("a pull-request read-back needs its head SHA")
        return PinnedInput(
            workflow_id=workflow_id,
            input_type=readback.input_type,
            input_number=readback.number,
            title=readback.title,
            body=readback.body,
            fingerprint=fingerprint,
            base_revision=merge_base,
            base_tip=base_tip,
            input_head=readback.head_sha,
            head_branch=readback.head_branch,
            base_branch=readback.base_branch,
            selection_actor=selection_actor,
            selection_actor_id=selection_actor_id,
            selection_actor_type=selection_actor_type,
            selection_actor_at=selection_actor_at,
            identity_mode=identity_mode,
        )
    return PinnedInput(
        workflow_id=workflow_id,
        input_type=readback.input_type,
        input_number=readback.number,
        title=readback.title,
        body=readback.body,
        fingerprint=fingerprint,
        base_revision=base_tip,
        base_tip=base_tip,
        selection_actor=selection_actor,
        selection_actor_id=selection_actor_id,
        selection_actor_type=selection_actor_type,
        selection_actor_at=selection_actor_at,
        identity_mode=identity_mode,
    )
