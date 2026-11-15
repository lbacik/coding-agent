"""Per-member identity records pinned in the Effective Configuration.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.10.
Matrix row id: SKV3-26.

These records live in the Skill Bundle package (not in
``coding_agent.workflow.effective``) so that the deployment path can build
them without importing the Effective Configuration module, which itself
imports the Skill Bundle package — a circular import otherwise.
``coding_agent.workflow.effective`` re-exports them.
"""

from __future__ import annotations

from dataclasses import dataclass

from coding_agent.skillbundle.policy import PolicyReview


@dataclass(frozen=True)
class InventoryFile:
    """One installed artifact file: relative path, content digest, exec bit."""

    path: str
    sha256: str
    executable: bool


@dataclass(frozen=True)
class MemberOverlay:
    """One installer Overlay record applied to a member."""

    id: str
    version: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class BundleMember:
    """One available Skill Bundle member's identity for the core (SKV3-26).

    Every declared member is pinned — including on-demand-only members no
    static instruction names. ``declared_ref`` is the entry's revision
    constraint (``None`` floats); ``resolved_commit`` is the concrete commit
    the deployment verified, so the same bytes at another commit still
    change the identity. ``files`` is the complete recursive inventory
    (including ``not-delivered`` files); ``overlays`` the applied Overlay
    identities; ``review`` the selected complete policy review with its
    dispositions, reference rulings, ordering and notices.
    """

    name: str
    source_identity: str
    resolved_commit: str
    declared_ref: str | None
    relative_source_path: str
    store_relative_path: str
    files: tuple[InventoryFile, ...]
    overlays: tuple[MemberOverlay, ...]
    review: PolicyReview
