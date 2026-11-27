"""The Worker's mirror of the Target Repository: one owner for credential
lifetime, mirror freshness and redaction.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §7.2 (the Base
Revision is the merge base of the pinned Input Head and the pinned Base Tip),
§8 (intake verifies an offer against the remote before any Claim; a
Continuation opens at the recorded work commit; a pull-request Attempt opens
at its pinned Input Head) and §18 (git authenticates through a short-lived
credential helper while the remote URL stays the canonical credential-free
form; every failure message passes through the redaction registry).

The mirror answers domain questions directly — the Base Revision for a head
and a Base Tip, whether a Round Handoff offer is still served, opening a
workspace at the Input Head or for a Continuation — so no caller ever sees a
credential helper or a token, and no caller can forget to refresh the mirror
before reading it: every public method clones the mirror on first use and
fetches-and-updates it otherwise, under its own short-lived credential
helper that closes on return.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from coding_agent.runtime.git import (
    GitFailure,
    Workspace,
    checkout_branch_at,
    checkout_workspace,
    ensure_mirror,
    fetch_revision,
    head_revision,
    is_ancestor,
    merge_base_of,
    remote_ref_sha,
)
from coding_agent.runtime.redaction import (
    GitCredentialHelper,
    SecretRegistry,
    temporary_git_credential_helper,
)
from coding_agent.runtime.round_handoff import RoundHandoffOffer


@dataclass(frozen=True)
class _OfferSync:
    """One shared remote ref → work commit → ancestry read (§8).

    ``remote_sha`` is what the remote serves at the offer's named ref
    (``None`` where the ref is absent); ``ancestor`` is whether the recorded
    Base Revision is in the work commit's history; ``failure`` carries the
    redacted transport or ancestry-read detail, or ``None`` where both reads
    succeeded. A ref serving another commit is a mismatch, not a failure.
    """

    remote_sha: str | None
    ancestor: bool
    failure: str | None


class TargetMirror:
    """The Worker's local bare mirror of the Target Repository (§7.2, §8).

    Constructed once from the mirror directory, the canonical
    credential-free remote URL, the Agent Identity token string and the
    redaction registry — never from the startup artifact. Holds no
    credential helper: each public method opens its own short-lived helper
    for exactly that call, refreshes the mirror first, and closes the
    helper on return, so no token outlives the check.
    """

    def __init__(
        self,
        mirror_dir: Path,
        canonical_remote: str,
        token: str,
        registry: SecretRegistry,
    ) -> None:
        self._mirror_dir = mirror_dir
        self._canonical_remote = canonical_remote
        self._token = token
        self._registry = registry

    @contextmanager
    def _fresh(self) -> Iterator[GitCredentialHelper]:
        """The mirror refreshed under its own short-lived helper (§7.2, §18).

        Clones where the state volume holds no mirror yet, fetches where one
        already exists. Yielding the helper lets the caller run its
        authenticated remote reads under the same lifetime; the helper file
        is removed when the call returns.
        """
        with temporary_git_credential_helper(self._token) as helper:
            ensure_mirror(
                self._mirror_dir,
                self._canonical_remote,
                redact=self._registry,
                helper=helper,
            )
            yield helper

    def _check_offer(
        self, offer: RoundHandoffOffer, helper: GitCredentialHelper | None
    ) -> _OfferSync:
        """The one ref → sha → ancestry implementation (§8).

        Reads what the remote serves at the offer's named ref, compares it
        to the recorded work commit, and proves the recorded Base Revision
        is in the work commit's history — all against the refreshed mirror.
        Transport and ancestry-read failures land in ``failure``; a moved or
        missing ref is a mismatch the public boundary reduces on its own.
        """
        try:
            remote_sha = remote_ref_sha(
                self._canonical_remote,
                offer.ref,
                redact=self._registry,
                helper=helper,
            )
        except GitFailure as exc:
            return _OfferSync(
                None,
                False,
                f"cannot verify ref {offer.ref!r} for {offer.attempt}: {exc}",
            )
        if remote_sha != offer.work_commit:
            return _OfferSync(remote_sha, False, None)
        try:
            ancestor = is_ancestor(
                self._mirror_dir,
                offer.base_revision,
                offer.work_commit,
                redact=self._registry,
            )
        except GitFailure as exc:
            return _OfferSync(
                remote_sha,
                False,
                f"cannot verify Base Revision {offer.base_revision} "
                f"for {offer.attempt}: {exc}",
            )
        return _OfferSync(remote_sha, ancestor, None)

    def resolve_base_revision(self, head_sha: str, base_tip: str) -> str:
        """The Base Revision: the merge base of a head and the Base Tip (§7.2).

        Refreshes the mirror first, so a fresh state volume resolves against
        the live remote rather than halting. Raises :class:`GitFailure` —
        redacted through the registry — where the range cannot be read, so
        the poll loop halts with ``claim-interrupted`` after the Claim has
        opened (§4.4, §16.1); no later sweep retries it.
        """
        with self._fresh():
            return merge_base_of(self._mirror_dir, head_sha, base_tip)

    def offer_still_served(self, offer: RoundHandoffOffer) -> bool:
        """Whether the remote still serves the offer's revisions (§8).

        The intake availability check: the named ref serves exactly the
        recorded work commit and the recorded Base Revision is in its
        history. Any transport failure or mismatch reads ``False`` — the
        sweep leaves the signal untouched and claims nothing, so a
        previously advertised revision that is no longer available is
        refused without a pickup offer.
        """
        try:
            with self._fresh() as helper:
                sync = self._check_offer(offer, helper)
        except Exception:
            return False
        return (
            sync.failure is None
            and sync.remote_sha == offer.work_commit
            and sync.ancestor
        )

    def open_workspace(
        self, workspace_dir: Path, *, branch: str | None = None
    ) -> Workspace:
        """A fresh checkout from the refreshed mirror (§7).

        Brings the mirror up to date first, then checks the workspace out at
        the given branch's head — the Base Revision — with the canonical
        remote retained as its ``origin``.
        """
        with self._fresh():
            return checkout_workspace(
                self._mirror_dir,
                workspace_dir,
                origin_url=self._canonical_remote,
                branch=branch,
            )

    def open_for_continuation(
        self,
        workspace_path: Path,
        offer: RoundHandoffOffer,
        attempt_branch: str,
    ) -> str:
        """Open the Continuation workspace at the recorded work commit (§8).

        Verifies the remote still serves the offer's work commit at its named
        ref, fetches the exact work commit and Base Revision, proves the Base
        Revision is an ancestor of the work commit, then opens the new Attempt
        branch at the work commit with a clean tree. The recorded Base
        Revision is retained even when the configured base branch has moved:
        ancestry is proven against the recording, never the current tip.
        Returns the work commit. Any failed verification raises
        :class:`GitFailure`: the Attempt ends ``internal-failure`` through
        the poll loop's infrastructure path — never model work on a moved or
        unavailable revision.
        """
        with self._fresh() as helper:
            sync = self._check_offer(offer, helper)
            if sync.failure is not None:
                raise GitFailure(f"continuation: {sync.failure}")
            if sync.remote_sha != offer.work_commit:
                served = (
                    sync.remote_sha if sync.remote_sha is not None else "nothing"
                )
                raise GitFailure(
                    f"continuation: ref {offer.ref!r} serves {served}, "
                    f"not work commit {offer.work_commit} for {offer.attempt}"
                )
            if not sync.ancestor:
                raise GitFailure(
                    f"continuation: Base Revision {offer.base_revision} is not an "
                    f"ancestor of work commit {offer.work_commit} for {offer.attempt}"
                )
            fetch_revision(
                workspace_path,
                self._canonical_remote,
                offer.work_commit,
                redact=self._registry,
                helper=helper,
            )
            fetch_revision(
                workspace_path,
                self._canonical_remote,
                offer.base_revision,
                redact=self._registry,
                helper=helper,
            )
            checkout_branch_at(workspace_path, attempt_branch, offer.work_commit)
            return offer.work_commit

    def open_for_pull_request(
        self,
        workspace_path: Path,
        input_head: str,
        head_branch: str,
    ) -> str:
        """Open a pull-request Attempt at its pinned Input Head (§7.2, §9.10).

        Refreshes the mirror first, fetches the exact pinned head SHA the
        remote serves, then opens a local branch named exactly after the pull
        request's head branch at that commit with a clean tree. The Base
        Revision stays as pinned at Claim (the merge base of the head and the
        base tip) and is never re-resolved here. Returns the checked-out
        ``HEAD`` — the Input Head. Any failed fetch or checkout raises
        :class:`GitFailure`: the Attempt ends ``internal-failure`` through
        the poll loop's infrastructure path before any model call — never
        model work on the base-branch tip.
        """
        if not head_branch:
            raise GitFailure("pull-request: the pinned head branch is missing")
        if not input_head:
            raise GitFailure("pull-request: the pinned Input Head is missing")
        with self._fresh() as helper:
            try:
                fetch_revision(
                    workspace_path,
                    self._canonical_remote,
                    input_head,
                    redact=self._registry,
                    helper=helper,
                )
            except GitFailure as exc:
                raise GitFailure(
                    f"pull-request: cannot fetch Input Head {input_head} "
                    f"for head branch {head_branch!r}: {exc}"
                ) from exc
            try:
                checkout_branch_at(workspace_path, head_branch, input_head)
            except GitFailure as exc:
                raise GitFailure(
                    f"pull-request: cannot check out Input Head {input_head} "
                    f"on head branch {head_branch!r}: {exc}"
                ) from exc
            return head_revision(workspace_path)


__all__ = ["TargetMirror"]
