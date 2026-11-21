"""Read-only Run Ledger reader on its own connection (ADR 0026).

The reader opens the ledger file with SQLite ``mode=ro`` and never runs
DDL, pragmas or writes. It returns the existing typed records plus the
aggregate reads the operator needs: the last finished Attempt, the
current Attempt's facts and a page of Attempts. It owns the lifecycle
tables (``attempts``, ``outcomes``, ``step_executions``,
``step_effects``, ``step_transitions`` and ``run_ledger_events``);
``operator_instance`` and ``operator_halt`` belong to
:mod:`coding_agent.operator.store`.

The legacy ``effects`` table is retired: nothing reads it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Any

from coding_agent.ledger import queries as _queries
from coding_agent.ledger.store import (
    AttemptRecord,
    OutcomeRecord,
    StepEffectRecord,
    StepExecutionRecord,
    StepTransitionRecord,
)

_READ_ONLY_TIMEOUT_SECONDS = 2.0


def _read_only_uri(path: Path) -> str:
    return f"file:{path}?mode=ro"


class RunLedgerReader:
    """Read-only view of one Run Ledger file on its own connection."""

    def __init__(self, path: Path, *, timeout_seconds: float = _READ_ONLY_TIMEOUT_SECONDS) -> None:
        """Open a read-only connection on ``path`` (SQLite ``mode=ro``)."""
        self._path = path
        self._connection = sqlite3.connect(
            _read_only_uri(path),
            uri=True,
            timeout=timeout_seconds,
            check_same_thread=False,
        )

    @property
    def path(self) -> Path:
        """The ledger file this reader observes."""
        return self._path

    @property
    def connection(self) -> sqlite3.Connection:
        """The reader's own read-only connection (for OperatorStore reads)."""
        return self._connection

    def close(self) -> None:
        """Close the reader's connection."""
        try:
            self._connection.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> RunLedgerReader:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def get_attempt(self, attempt_id: str) -> AttemptRecord | None:
        """One ``attempts`` row by id, or ``None`` where missing."""
        return _queries.get_attempt(self._connection, attempt_id)

    def get_attempt_created_at(self, attempt_id: str) -> str | None:
        """The ``created_at`` timestamp for one Attempt, if recorded."""
        return _queries.get_attempt_created_at(self._connection, attempt_id)

    def has_unfinished_attempt(self) -> bool:
        """Whether the ledger holds an Attempt neither finished nor released."""
        return _queries.has_unfinished_attempt(self._connection)

    def get_outcome(self, attempt_id: str) -> OutcomeRecord | None:
        """One ``outcomes`` row by Attempt id, or ``None`` where missing."""
        return _queries.get_outcome(self._connection, attempt_id)

    def list_step_executions(self, attempt_id: str) -> tuple[StepExecutionRecord, ...]:
        """Every ``step_executions`` row for one Attempt, in open order."""
        return _queries.list_step_executions(self._connection, attempt_id)

    def list_step_effects(self, attempt_id: str) -> tuple[StepEffectRecord, ...]:
        """Every ``step_effects`` row for one Attempt, in key order."""
        return _queries.list_step_effects(self._connection, attempt_id)

    def list_step_transitions(self, attempt_id: str) -> tuple[StepTransitionRecord, ...]:
        """Every ``step_transitions`` row for one Attempt, in order."""
        return _queries.list_step_transitions(self._connection, attempt_id)

    def get_responses_without_usage(self, attempt_id: str) -> int:
        """The durable worst-case counter for one Attempt, else zero."""
        return _queries.get_responses_without_usage(self._connection, attempt_id)

    def get_responses_with_unreported_cache_write(self, attempt_id: str) -> int:
        """The durable unreported-cache-write counter for one Attempt, else zero."""
        return _queries.get_responses_with_unreported_cache_write(
            self._connection, attempt_id
        )

    def get_latest_usage(self, attempt_id: str) -> dict[str, Any] | None:
        """The newest ``usage`` record payload for one Attempt, if any."""
        return _queries.get_latest_usage(self._connection, attempt_id)

    def count_events(self, attempt_id: str, kind: str) -> int:
        """How many ledger events of ``kind`` one Attempt recorded."""
        return _queries.count_events_by_kind(self._connection, attempt_id, kind)

    def list_event_payloads(
        self, attempt_id: str, kinds: tuple[str, ...]
    ) -> list[tuple[str, dict[str, Any] | None]]:
        """Every ``(kind, payload)`` pair for one Attempt's events of ``kinds``.

        In ledger id order; a payload that does not decode reads as ``None``.
        """
        return _queries.list_event_payloads_by_kinds(
            self._connection, attempt_id, kinds
        )

    def last_finished_attempt(self) -> tuple[str, str] | None:
        """The latest finished, released or outcome-recorded-with-outcome Attempt."""
        return _queries.last_finished_attempt(self._connection)

    def list_attempts_page_rows(
        self, *, limit: int, bound: int | None
    ) -> list[tuple[Any, ...]]:
        """Raw ``attempts`` page rows, newest first (``limit + 1`` for cursor)."""
        return _queries.list_attempts_page_rows(
            self._connection, limit=limit, bound=bound
        )

    def get_attempt_page_row(self, attempt_id: str) -> tuple[Any, ...] | None:
        """The ``attempts`` page row for one Attempt id, if recorded."""
        return _queries.get_attempt_page_row(self._connection, attempt_id)

    def list_all_outcomes(self) -> dict[str, dict[str, str]]:
        """Every outcome keyed by Attempt id (for the attempts-page join)."""
        return _queries.list_all_outcomes(self._connection)
