"""Selection Label authorisation by the ``labeled`` event (contract §5.1, §18).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §5.1 (at the
Claim read-back the Worker reads the latest ``labeled`` event for the
Selection Label and tests its actor according to the identity mode the
Worker detected at startup) and §18 (the identity mode is detected, not
configured: the Agent Identity's numeric id from ``GET /user`` against the
Target Repository owner's id from ``GET /repos/{owner}/{repo}`` — equal ids
are a shared identity, different ids a dedicated identity).

Row ids: INP-1, INP-6, INP-19, INP-20.

This module is pure: it takes the reported ids and the reported event and
answers which mode holds and whether the label application is authorised.
No YAML key selects the mode.
"""

from __future__ import annotations

from typing import Literal

from coding_agent.inputs.port import LabeledEvent

IdentityMode = Literal["shared-identity", "dedicated"]
"""The detected identity mode (§18, #288 point 1)."""


def detect_identity_mode(agent_id: int, owner_id: int) -> IdentityMode:
    """Detect the identity mode from the two numeric ids (§18).

    Equal ids are a **shared identity** (the Agent Identity is the Target
    Repository owner); different ids are a **dedicated identity**. No YAML
    key controls it.
    """
    if agent_id == owner_id:
        return "shared-identity"
    return "dedicated"


def label_authorised(
    event: LabeledEvent | None,
    *,
    agent_login: str,
    agent_id: int | None,
    identity_mode: IdentityMode,
) -> bool:
    """Whether the Selection Label application is authorised (§5.1).

    The actor, the time and the mode are recorded with the Attempt
    (INP-19, INP-20). ``None`` (no ``labeled`` event for the Selection
    Label) is refused: without an event there is no authorisation to test.

    - **shared identity** (the Agent Identity is the Target Repository
      owner): only a ``Bot``-type actor is refused. An actor equal to the
      Agent Identity is the owner and is accepted.
    - **dedicated identity:** an actor equal to the Agent Identity or a
      ``Bot``-type account is refused; a human's application is accepted.
    """
    if event is None:
        return False
    if event.actor_type == "Bot":
        return False
    if identity_mode == "shared-identity":
        return True
    if agent_id is not None:
        return event.actor_id != agent_id
    return event.actor_login != agent_login
