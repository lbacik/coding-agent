"""The provider-neutral in-place retry bound (§14.4, contract §13).

In-place retry is bounded at 3 attempts and 5 minutes per operation, and
the waiting counts against the Attempt's wall clock; beyond the bound the
failure is infrastructure-class. Both the adapter's classified requests
and the effect driver's lookup-before-repeat loop read the same bound
from here, so the two retries cannot drift.
"""

from __future__ import annotations

MAX_IN_PLACE_ATTEMPTS = 3
"""Total requests per operation before the bound is exhausted (contract §13)."""

MAX_IN_PLACE_WAIT_SECONDS = 5 * 60.0
"""Total waited seconds per operation before the bound is exhausted (§13)."""

INITIAL_BACKOFF_SECONDS = 1.0
"""First wait of the exponential backoff for a secondary limit without ``Retry-After``."""


__all__ = [
    "INITIAL_BACKOFF_SECONDS",
    "MAX_IN_PLACE_ATTEMPTS",
    "MAX_IN_PLACE_WAIT_SECONDS",
]
