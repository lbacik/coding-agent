"""Shared operator-contract field shapes (contract major 1, ``v1.0.4``).

The published ``agent-operator-contract`` schemas are the source of truth
for every ``agentctl`` reply. This module holds the small shared pieces
the producers need so they agree with those documents:

- the ``identity.contract`` object (``{"major": 1, "minor": 0}``) — the
  envelope's ``contract`` stays the ``"major.minor"`` string;
- RFC 3339 UTC timestamps with a ``Z`` suffix, both for new writes and
  for converting stored values (older volumes recorded ``+00:00``);
- the ``figure`` documents ``current_attempt`` budgets and figures use.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

CONTRACT_MAJOR: int = 1
"""The contract major every reply speaks."""

CONTRACT_MINOR: int = 0
"""The contract minor every reply speaks."""

CONTRACT_VERSION: str = f"{CONTRACT_MAJOR}.{CONTRACT_MINOR}"
"""The envelope's ``contract`` string (``"1.0"``)."""

IDENTITY_CONTRACT: dict[str, int] = {"major": CONTRACT_MAJOR, "minor": CONTRACT_MINOR}
"""The ``identity.contract`` object (and the one nested in ``status``)."""


def utc_now_z() -> str:
    """Now in UTC, formatted as RFC 3339 with a ``Z`` suffix."""
    return _format_z(datetime.now(timezone.utc))


def _format_z(moment: datetime) -> str:
    """Format ``moment`` as RFC 3339 UTC with a ``Z`` suffix."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    base = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        base += f".{moment.microsecond:06d}"
    return base + "Z"


def to_z(value: str | datetime | None) -> str | None:
    """Convert a stored timestamp to the contract's ``Z`` shape.

    ``None`` stays ``None``; a string already ending in ``Z`` passes
    through; anything else parses as ISO-8601 (naive values read as UTC)
    and formats with :func:`utc_now_z` rules. Stored rows from older
    volumes (``+00:00`` offsets) convert here so replies never carry them.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return _format_z(value)
    text = str(value)
    if text.endswith("Z"):
        return text
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    return _format_z(parsed)


def to_z_or_now(value: str | datetime | None) -> str:
    """Convert a stored timestamp, falling back to now where missing."""
    converted = to_z(value)
    return converted if converted is not None else utc_now_z()


def figure(
    value: int | float | None,
    unit: str,
    basis: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """One contract ``figure``: value is null exactly for null bases.

    ``basis`` is one of ``measured``, ``estimated``, ``configured``,
    ``assumed``, ``unavailable`` or ``not_applicable``; a ``None`` value
    forces ``unavailable`` where the caller passed another null basis.
    """
    resolved_basis = basis
    if value is None and resolved_basis not in ("unavailable", "not_applicable"):
        resolved_basis = "unavailable"
    if resolved_basis in ("unavailable", "not_applicable"):
        result: dict[str, Any] = {
            "value": None,
            "unit": unit,
            "basis": resolved_basis,
        }
    else:
        result = {"value": value, "unit": unit, "basis": resolved_basis}
    if reason is not None:
        result["reason"] = reason
    return result


__all__ = [
    "CONTRACT_MAJOR",
    "CONTRACT_MINOR",
    "CONTRACT_VERSION",
    "IDENTITY_CONTRACT",
    "figure",
    "to_z",
    "to_z_or_now",
    "utc_now_z",
]
