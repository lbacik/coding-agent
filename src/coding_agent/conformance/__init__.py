"""Producer conformance for the operator contract (issue #260).

Contract: ``agent-operator-contract`` major 1 at pinned revision
``06ee46c`` (``docs/spec.md`` §4, §5, §6, ``examples/agentctl/``), the
revision :mod:`coding_agent.agentctl` speaks. The upstream
``agent-operator-contract`` ``v1.0.0`` tag that adoption-plan step 1 was
meant to publish — and with it the canonical harness API and the
published JSON Schemas — does not exist yet, so this package tracks the
pinned revision instead and is written to be replaced by it: the schema
files under ``conformance/schemas/`` mirror the contract's fixture
shapes field for field, and :mod:`coding_agent.conformance.harness`
implements the five harness operations the issue names. When ``v1.0.0``
lands, the pin moves and the schemas become the published ones; the
harness API stays the same shape.

What the suite covers that the in-process tests cannot: real process
death (a ``SIGKILL``ed OS process, not a thread), ``docker exec`` and
``docker run`` invocation, the read-only root filesystem, the discovery
labels and the socket permissions. The Docker half runs in CI against
the built image (``conformance/producer-suite.sh``); the in-repo half
runs here with pytest over the fake provider (``tests/support/``'s
scripted model) and the fake tracker, plus structural assertions over
the ``Dockerfile`` and ``compose.yaml`` that fail the same way without
a daemon.
"""

from __future__ import annotations

CONTRACT_PIN = "06ee46c"
CONTRACT_MAJOR = 1
CONTRACT_VERSION = "1.0"

__all__ = ["CONTRACT_MAJOR", "CONTRACT_PIN", "CONTRACT_VERSION"]
