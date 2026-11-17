"""Guard rails for the Run Ledger reader refactor (issue #435).

The Run Ledger's table layout lives in exactly one module:
:mod:`coding_agent.ledger.queries`. These tests fail where knowledge of
the lifecycle tables (``attempts``, ``outcomes``, ``effects``,
``step_executions``, ``step_effects``, ``step_transitions``,
``run_ledger_events``) or of the operator tables (``operator_instance``,
``operator_halt``) leaks anywhere else:

- no lifecycle ``SELECT`` outside the ``ledger`` package (Intake and
  Operator Command SQL in :mod:`operator.commands` is exempt, as is the
  out-of-scope :mod:`operator.events` module);
- no ``operator_instance``/``operator_halt`` SQL outside
  :mod:`operator.store` (same exemptions);
- no direct ``sqlite3`` read connections outside the ledger reader, the
  ledger itself, the operator store and the exempt command path.

Companion behaviour tests: the reader cannot write, and a ledger that
predates the step tables reads back empty instead of raising.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "coding_agent"

LIFECYCLE_TABLES = (
    "attempts",
    "outcomes",
    "effects",
    "step_executions",
    "step_effects",
    "step_transitions",
    "run_ledger_events",
)

OPERATOR_TABLES = (
    "operator_instance",
    "operator_halt",
)

_SELECT_OR_FROM = re.compile(r"(?i)\b(?:select|from)\b")


def _table_pattern(table: str) -> re.Pattern[str]:
    return re.compile(r"(?i)\b" + re.escape(table) + r"\b")


_TABLE_PATTERNS = {table: _table_pattern(table) for table in (*LIFECYCLE_TABLES, *OPERATOR_TABLES)}

# Files allowed to hold lifecycle SQL: the ledger package owns the table
# layout; commands.py owns Intake/Operator Command SQL (ADR 0026); events.py
# owns the out-of-scope operator_events table.
_LIFECYCLE_ALLOWED = ("ledger/", "operator/commands.py", "operator/events.py")
# Files allowed to hold operator_instance/operator_halt SQL: the store owns
# those tables; commands.py keeps its exempt Intake/Command SQL.
_OPERATOR_ALLOWED = ("operator/store.py", "operator/commands.py", "operator/events.py")
# Modules allowed to open sqlite connections: the ledger (writer + reader),
# the operator store (its own connection) and the exempt command path.
_CONNECT_ALLOWED = (
    "ledger/",
    "operator/store.py",
    "operator/commands.py",
    "operator/events.py",
)


def _string_literals(path: Path) -> list[str]:
    """Every non-docstring string literal in ``path``, with adjacent parts joined.

    Documentation strings — module, class and function docstrings as well
    as attribute docstrings (a bare string statement after an assignment
    or dataclass field) — are prose, not knowledge of the table layout:
    "rows come straight from the ledger's ``attempts`` table" must not
    count as SQL. A bare string statement never executes, so every
    ``ast.Expr`` string is skipped; SQL always lives in an assigned,
    passed or returned literal. Only those are scanned.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # Ids of string constants that are bare expression statements. Both
    # walks visit the same tree, so an id recorded here identifies the same
    # node below; a bare string can never execute as SQL.
    statement_string_ids: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            statement_string_ids.add(id(node.value))
    literals: list[str] = []
    for node in ast.walk(tree):
        if id(node) in statement_string_ids:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
            if parts:
                literals.append("".join(parts))
    return literals


def _python_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def _relative(path: Path) -> str:
    return path.relative_to(SRC_ROOT).as_posix()


def test_no_lifecycle_sql_outside_ledger() -> None:
    """No lifecycle SELECT outside the ledger package (exemptions above)."""
    offenders: list[str] = []
    for path in _python_files():
        rel = _relative(path)
        if rel.startswith(_LIFECYCLE_ALLOWED):
            continue
        for literal in _string_literals(path):
            if not _SELECT_OR_FROM.search(literal):
                continue
            hits = [t for t in LIFECYCLE_TABLES if _TABLE_PATTERNS[t].search(literal)]
            if hits:
                offenders.append(f"{rel}: {sorted(hits)}")
                break
    assert offenders == [], "lifecycle SQL outside ledger/: " + "; ".join(offenders)


def test_no_instance_halt_sql_outside_store() -> None:
    """No operator_instance/operator_halt SQL outside the store (exempt files)."""
    offenders: list[str] = []
    for path in _python_files():
        rel = _relative(path)
        if rel.startswith(_OPERATOR_ALLOWED):
            continue
        for literal in _string_literals(path):
            if not _SELECT_OR_FROM.search(literal):
                continue
            hits = [t for t in OPERATOR_TABLES if _TABLE_PATTERNS[t].search(literal)]
            if hits:
                offenders.append(f"{rel}: {sorted(hits)}")
                break
    assert offenders == [], "operator-table SQL outside store: " + "; ".join(offenders)


def _opens_sqlite_connection(path: Path) -> bool:
    """Whether ``path`` calls ``sqlite3.connect`` or ``sqlite3.Connection``.

    AST-based so annotations (``connection: sqlite3.Connection``) and
    ``except sqlite3.Error`` handlers do not count: only a call that opens
    a handle does.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("connect", "Connection"):
            value = func.value
            if isinstance(value, ast.Name) and value.id == "sqlite3":
                return True
    return False


def test_no_direct_sqlite_connections_for_reads() -> None:
    """Only the ledger, the store and the exempt command path open sqlite."""
    offenders: list[str] = []
    for path in _python_files():
        rel = _relative(path)
        if rel.startswith(_CONNECT_ALLOWED):
            continue
        if _opens_sqlite_connection(path):
            offenders.append(rel)
    assert offenders == [], "direct sqlite use outside ledger/store: " + "; ".join(offenders)
