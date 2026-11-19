"""The Run Ledger never imports the operator cycle back (issue #439).

Static boundary between the ledger and the operator: the Attempt Archive
write side lives in the leaf module ``coding_agent.operator.archive`` so
``ledger/`` no longer reaches ``operator.attempts`` (whose read side
imports the ledger reader back). Two checks, both parsed from the source
with :mod:`ast`:

- no module in ``coding_agent.ledger`` carries a function-local import
  from ``coding_agent.operator``; every operator dependency of the ledger
  is a module-level import of a leaf module;
- starting from every module in ``coding_agent.ledger`` and following
  every ``coding_agent.*`` import (module-level and function-local),
  transitively, no module reached outside ``coding_agent.ledger`` imports
  ``coding_agent.ledger`` back.

A third check pins the archive leaf: nothing ``operator.archive`` reaches
imports ``coding_agent.ledger``, ``operator.attempts`` or
``operator.snapshots``.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
LEDGER_DIR = SRC_ROOT / "coding_agent" / "ledger"
LEDGER_PREFIX = "coding_agent.ledger"
OPERATOR_PREFIX = "coding_agent.operator"
CODING_AGENT_PREFIX = "coding_agent."

ARCHIVE_MODULE = "coding_agent.operator.archive"


def _module_path(module: str) -> Path | None:
    """The source file for ``module``, or ``None`` where it has none here."""
    relative = module.replace(".", "/")
    candidate = SRC_ROOT / (relative + ".py")
    if candidate.is_file():
        return candidate
    candidate = SRC_ROOT / relative / "__init__.py"
    if candidate.is_file():
        return candidate
    return None


def _resolve_base(owner: str, node: ast.ImportFrom) -> str:
    """The absolute module a ``from ... import ...`` statement reads from."""
    if node.level == 0:
        return node.module or ""
    anchor = owner.rpartition(".")[0]
    for _ in range(node.level - 1):
        anchor = anchor.rpartition(".")[0]
    if node.module:
        return f"{anchor}.{node.module}" if anchor else node.module
    return anchor


def _imported_modules(tree: ast.AST, owner: str) -> set[str]:
    """Every ``coding_agent.*`` module ``tree`` depends on.

    Both ``import coding_agent.x`` and ``from coding_agent.x import ...``
    forms, module-level and function-local: for ``from P import N`` both
    ``P`` and ``P.N`` count where ``P.N`` resolves to a module, since
    importing a submodule through its package still executes it.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "coding_agent" or alias.name.startswith(
                    CODING_AGENT_PREFIX
                ):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_base(owner, node)
            if not base:
                continue
            if base == "coding_agent" or base.startswith(CODING_AGENT_PREFIX):
                found.add(base)
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    candidate = f"{base}.{alias.name}"
                    if _module_path(candidate) is not None:
                        found.add(candidate)
    return found


def _function_local_imports(tree: ast.AST) -> list[ast.AST]:
    """Every import statement nested inside a function definition."""
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    hits: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        current: ast.AST | None = parent.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                hits.append(node)
                break
            current = parent.get(current)
    return hits


def _ledger_modules() -> dict[str, Path]:
    """Every module in ``coding_agent.ledger`` by dotted name."""
    modules: dict[str, Path] = {}
    for path in sorted(LEDGER_DIR.glob("*.py")):
        if path.name == "__init__.py":
            modules[LEDGER_PREFIX] = path
        else:
            modules[f"{LEDGER_PREFIX}.{path.stem}"] = path
    return modules


def _in_boundary(module: str) -> bool:
    """Whether ``module`` is inside the ledger/operator boundary under test."""
    return module == LEDGER_PREFIX or module.startswith(
        (LEDGER_PREFIX + ".", OPERATOR_PREFIX + ".")
    )


def _reachable_modules(roots: list[str]) -> dict[str, Path]:
    """Every boundary module reachable from ``roots``.

    The traversal is scoped to the ledger/operator boundary: imports
    naming any other ``coding_agent`` package (workflow, runtime,
    skillbundle, inputs, provider, identity, github) are opaque leaves
    and are not traversed. Their own layering with the ledger predates
    this change and is out of scope; the fresh-interpreter import test
    guards the runtime behaviour.
    """
    seen: dict[str, Path] = {}
    queue: list[str] = list(roots)
    while queue:
        module = queue.pop()
        if module in seen:
            continue
        path = _module_path(module)
        if path is None:
            continue
        seen[module] = path
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for dependency in _imported_modules(tree, module):
            if dependency not in seen and _in_boundary(dependency):
                queue.append(dependency)
    return seen


def _full_closure(roots: list[str]) -> dict[str, Path]:
    """Every module reachable from ``roots``, following all imports."""
    seen: dict[str, Path] = {}
    queue: list[str] = list(roots)
    while queue:
        module = queue.pop()
        if module in seen:
            continue
        path = _module_path(module)
        if path is None:
            continue
        seen[module] = path
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for dependency in _imported_modules(tree, module):
            if dependency not in seen:
                queue.append(dependency)
    return seen


def test_no_function_local_operator_imports_in_ledger() -> None:
    """``ledger/`` imports ``coding_agent.operator`` only at module level."""
    offenders: list[str] = []
    for module, path in sorted(_ledger_modules().items()):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _function_local_imports(tree):
            if isinstance(node, ast.ImportFrom):
                target = _resolve_base(module, node)
            else:
                assert isinstance(node, ast.Import)
                target = node.names[0].name if node.names else ""
            if target == OPERATOR_PREFIX or target.startswith(OPERATOR_PREFIX + "."):
                offenders.append(f"{path.name}:{node.lineno}: {target}")
    assert not offenders, (
        "function-local coding_agent.operator imports in ledger/: "
        + ", ".join(offenders)
    )


def test_nothing_reachable_from_ledger_imports_ledger_back() -> None:
    """No import cycle: nothing ``ledger/`` reaches imports ``ledger/``.

    The traversal follows the ledger/operator boundary: every operator
    module the ledger names is itself followed, while other
    ``coding_agent`` packages are opaque leaves. On the pre-change tree
    this fails through ``ledger/store.py`` -> ``operator.attempts`` ->
    ``ledger.reader``.
    """
    reached = _reachable_modules(sorted(_ledger_modules().keys()))
    offenders: list[str] = []
    for module in sorted(reached):
        if module == LEDGER_PREFIX or module.startswith(LEDGER_PREFIX + "."):
            continue
        path = reached[module]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for dependency in sorted(_imported_modules(tree, module)):
            if dependency == LEDGER_PREFIX or dependency.startswith(
                LEDGER_PREFIX + "."
            ):
                offenders.append(f"{module} -> {dependency}")
    assert not offenders, (
        "modules reachable from ledger/ that import ledger/ back: "
        + ", ".join(offenders)
    )


_ARCHIVE_FORBIDDEN = (
    "coding_agent.operator.attempts",
    "coding_agent.operator.snapshots",
)


def test_archive_module_is_a_leaf() -> None:
    """``operator.archive`` reaches no ledger, attempts or snapshots module.

    Unlike the boundary traversal above, this follows the archive's full
    transitive closure: the archive must be a leaf through every package
    it names (events, store, contract, redaction, attempt id, identity,
    provider), not just through the operator boundary.
    """
    archive_path = _module_path(ARCHIVE_MODULE)
    assert archive_path is not None, "coding_agent.operator.archive is missing"
    reached = _full_closure([ARCHIVE_MODULE])
    offenders: list[str] = []
    for module in sorted(reached):
        if module == ARCHIVE_MODULE:
            continue
        if module == LEDGER_PREFIX or module.startswith(LEDGER_PREFIX + "."):
            offenders.append(module)
            continue
        if module in _ARCHIVE_FORBIDDEN:
            offenders.append(module)
            continue
        path = reached[module]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for dependency in sorted(_imported_modules(tree, module)):
            if dependency == LEDGER_PREFIX or dependency.startswith(
                LEDGER_PREFIX + "."
            ):
                offenders.append(f"{module} -> {dependency}")
            elif dependency in _ARCHIVE_FORBIDDEN:
                offenders.append(f"{module} -> {dependency}")
    assert not offenders, (
        "modules reachable from operator.archive that break its leaf shape: "
        + ", ".join(offenders)
    )
