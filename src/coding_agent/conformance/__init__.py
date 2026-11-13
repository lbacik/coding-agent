"""Producer conformance for the operator contract (issue #359).

Contract: ``agent-operator-contract`` pinned at tag ``v1.0.4`` (see
``contract-pin.txt`` and the ``agent-operator-contract`` dev dependency).
The upstream schema documents are the source of truth; validation loads
them from the installed package, so no contract schema is copied into
this repository.
"""

from __future__ import annotations

CONTRACT_PIN = "v1.0.4"
CONTRACT_MAJOR = 1
CONTRACT_VERSION = "1.0"

__all__ = ["CONTRACT_MAJOR", "CONTRACT_PIN", "CONTRACT_VERSION"]
