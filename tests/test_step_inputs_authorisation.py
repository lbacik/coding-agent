"""Selection Label authorisation by the ``labeled`` event (§5.1, §18).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §5.1 (the
actor test under the detected identity mode) and §18 (the mode is
detected, not configured).

Row ids: INP-1, INP-6, INP-19, INP-20.
"""

from __future__ import annotations

from coding_agent.inputs.authorisation import (
    detect_identity_mode,
    label_authorised,
)
from coding_agent.inputs.port import LabeledEvent


def _event(
    *,
    actor_login: str = "owner",
    actor_id: int | None = 999,
    actor_type: str = "User",
    created_at: str = "2026-10-01T00:00:00Z",
) -> LabeledEvent:
    return LabeledEvent(
        label="agent:implement",
        actor_login=actor_login,
        actor_id=actor_id,
        actor_type=actor_type,
        created_at=created_at,
    )


def test_equal_ids_are_a_shared_identity() -> None:
    """§18: the Agent Identity equal to the owner is a shared identity."""
    assert detect_identity_mode(999, 999) == "shared-identity"


def test_different_ids_are_a_dedicated_identity() -> None:
    """§18: different ids are a dedicated identity; no YAML selects it."""
    assert detect_identity_mode(1001, 999) == "dedicated"


def test_missing_event_is_refused() -> None:
    """§5.1: without a ``labeled`` event there is no authorisation to test."""
    assert not label_authorised(
        None, agent_login="owner", agent_id=999, identity_mode="shared-identity"
    )
    assert not label_authorised(
        None, agent_login="coding-agent", agent_id=1001, identity_mode="dedicated"
    )


def test_shared_identity_accepts_the_agent_identity_as_owner() -> None:
    """INP-19: a label the Agent Identity applied is the owner's authorisation."""
    assert label_authorised(
        _event(actor_login="owner", actor_id=999, actor_type="User"),
        agent_login="owner",
        agent_id=999,
        identity_mode="shared-identity",
    )


def test_shared_identity_accepts_a_human() -> None:
    """INP-19: a human's application is accepted under a shared identity."""
    assert label_authorised(
        _event(actor_login="teammate", actor_id=1234, actor_type="User"),
        agent_login="owner",
        agent_id=999,
        identity_mode="shared-identity",
    )


def test_shared_identity_refuses_a_bot() -> None:
    """INP-19: only a ``Bot``-type actor is refused under a shared identity."""
    assert not label_authorised(
        _event(actor_login="some-bot", actor_id=4321, actor_type="Bot"),
        agent_login="owner",
        agent_id=999,
        identity_mode="shared-identity",
    )


def test_dedicated_identity_refuses_the_agent_identity() -> None:
    """INP-20: an actor equal to the Agent Identity is refused when dedicated."""
    assert not label_authorised(
        _event(actor_login="coding-agent", actor_id=1001, actor_type="User"),
        agent_login="coding-agent",
        agent_id=1001,
        identity_mode="dedicated",
    )


def test_dedicated_identity_refuses_a_bot() -> None:
    """INP-20: a ``Bot``-type account is refused when dedicated."""
    assert not label_authorised(
        _event(actor_login="some-bot", actor_id=4321, actor_type="Bot"),
        agent_login="coding-agent",
        agent_id=1001,
        identity_mode="dedicated",
    )


def test_dedicated_identity_accepts_a_human() -> None:
    """INP-20: a human's application is accepted when dedicated."""
    assert label_authorised(
        _event(actor_login="owner", actor_id=999, actor_type="User"),
        agent_login="coding-agent",
        agent_id=1001,
        identity_mode="dedicated",
    )


def test_actor_compared_by_id_where_known() -> None:
    """§5.1: the numeric id decides the actor test where it is known."""
    assert not label_authorised(
        _event(actor_login="renamed", actor_id=1001, actor_type="User"),
        agent_login="coding-agent",
        agent_id=1001,
        identity_mode="dedicated",
    )
