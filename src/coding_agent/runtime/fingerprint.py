from __future__ import annotations

import hashlib


def compute_fingerprint(title: str, body: str) -> str:
    """The Fingerprint: a digest of the Target Issue's title and body, as read
    at this run. `\\0`-separated so a shifted split (`("a", "bc")` vs.
    `("ab", "c")`) never collides."""
    digest = hashlib.sha256()
    digest.update(title.encode("utf-8"))
    digest.update(b"\0")
    digest.update(body.encode("utf-8"))
    return digest.hexdigest()
