"""Which Effect module settles which intent (§14.1, §14.2).

The registry maps each step operation and each terminal-write kind to its
module, so neither the forward publication path nor restart settlement
carries a write-kind ladder. Settlement resolves the module from the stored
intent row: rows keyed by ``"operation"``, terminal-comment rows
(``{"terminal-write": ...}``) and the legacy ``""`` / ``"terminal"``
operation strings all settle through today's format, which never changes.
"""

from __future__ import annotations

from typing import Any

from coding_agent.ledger.store import StepEffectRecord
from coding_agent.runtime.effects.modules import (
    CommentEffect,
    EffectModule,
    PullRequestEffect,
    ReviewEffect,
    TerminalCommentEffect,
)

POST_COMMENT = CommentEffect()
"""The ``post-comment`` Effect module (§10.7)."""

PUBLISH_REVIEW = ReviewEffect()
"""The ``publish-review`` Effect module (§10.6)."""

OPEN_PULL_REQUEST = PullRequestEffect()
"""The ``open-pull-request`` Effect module (§10.5)."""

TERMINAL_COMMENT = TerminalCommentEffect()
"""The terminal-comment Effect module (§15.3)."""

STEP_EFFECT_MODULES: dict[str, EffectModule[Any]] = {
    "post-comment": POST_COMMENT,
    "publish-review": PUBLISH_REVIEW,
    "open-pull-request": OPEN_PULL_REQUEST,
}
"""Each step operation that writes through an Effect module (§10.5–§10.7)."""

TERMINAL_EFFECT_MODULES: dict[str, EffectModule[Any]] = {
    "terminal-comment": TERMINAL_COMMENT,
}
"""Each terminal-write kind that writes through an Effect module (§15.3)."""

STEP_EFFECT_OPERATIONS = frozenset(STEP_EFFECT_MODULES)
"""The step operations owned by Effect modules, for the graph consult (§8.5)."""


def module_for_operation(operation: str) -> EffectModule[Any] | None:
    """The module for one step operation or terminal-write kind, if any."""
    if operation in STEP_EFFECT_MODULES:
        return STEP_EFFECT_MODULES[operation]
    return TERMINAL_EFFECT_MODULES.get(operation)


def _operation_of(intent: StepEffectRecord) -> str:
    target = intent.expected_target or {}
    operation = target.get("operation")
    return operation if isinstance(operation, str) else ""


def module_for_intent(intent: StepEffectRecord) -> EffectModule[Any] | None:
    """The module settling one stored intent row, if any (§14.2).

    Terminal-comment rows (``{"terminal-write": "terminal"}``) and the legacy
    ``""`` / ``"terminal"`` operation strings settle through the terminal
    comment module; terminal label and assignment rows
    (``remove-selection-label``, ``add-end-labels``, ``release-assignment``)
    and any other unknown operation (assignment, label, push) have no module
    and stay for the terminal path's idempotent replay.
    """
    target = intent.expected_target or {}
    terminal_write = target.get("terminal-write")
    if terminal_write == "terminal":
        return TERMINAL_COMMENT
    if isinstance(terminal_write, str):
        return None
    operation = _operation_of(intent)
    if operation in ("", "terminal"):
        return TERMINAL_COMMENT
    return module_for_operation(operation)
