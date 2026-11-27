"""Test-only Attempt Marker renderings: the GitHub text for a marker value.

Every write and lookup takes the structured ``AttemptMarker``; the bodies
tests assert over still carry the adapter's rendered text. These helpers
render the Claim and terminal markers the GitHub way (byte-identical to
every receipt), so tests name the value once and assert the text.
"""

from __future__ import annotations

from coding_agent.inputs.claim import claim_marker_for, terminal_marker_for
from coding_agent.inputs.github import format_attempt_marker


def claim_marker_text(attempt: str) -> str:
    """The GitHub Claim Comment marker text for ``attempt`` (§6.1)."""
    return format_attempt_marker(claim_marker_for(attempt))


def terminal_marker_text(attempt: str) -> str:
    """The GitHub terminal marker text for ``attempt`` (§15.3)."""
    return format_attempt_marker(terminal_marker_for(attempt))
