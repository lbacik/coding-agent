"""Marker-looked-up external writes: one driver plus one module per kind.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §14 (every
external write is an Effect Intent settled once: the key travels in the
write's Attempt Marker; lookups filter by the Agent Identity) and §14.2
(settlement on restart).

Each marker-looked-up write kind owns one Effect module holding its typed
intent target (which serialises to and from today's ``expected_target``
dict), the existing-effect lookup by Attempt Marker, the optional
precondition, the write itself and the remote-id extraction. The single
:func:`drive_effect` driver owns the intent → lookup → receipt /
not-performed sequence, once in total: the forward publication path, restart
settlement and Attempt finalisation are separate entry points over the same
modules (ADR 0028: restart never enters the graph).

Adding a write kind means adding one module here and one registry entry.
"""

from coding_agent.runtime.effects.driver import (
    EffectContext,
    EffectOutcome,
    Failed,
    Landed,
    NotPerformed,
    Performed,
    drive_effect,
)
from coding_agent.runtime.effects.modules import (
    CommentEffect,
    CommentTarget,
    EffectModule,
    PullRequestEffect,
    PullRequestTarget,
    ReviewEffect,
    ReviewTarget,
    TerminalCommentEffect,
    TerminalCommentTarget,
)
from coding_agent.runtime.effects.registry import (
    OPEN_PULL_REQUEST,
    POST_COMMENT,
    PUBLISH_REVIEW,
    STEP_EFFECT_MODULES,
    STEP_EFFECT_OPERATIONS,
    TERMINAL_COMMENT,
    module_for_intent,
    module_for_operation,
)

__all__ = [
    "CommentEffect",
    "CommentTarget",
    "EffectContext",
    "EffectModule",
    "EffectOutcome",
    "Failed",
    "Landed",
    "NotPerformed",
    "OPEN_PULL_REQUEST",
    "POST_COMMENT",
    "PUBLISH_REVIEW",
    "Performed",
    "PullRequestEffect",
    "PullRequestTarget",
    "ReviewEffect",
    "ReviewTarget",
    "STEP_EFFECT_MODULES",
    "STEP_EFFECT_OPERATIONS",
    "TERMINAL_COMMENT",
    "TerminalCommentEffect",
    "TerminalCommentTarget",
    "drive_effect",
    "module_for_intent",
    "module_for_operation",
]
