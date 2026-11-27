"""The code-host facts, centralised as one frozen value object.

Contract: ``docs/contract/mvp-runtime-contract.md`` §13 and §18. Every fact
about the code host that used to be hard-coded across the runtime, identity
and preflight modules lives here exactly once: the canonical
credential-free remote URL shape, the credential-helper store host, the
id-based noreply address shape, the publication body limit, and the
protected-CI directory. There is exactly one instance, :data:`GITHUB`; no
caller constructs another and no host-selection knob exists.

The profile holds strings, tuples and ints only — no client, no network —
so the runtime, identity and preflight modules import it without pulling in
any worker-port machinery.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coding_agent.identity.startup import Identity


@dataclass(frozen=True)
class CodeHostProfile:
    """The code-host facts every other module treats as configuration (§18).

    All values are data: the two pure methods below derive the remote URL
    and the commit email from them, so every call site injects the profile
    instead of repeating the literals.
    """

    host: str
    """The code host's bare hostname (``github.com``).

    The canonical credential-free remote URL is ``https://<host>/...`` and
    the id-based noreply address lives under ``users.noreply.<host>``.
    """

    redaction_hosts: tuple[str, ...]
    """Hosts whose credential-store entries reach git through the helper.

    The first entry is the git credential helper's store host (§18,
    L3-ERR-11): the token is written as
    ``https://x-access-token:<token>@<redaction_hosts[0]>`` while the remote
    URL stays the canonical credential-free form.
    """

    body_char_limit: int
    """The publication body limit (§14.4, OP-19).

    A rendered publication body never exceeds this: only the ``document`` is
    truncated, with an explicit marker and a pointer to the Step Artifact.
    """

    protected_ci_paths: tuple[str, ...]
    """The protected-CI directory facts (§13, L3-ERR-9).

    Index 0 is the prefix form with the trailing slash (for ``startswith``
    checks and for joining probe paths); index 1 is the root form without
    it (for equality checks).
    """

    def remote_url(self, owner: str, repo: str) -> str:
        """The canonical credential-free URL for ``owner``/``repo`` (§18)."""
        return f"https://{self.host}/{owner}/{repo}.git"

    def commit_email(self, identity: Identity) -> str:
        """The id-based noreply address for the Agent Identity (§18)."""
        return f"{identity.account_id}+{identity.login}@users.noreply.{self.host}"


GITHUB = CodeHostProfile(
    host="github.com",
    redaction_hosts=("github.com",),
    body_char_limit=65_536,
    protected_ci_paths=(".github/workflows/", ".github/workflows"),
)
"""The single code-host profile: GitHub, with today's values exactly."""
