"""The ``agentctl`` contract CLI: one JSON envelope on stdout, distinct exit codes.

Contract: agent-operator-contract major 1 at ``06ee46c``
(``docs/spec.md`` §4, §4.1, §5, §6), ``examples/agentctl/`` fixtures at
that revision as reference shapes. There is no legacy grammar: without
``--contract`` as the first argument the call answers ``usage``.

One module owns the envelope, the error codes, the exit codes and the
fixed check order of §4.1 (contract → grammar → capability → values →
repository → idempotency → state). Capability gating comes before
argument validation. ``identity``, ``status``, ``stop``, ``resume`` and ``command`` are served by
the in-process control listener over the operator Unix socket (adoption
step 4, #248, #249); every other operation that needs an engine still
answers ``agent_unreachable`` until its ticket lands.
"""

from __future__ import annotations

import json
import re
import socket
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

CONTRACT_MAJOR: int = 1
CONTRACT_MINOR: int = 0
CONTRACT_VERSION: str = f"{CONTRACT_MAJOR}.{CONTRACT_MINOR}"
SUPPORTED_MAJORS: list[int] = [1]

# Capabilities coding-agent advertises in major 1. Core operations need no
# entry; ``pool`` is never advertised. ``stop-after`` (#251) and
# ``next-issue`` (#252) are both served by the control listener.
ADVERTISED_CAPABILITIES: frozenset[str] = frozenset({"next-issue", "stop-after"})

STOP_AFTER_MIN = 1
STOP_AFTER_MAX = 999

ErrorCode = Literal[
    "rejected",
    "repository_mismatch",
    "payload_mismatch",
    "invalid_argument",
    "usage",
    "unsupported",
    "contract_mismatch",
    "not_found",
    "agent_unreachable",
    "internal",
]

EXIT_FOR_CODE: dict[ErrorCode, int] = {
    "rejected": 1,
    "repository_mismatch": 1,
    "payload_mismatch": 1,
    "invalid_argument": 1,
    "usage": 2,
    "unsupported": 3,
    "contract_mismatch": 4,
    "not_found": 5,
    "agent_unreachable": 6,
    "internal": 7,
}

RETRYABLE_FOR_CODE: dict[ErrorCode, bool] = {
    "rejected": False,
    "repository_mismatch": False,
    "payload_mismatch": False,
    "invalid_argument": False,
    "usage": False,
    "unsupported": False,
    "contract_mismatch": False,
    "not_found": False,
    "agent_unreachable": True,
    "internal": True,
}

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class _ErrorBody(TypedDict):
    code: str
    message: str
    retryable: bool
    request_id: NotRequired[str]
    details: NotRequired[dict[str, object]]


class _Envelope(TypedDict):
    contract: str
    ok: bool
    result: NotRequired[dict[str, object]]
    error: NotRequired[_ErrorBody]


def _emit(envelope: _Envelope) -> None:
    sys.stdout.write(json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n")


def _emit_error(
    code: ErrorCode,
    message: str,
    request_id: str | None = None,
    details: dict[str, object] | None = None,
) -> None:
    body: _ErrorBody = {
        "code": code,
        "message": message,
        "retryable": RETRYABLE_FOR_CODE[code],
    }
    if request_id is not None and _REQUEST_ID_RE.match(request_id) is not None:
        body["request_id"] = request_id
    if details is not None:
        body["details"] = details
    _emit({"contract": CONTRACT_VERSION, "ok": False, "error": body})


def _is_valid_request_id(value: str) -> bool:
    return _REQUEST_ID_RE.match(value) is not None


def _is_valid_repo(value: str) -> bool:
    if "/" not in value:
        return False
    owner, _, repo = value.partition("/")
    if not owner or not repo or "/" in repo:
        return False
    if " " in value or "\t" in value or "\n" in value:
        return False
    return True


def _parse_mutation_flags(
    tokens: list[str],
) -> tuple[dict[str, str] | None, str | None]:
    """Parse ``--request-id``/``--expect-repo`` plus any extra flags.

    Returns ``(flags, usage_message)``. ``flags`` maps flag names without
    dashes to their values and may hold other ``--`` flags the caller
    validates (``--after``, ``--saved-at``, ``--path``, ``--limit``,
    ``--before``). A missing value or an unknown positional is a grammar
    failure (``usage``); value validity is checked later (``invalid_argument``).
    """
    flags: dict[str, str] = {}
    extra_values: dict[str, list[str]] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("--"):
            return None, f"unexpected argument {tok!r}"
        name = tok[2:]
        if name not in (
            "request-id",
            "expect-repo",
            "after",
            "saved-at",
            "path",
            "limit",
            "before",
        ):
            return None, f"unknown argument {tok!r}"
        if i + 1 >= len(tokens):
            return None, f"argument {tok}: expected one argument"
        value = tokens[i + 1]
        if name == "path":
            extra_values.setdefault("path", []).append(value)
        else:
            if name in flags:
                return None, f"argument {tok}: expected one argument"
            flags[name] = value
        i += 2
    if extra_values:
        flags["__paths"] = "\x00".join(extra_values["path"])
    return flags, None


def _request_id_of(tokens: list[str]) -> str | None:
    """Best-effort ``--request-id`` for ``unsupported`` errors (no validation)."""
    for i, tok in enumerate(tokens):
        if tok == "--request-id" and i + 1 < len(tokens):
            return tokens[i + 1]
    return None


def _offline_is_running() -> bool:
    """Whether a live Worker holds the volume lock (Offline Read probe)."""
    from coding_agent.operator import offline as offline_reader
    from coding_agent.operator import paths as operator_paths

    return offline_reader.instance_running(operator_paths.state_dir_path())


def _emit_rejected_instance_running(request_id: str | None = None) -> None:
    """Answer ``rejected`` with ``details.reason: "instance_running"`` (#257)."""
    from coding_agent.operator.offline import INSTANCE_RUNNING_REASON

    _emit_error(
        "rejected",
        "instance is running",
        request_id,
        {"reason": INSTANCE_RUNNING_REASON},
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: ``agentctl --contract 1 <operation>`` → envelope + exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return _run(args)
    except Exception:
        try:
            _emit_error("internal", "internal error")
        except Exception:
            pass
        return EXIT_FOR_CODE["internal"]


def _run(argv: list[str]) -> int:
    # §4.1 step 1: contract major.
    if not argv or argv[0] != "--contract":
        _emit_error("usage", "first argument must be --contract <major>")
        return EXIT_FOR_CODE["usage"]
    if len(argv) < 2:
        _emit_error("usage", "argument --contract: expected one argument")
        return EXIT_FOR_CODE["usage"]
    if argv[1] != str(CONTRACT_MAJOR):
        _emit_error(
            "contract_mismatch",
            "this instance speaks contract major 1",
            None,
            {"supported": list(SUPPORTED_MAJORS)},
        )
        return EXIT_FOR_CODE["contract_mismatch"]
    rest = argv[2:]

    # Global ``--offline`` immediately after the contract version.
    offline = False
    if rest and rest[0] == "--offline":
        offline = True
        rest = rest[1:]
    if not rest:
        _emit_error("usage", "expected an operation")
        return EXIT_FOR_CODE["usage"]

    op = rest[0]

    # Offline Read probes the lock first (#257): a live Worker holding the
    # volume lock means every offline call answers ``rejected`` with
    # ``instance_running``, before grammar, values or capability checks.
    if offline and _offline_is_running():
        _emit_rejected_instance_running(_request_id_of(rest))
        return EXIT_FOR_CODE["rejected"]

    # §4.1 step 2: grammar (usage). Each branch either emits usage or
    # returns a parsed shape for the capability gate.
    if op == "identity":
        if len(rest) != 1:
            _emit_error("usage", f"unexpected argument {rest[1]!r}")
            return EXIT_FOR_CODE["usage"]
        if offline:
            return _serve_identity_offline()
        return _serve_identity()
    if op == "status":
        if len(rest) != 1:
            _emit_error("usage", f"unexpected argument {rest[1]!r}")
            return EXIT_FOR_CODE["usage"]
        if offline:
            return _serve_status_offline()
        return _serve_status()
    if op == "command":
        if len(rest) != 2 or rest[1].startswith("--") or not rest[1]:
            _emit_error("usage", "expected <request-id> for 'command'")
            return EXIT_FOR_CODE["usage"]
        if offline:
            return _serve_command_offline(rest[1])
        return _serve_command(rest[1])
    if op == "attempts":
        return _run_attempts(rest[1:], offline)
    if op == "stop":
        return _run_stop(rest[1:], offline)
    if op == "resume":
        return _run_resume(rest[1:], offline)
    if op == "next":
        return _run_next(rest[1:], offline)
    if op == "handoff":
        return _run_handoff(rest[1:], offline)
    if op == "recovery":
        return _run_recovery(rest[1:], offline)
    if op == "errors":
        return _run_errors_reset(rest[1:], offline)
    _emit_error("usage", f"unknown operation {op!r}")
    return EXIT_FOR_CODE["usage"]


def _run_attempts(tokens: list[str], offline: bool) -> int:
    if not tokens:
        _emit_error("usage", "expected a subcommand for 'attempts'")
        return EXIT_FOR_CODE["usage"]
    sub = tokens[0]
    if sub == "list":
        flags, usage = _parse_mutation_flags(tokens[1:])
        if usage is not None:
            _emit_error("usage", usage)
            return EXIT_FOR_CODE["usage"]
        if flags is None:  # pragma: no cover - helper never returns None flags
            _emit_error("usage", "unexpected argument")
            return EXIT_FOR_CODE["usage"]
        # Values check (invalid_argument) comes after the capability gate;
        # ``attempts list`` is core so it reaches this step.
        if "request-id" in flags or "expect-repo" in flags:
            _emit_error("usage", "unexpected argument for 'attempts list'")
            return EXIT_FOR_CODE["usage"]
        if "after" in flags or "saved-at" in flags or "__paths" in flags:
            _emit_error("usage", "unexpected argument for 'attempts list'")
            return EXIT_FOR_CODE["usage"]
        limit = 20
        if "limit" in flags:
            raw = flags["limit"]
            try:
                limit = int(raw)
            except ValueError:
                _emit_error("invalid_argument", f"argument --limit: invalid value {raw!r}")
                return EXIT_FOR_CODE["invalid_argument"]
            if limit < 1 or limit > 100:
                _emit_error("invalid_argument", f"argument --limit: invalid value {raw!r}")
                return EXIT_FOR_CODE["invalid_argument"]
        return _serve_attempts_list(limit, flags.get("before"), offline)
    if sub == "show":
        if len(tokens) != 2 or tokens[1].startswith("--") or not tokens[1]:
            _emit_error("usage", "expected <attempt-id> for 'attempts show'")
            return EXIT_FOR_CODE["usage"]
        return _serve_attempts_show(tokens[1], offline)
    if sub == "export":
        if len(tokens) < 2 or tokens[1].startswith("--") or not tokens[1]:
            _emit_error("usage", "expected <attempt-id> for 'attempts export'")
            return EXIT_FOR_CODE["usage"]
        flags, usage = _parse_mutation_flags(tokens[2:])
        if usage is not None:
            _emit_error("usage", usage)
            return EXIT_FOR_CODE["usage"]
        if flags is None:  # pragma: no cover - helper never returns None flags
            _emit_error("usage", "unexpected argument")
            return EXIT_FOR_CODE["usage"]
        allowed = {"__paths"}
        if any(key not in allowed for key in flags):
            _emit_error("usage", "unexpected argument for 'attempts export'")
            return EXIT_FOR_CODE["usage"]
        raw_paths: list[str] = []
        if "__paths" in flags:
            raw_paths = flags["__paths"].split("\x00")
        return _serve_attempts_export(tokens[1], raw_paths, offline)
    _emit_error("usage", f"unknown subcommand {sub!r} for 'attempts'")
    return EXIT_FOR_CODE["usage"]


def _run_stop(tokens: list[str], offline: bool) -> int:
    # Grammar: optional ``--after N`` plus required mutation flags.
    after: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--after":
            if after is not None:
                _emit_error("usage", "argument --after: expected one argument")
                return EXIT_FOR_CODE["usage"]
            if i + 1 >= len(tokens):
                _emit_error("usage", "argument --after: expected one argument")
                return EXIT_FOR_CODE["usage"]
            after = tokens[i + 1]
            i += 2
        else:
            rest.append(tok)
            i += 1
    flags, usage = _parse_mutation_flags(rest)
    if usage is not None:
        _emit_error("usage", usage)
        return EXIT_FOR_CODE["usage"]
    assert flags is not None
    if "request-id" not in flags or "expect-repo" not in flags:
        _emit_error("usage", "argument --request-id and --expect-repo are required")
        return EXIT_FOR_CODE["usage"]
    if any(key not in ("request-id", "expect-repo") for key in flags):
        _emit_error("usage", "unexpected argument for 'stop'")
        return EXIT_FOR_CODE["usage"]

    # §4.1 step 3: capability before values. ``stop-after`` is advertised.
    if after is not None and "stop-after" not in ADVERTISED_CAPABILITIES:
        _emit_error(
            "unsupported",
            "this instance does not advertise stop-after",
            _request_id_of(tokens),
            {"capability": "stop-after"},
        )
        return EXIT_FOR_CODE["unsupported"]

    # §4.1 step 4: values, including ``--after`` (1 ≤ N ≤ 999).
    request_id = flags["request-id"]
    expect_repo = flags["expect-repo"]
    if not _is_valid_request_id(request_id):
        _emit_error(
            "invalid_argument",
            f"argument --request-id: invalid value {request_id!r}",
            request_id,
        )
        return EXIT_FOR_CODE["invalid_argument"]
    if not _is_valid_repo(expect_repo):
        _emit_error(
            "invalid_argument",
            f"argument --expect-repo: invalid value {expect_repo!r}",
            request_id,
        )
        return EXIT_FOR_CODE["invalid_argument"]
    after_value: int | None = None
    if after is not None:
        try:
            parsed = int(after)
        except ValueError:
            parsed = 0
        if parsed < STOP_AFTER_MIN or parsed > STOP_AFTER_MAX:
            _emit_error(
                "invalid_argument",
                f"argument --after: invalid value {after!r}",
                request_id,
            )
            return EXIT_FOR_CODE["invalid_argument"]
        after_value = parsed

    # Offline mutations answer unsupported (the lock probe in ``_run``
    # already rejected a live holder with ``instance_running``).
    if offline:
        _emit_error(
            "unsupported",
            "mutations are not available offline",
            request_id,
            {"offline": True},
        )
        return EXIT_FOR_CODE["unsupported"]
    return _serve_stop(request_id, expect_repo, after=after_value)


def _run_resume(tokens: list[str], offline: bool) -> int:
    flags, usage = _parse_mutation_flags(tokens)
    if usage is not None:
        _emit_error("usage", usage)
        return EXIT_FOR_CODE["usage"]
    assert flags is not None
    if "request-id" not in flags or "expect-repo" not in flags:
        _emit_error("usage", "argument --request-id and --expect-repo are required")
        return EXIT_FOR_CODE["usage"]
    if any(key not in ("request-id", "expect-repo") for key in flags):
        _emit_error("usage", "unexpected argument for 'resume'")
        return EXIT_FOR_CODE["usage"]
    request_id = flags["request-id"]
    expect_repo = flags["expect-repo"]
    if not _is_valid_request_id(request_id):
        _emit_error(
            "invalid_argument",
            f"argument --request-id: invalid value {request_id!r}",
            request_id,
        )
        return EXIT_FOR_CODE["invalid_argument"]
    if not _is_valid_repo(expect_repo):
        _emit_error(
            "invalid_argument",
            f"argument --expect-repo: invalid value {expect_repo!r}",
            request_id,
        )
        return EXIT_FOR_CODE["invalid_argument"]
    # Offline mutations answer unsupported (the lock probe in ``_run``
    # already rejected a live holder with ``instance_running``).
    if offline:
        _emit_error(
            "unsupported",
            "mutations are not available offline",
            request_id,
            {"offline": True},
        )
        return EXIT_FOR_CODE["unsupported"]
    return _serve_resume(request_id, expect_repo)


def _parse_next_issue(raw: str) -> int | None:
    """The validated priority issue number, or ``None`` where invalid."""
    if raw.startswith("+"):
        raw = raw[1:]
    if not raw.isdigit():
        return None
    try:
        number = int(raw)
    except ValueError:
        return None
    if number < 1:
        return None
    return number


def _run_next(tokens: list[str], offline: bool) -> int:
    # Grammar: ``next issue N`` plus required mutation flags.
    if len(tokens) < 2 or tokens[0] != "issue":
        _emit_error("usage", "expected 'next issue <N>'")
        return EXIT_FOR_CODE["usage"]
    issue = tokens[1]
    if not issue or issue.startswith("--"):
        _emit_error("usage", "expected <N> for 'next issue'")
        return EXIT_FOR_CODE["usage"]
    flags, usage = _parse_mutation_flags(tokens[2:])
    if usage is not None:
        _emit_error("usage", usage)
        return EXIT_FOR_CODE["usage"]
    assert flags is not None
    if "request-id" not in flags or "expect-repo" not in flags:
        _emit_error("usage", "argument --request-id and --expect-repo are required")
        return EXIT_FOR_CODE["usage"]
    if any(key not in ("request-id", "expect-repo") for key in flags):
        _emit_error("usage", "unexpected argument for 'next issue'")
        return EXIT_FOR_CODE["usage"]

    # §4.1 step 3: capability before values. ``next-issue`` is advertised.
    if "next-issue" not in ADVERTISED_CAPABILITIES:
        _emit_error(
            "unsupported",
            "this instance does not advertise next-issue",
            _request_id_of(tokens),
            {"capability": "next-issue"},
        )
        return EXIT_FOR_CODE["unsupported"]

    # §4.1 step 4: values, including the positive issue number.
    request_id = flags["request-id"]
    expect_repo = flags["expect-repo"]
    if not _is_valid_request_id(request_id):
        _emit_error(
            "invalid_argument",
            f"argument --request-id: invalid value {request_id!r}",
            request_id,
        )
        return EXIT_FOR_CODE["invalid_argument"]
    if not _is_valid_repo(expect_repo):
        _emit_error(
            "invalid_argument",
            f"argument --expect-repo: invalid value {expect_repo!r}",
            request_id,
        )
        return EXIT_FOR_CODE["invalid_argument"]
    issue_number = _parse_next_issue(issue)
    if issue_number is None:
        _emit_error(
            "invalid_argument",
            f"argument <N>: invalid value {issue!r}",
            request_id,
        )
        return EXIT_FOR_CODE["invalid_argument"]

    # Offline mutations answer unsupported (the lock probe in ``_run``
    # already rejected a live holder with ``instance_running``).
    if offline:
        _emit_error(
            "unsupported",
            "mutations are not available offline",
            request_id,
            {"offline": True},
        )
        return EXIT_FOR_CODE["unsupported"]
    return _serve_next(request_id, expect_repo, issue_number)


def _run_handoff(tokens: list[str], offline: bool = False) -> int:
    _ = offline
    if len(tokens) < 1 or tokens[0] != "now":
        _emit_error("usage", "expected 'handoff now'")
        return EXIT_FOR_CODE["usage"]
    flags, usage = _parse_mutation_flags(tokens[1:])
    if usage is not None:
        _emit_error("usage", usage)
        return EXIT_FOR_CODE["usage"]
    assert flags is not None
    if "request-id" not in flags or "expect-repo" not in flags:
        _emit_error("usage", "argument --request-id and --expect-repo are required")
        return EXIT_FOR_CODE["usage"]
    if any(key not in ("request-id", "expect-repo") for key in flags):
        _emit_error("usage", "unexpected argument for 'handoff now'")
        return EXIT_FOR_CODE["usage"]
    _emit_error(
        "unsupported",
        "this instance does not advertise operator-handoff",
        _request_id_of(tokens),
        {"capability": "operator-handoff"},
    )
    return EXIT_FOR_CODE["unsupported"]


def _run_recovery(tokens: list[str], offline: bool = False) -> int:
    _ = offline
    if not tokens:
        _emit_error("usage", "expected 'recovery retry' or 'recovery release'")
        return EXIT_FOR_CODE["usage"]
    sub = tokens[0]
    if sub == "retry":
        if len(tokens) < 2 or not tokens[1] or tokens[1].startswith("--"):
            _emit_error("usage", "expected <attempt-id> for 'recovery retry'")
            return EXIT_FOR_CODE["usage"]
        flags, usage = _parse_mutation_flags(tokens[2:])
        if usage is not None:
            _emit_error("usage", usage)
            return EXIT_FOR_CODE["usage"]
        assert flags is not None
        if "request-id" not in flags or "expect-repo" not in flags:
            _emit_error("usage", "argument --request-id and --expect-repo are required")
            return EXIT_FOR_CODE["usage"]
        if any(key not in ("request-id", "expect-repo") for key in flags):
            _emit_error("usage", "unexpected argument for 'recovery retry'")
            return EXIT_FOR_CODE["usage"]
        _emit_error(
            "unsupported",
            "this instance does not advertise recovery-retry",
            _request_id_of(tokens),
            {"capability": "recovery-retry"},
        )
        return EXIT_FOR_CODE["unsupported"]
    if sub == "release":
        if len(tokens) < 2 or not tokens[1] or tokens[1].startswith("--"):
            _emit_error("usage", "expected <attempt-id> for 'recovery release'")
            return EXIT_FOR_CODE["usage"]
        flags, usage = _parse_mutation_flags(tokens[2:])
        if usage is not None:
            _emit_error("usage", usage)
            return EXIT_FOR_CODE["usage"]
        assert flags is not None
        if "request-id" not in flags or "expect-repo" not in flags or "saved-at" not in flags:
            _emit_error(
                "usage",
                "argument --saved-at, --request-id and --expect-repo are required",
            )
            return EXIT_FOR_CODE["usage"]
        if any(key not in ("request-id", "expect-repo", "saved-at") for key in flags):
            _emit_error("usage", "unexpected argument for 'recovery release'")
            return EXIT_FOR_CODE["usage"]
        _emit_error(
            "unsupported",
            "this instance does not advertise recovery-release",
            _request_id_of(tokens),
            {"capability": "recovery-release"},
        )
        return EXIT_FOR_CODE["unsupported"]
    _emit_error("usage", f"unknown subcommand {sub!r} for 'recovery'")
    return EXIT_FOR_CODE["usage"]


def _run_errors_reset(tokens: list[str], offline: bool = False) -> int:
    _ = offline
    if not tokens or tokens[0] != "reset":
        if not tokens:
            _emit_error("usage", "expected 'errors reset'")
        else:
            _emit_error("usage", f"unexpected argument {tokens[0]!r} for 'errors'")
        return EXIT_FOR_CODE["usage"]
    flags, usage = _parse_mutation_flags(tokens[1:])
    if usage is not None:
        _emit_error("usage", usage)
        return EXIT_FOR_CODE["usage"]
    assert flags is not None
    if "request-id" not in flags or "expect-repo" not in flags:
        _emit_error("usage", "argument --request-id and --expect-repo are required")
        return EXIT_FOR_CODE["usage"]
    if any(key not in ("request-id", "expect-repo") for key in flags):
        _emit_error("usage", "unexpected argument for 'errors reset'")
        return EXIT_FOR_CODE["usage"]
    _emit_error(
        "unsupported",
        "this instance does not advertise errors-reset",
        _request_id_of(tokens),
        {"capability": "errors-reset"},
    )
    return EXIT_FOR_CODE["unsupported"]


def _serve_attempts_list(limit: int, before: str | None, offline: bool) -> int:
    """Serve ``attempts list`` from the volume, newest first (#255, #257).

    A volume-direct read in this process — no engine dialled — so the same
    archive code path serves online and ``--offline`` alike. Online reads
    the ledger file directly; offline (whose live holder ``_run`` already
    rejected with ``instance_running``) reads through the ledger copy, so an
    uncheckpointed WAL shows the latest committed state while the source
    directory is unchanged. An unknown ``--before`` cursor answers
    ``invalid_argument``; an unreadable ledger answers ``internal``. A
    missing ledger file means no Attempt ever opened: the page is empty.
    """
    from coding_agent.operator import attempts as attempt_archives
    from coding_agent.operator import paths as operator_paths

    state_dir = operator_paths.state_dir_path()
    ledger_path = operator_paths.ledger_path_for_state(state_dir)
    if offline:
        from coding_agent.operator import offline as offline_reader

        try:
            page, next_cursor = offline_reader.load_offline_attempts_page(
                ledger_path, limit=limit, before=before, state_dir=state_dir
            )
        except attempt_archives.InvalidCursor:
            _emit_error(
                "invalid_argument",
                f"argument --before: invalid value {before!r}",
            )
            return EXIT_FOR_CODE["invalid_argument"]
        except ValueError:
            _emit_error(
                "invalid_argument", f"argument --limit: invalid value {limit!r}"
            )
            return EXIT_FOR_CODE["invalid_argument"]
        except (OSError, sqlite3.Error):
            _emit_error("internal", "internal error")
            return EXIT_FOR_CODE["internal"]
        _emit(
            {
                "contract": CONTRACT_VERSION,
                "ok": True,
                "result": {"attempts": page, "next_cursor": next_cursor},
            }
        )
        return 0
    try:
        page, next_cursor = attempt_archives.list_attempts_page(
            ledger_path, limit=limit, before=before
        )
    except attempt_archives.InvalidCursor:
        _emit_error(
            "invalid_argument",
            f"argument --before: invalid value {before!r}",
        )
        return EXIT_FOR_CODE["invalid_argument"]
    except ValueError:
        _emit_error("invalid_argument", f"argument --limit: invalid value {limit!r}")
        return EXIT_FOR_CODE["invalid_argument"]
    except (OSError, sqlite3.Error):
        _emit_error("internal", "internal error")
        return EXIT_FOR_CODE["internal"]
    _emit(
        {
            "contract": CONTRACT_VERSION,
            "ok": True,
            "result": {"attempts": page, "next_cursor": next_cursor},
        }
    )
    return 0


def _serve_attempts_show(attempt_id: str, offline: bool) -> int:
    """Serve ``attempts show`` from the volume: summary plus manifest (#255, #257).

    The summary comes from the ledger (an Attempt shows as soon as its
    Claim opens, even while it runs); the manifest is computed on read from
    the Attempt Archive. ``None`` means no Attempt is recorded for the id —
    the caller answers ``not_found``. A missing ledger file likewise holds
    no Attempt. Volume-direct like ``attempts list``: online reads the
    ledger file directly while offline reads through the copy (the lock
    probe in ``_run`` already rejected a live holder), sharing the one
    archive code path.
    """
    from coding_agent.operator import attempts as attempt_archives
    from coding_agent.operator import paths as operator_paths

    state_dir: Path = operator_paths.state_dir_path()
    ledger_path = operator_paths.ledger_path_for_state(state_dir)
    if offline:
        from coding_agent.operator import offline as offline_reader

        try:
            shown = offline_reader.load_offline_attempt(
                ledger_path, state_dir, attempt_id
            )
        except (OSError, sqlite3.Error):
            _emit_error("internal", "internal error")
            return EXIT_FOR_CODE["internal"]
        if shown is None:
            _emit_error("not_found", f"no attempt is recorded for {attempt_id}")
            return EXIT_FOR_CODE["not_found"]
        _emit({"contract": CONTRACT_VERSION, "ok": True, "result": shown})
        return 0
    try:
        shown = attempt_archives.show_attempt(
            ledger_path=ledger_path, state_dir=state_dir, attempt_id=attempt_id
        )
    except (OSError, sqlite3.Error):
        _emit_error("internal", "internal error")
        return EXIT_FOR_CODE["internal"]
    if shown is None:
        _emit_error("not_found", f"no attempt is recorded for {attempt_id}")
        return EXIT_FOR_CODE["not_found"]
    _emit({"contract": CONTRACT_VERSION, "ok": True, "result": shown})
    return 0


def _serve_attempts_export(
    attempt_id: str, raw_paths: list[str], offline: bool
) -> int:
    """Serve ``attempts export`` from the volume as a pax tar (#256, #257).

    Streams an uncompressed pax tar whose first member is ``manifest.json``,
    then the evidence files. ``--path`` restricts the tar to that member
    (or subtree). An unknown Attempt or path answers ``not_found`` before
    any streaming starts, so no tar bytes reach stdout. Files stream from
    the volume, so a torn read of an unsealed file shows up as a ``sha256``
    mismatch for the Consumer. Volume-direct like ``list``/``show``: online
    reads the ledger file directly while offline reads through the copy
    (the lock probe in ``_run`` already rejected a live holder), sharing
    the one archive code path.
    """

    from coding_agent.operator import attempts as attempt_archives
    from coding_agent.operator import paths as operator_paths

    state_dir: Path = operator_paths.state_dir_path()
    ledger_path = operator_paths.ledger_path_for_state(state_dir)
    shown: dict[str, Any] | None
    archive_dir: Path | None
    if offline:
        from coding_agent.operator import offline as offline_reader

        try:
            loaded = offline_reader.load_offline_export(
                ledger_path, state_dir, attempt_id
            )
        except (OSError, sqlite3.Error):
            _emit_error("internal", "internal error")
            return EXIT_FOR_CODE["internal"]
        if loaded is None:
            _emit_error("not_found", f"no attempt is recorded for {attempt_id}")
            return EXIT_FOR_CODE["not_found"]
        shown, archive_dir = loaded
    else:
        try:
            shown = attempt_archives.show_attempt(
                ledger_path=ledger_path, state_dir=state_dir, attempt_id=attempt_id
            )
        except (OSError, sqlite3.Error):
            _emit_error("internal", "internal error")
            return EXIT_FOR_CODE["internal"]
        if shown is None:
            _emit_error("not_found", f"no attempt is recorded for {attempt_id}")
            return EXIT_FOR_CODE["not_found"]
        archive_dir = None
    assert shown is not None
    base_manifest: list[dict[str, object]] = list(shown.get("manifest", []))
    attempt_summary: dict[str, object] = dict(shown.get("attempt", {}))
    sealed = attempt_summary.get("sealed") is True
    sealed_at_raw = attempt_summary.get("sealed_at")
    sealed_at: str | None = (
        str(sealed_at_raw) if isinstance(sealed_at_raw, str) else None
    )
    try:
        filtered = attempt_archives.filter_manifest_entries(
            base_manifest, raw_paths
        )
    except Exception:
        _emit_error("internal", "internal error")
        return EXIT_FOR_CODE["internal"]
    if filtered is None:
        if raw_paths:
            _emit_error(
                "not_found", f"no evidence is recorded for path {raw_paths[0]!r}"
            )
        else:
            _emit_error("not_found", f"no attempt is recorded for {attempt_id}")
        return EXIT_FOR_CODE["not_found"]
    document = attempt_archives.export_manifest_document(
        attempt_id=attempt_id,
        sealed=sealed,
        sealed_at=sealed_at,
        entries=filtered,
    )
    if not offline:
        archive_dir = None
        try:
            from coding_agent.operator.attempts import (
                archive_dir_for as _archive_dir_for,
            )

            repository: str | None = None
            try:
                connection = sqlite3.connect(str(ledger_path), timeout=2.0)
                try:
                    row = connection.execute(
                        "SELECT target_repository FROM operator_instance WHERE id = 1"
                    ).fetchone()
                finally:
                    connection.close()
                if row is not None and row[0]:
                    repository = str(row[0])
            except (OSError, sqlite3.Error):
                repository = None
            if repository is not None:
                try:
                    archive_dir = _archive_dir_for(
                        state_dir, repository, attempt_id
                    )
                except ValueError:
                    archive_dir = None
        except Exception:
            archive_dir = None
    binary = getattr(sys.stdout, "buffer", sys.stdout)
    try:
        attempt_archives.write_export_tar(
            archive_dir=archive_dir,
            document=document,
            entries=filtered,
            fileobj=binary,
        )
        try:
            binary.flush()
        except Exception:
            pass
    except OSError:
        _emit_error("internal", "internal error")
        return EXIT_FOR_CODE["internal"]
    return 0


def _needs_engine(
    request_id: str | None, offline: bool, *, is_mutation: bool
) -> int:
    """Every remaining engine operation still answers unreachable (#248).

    ``identity`` and ``status`` go through the socket below; each later
    operation moves over when its ticket lands.
    """
    _ = offline
    if is_mutation:
        assert request_id is not None
        _emit_error(
            "agent_unreachable",
            "cannot reach the agent process; the outcome is unknown, "
            "retry with the same request ID",
            request_id,
        )
    else:
        _emit_error("agent_unreachable", "cannot reach the agent process")
    return EXIT_FOR_CODE["agent_unreachable"]


class _EngineUnreachable(Exception):
    """The socket is missing, refused, or closed without answering."""


class _EngineMalformed(Exception):
    """The engine answered, but the answer is not a usable result."""


_ENGINE_TIMEOUT_SECONDS = 5.0
_ENGINE_MAX_REPLY_BYTES = 1024 * 1024


def _query_engine(request_body: dict[str, Any]) -> dict[str, Any]:
    """Ask the live Worker over the operator Unix socket (#248, #249).

    ``request_body`` carries the op and its fields (``request_id`` and
    ``expect_repo`` for mutations); the engine answers one JSON object.
    Only a Unix socket is ever dialled: there is no network listener.
    Raises :class:`_EngineUnreachable` where the socket is missing,
    refused or closed without answering, and :class:`_EngineMalformed`
    where the answer is not a usable result dict.
    """
    from coding_agent.operator.paths import client_socket_path

    path = client_socket_path()
    request = (json.dumps(request_body) + "\n").encode("utf-8")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(_ENGINE_TIMEOUT_SECONDS)
        try:
            client.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            raise _EngineUnreachable(str(exc)) from exc
        try:
            client.sendall(request)
        except OSError as exc:
            raise _EngineUnreachable(str(exc)) from exc
        data = b""
        while not data.endswith(b"\n"):
            try:
                chunk = client.recv(4096)
            except (TimeoutError, OSError) as exc:
                raise _EngineUnreachable(str(exc)) from exc
            if not chunk:
                break
            data += chunk
            if len(data) > _ENGINE_MAX_REPLY_BYTES + 1:
                raise _EngineMalformed("engine reply too large")
        if not data.strip():
            raise _EngineUnreachable("engine closed without answering")
        try:
            reply: Any = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _EngineMalformed(str(exc)) from exc
        if not isinstance(reply, dict):
            raise _EngineMalformed("engine reply is not an object")
        if reply.get("malformed") is True:
            raise _EngineMalformed("engine could not serve the operation")
        return reply
    finally:
        try:
            client.close()
        except OSError:
            pass


def _valid_identity(result: dict[str, Any]) -> bool:
    """Whether ``result`` carries every ``identity`` field the contract needs.

    Contract ``v1.0.4``: ``contract`` is the ``{"major", "minor"}`` object
    (the envelope keeps the ``"major.minor"`` string), ``mode`` is
    ``online``/``offline``, and ``process_started_at`` is ``None`` exactly
    for ``offline``.
    """
    if result.get("backend") != "coding-agent":
        return False
    if "agent_version" not in result:
        return False
    contract = result.get("contract")
    if not isinstance(contract, dict):
        return False
    if contract.get("major") != CONTRACT_MAJOR:
        return False
    if "repository" not in result:
        return False
    if result.get("mode") not in ("online", "offline"):
        return False
    if result.get("mode") == "offline":
        if result.get("process_started_at") is not None:
            return False
    elif not isinstance(result.get("process_started_at"), str):
        return False
    if "capabilities" not in result:
        return False
    return True


def _valid_status(result: dict[str, Any]) -> bool:
    """Whether ``result`` carries the minimal ``status`` sections (``v1.0.4``)."""
    instance = result.get("instance")
    intake = result.get("intake")
    identity = result.get("identity")
    if not isinstance(instance, dict) or "state" not in instance:
        return False
    if not isinstance(intake, dict) or "state" not in intake:
        return False
    if not isinstance(identity, dict) or not _valid_identity(identity):
        return False
    if "generated_at" not in result:
        return False
    if "recent_events" not in result or "recent_commands" not in result:
        return False
    return True


def _serve_identity() -> int:
    """Serve ``identity`` from the live Worker or classify the failure."""
    try:
        result = _query_engine({"op": "identity"})
    except _EngineUnreachable:
        _emit_error("agent_unreachable", "cannot reach the agent process")
        return EXIT_FOR_CODE["agent_unreachable"]
    except _EngineMalformed:
        _emit_error("internal", "internal error")
        return EXIT_FOR_CODE["internal"]
    if not _valid_identity(result):
        _emit_error("internal", "internal error")
        return EXIT_FOR_CODE["internal"]
    _emit({"contract": CONTRACT_VERSION, "ok": True, "result": result})
    return 0


def _serve_status() -> int:
    """Serve ``status`` from the live Worker or classify the failure."""
    try:
        result = _query_engine({"op": "status"})
    except _EngineUnreachable:
        _emit_error("agent_unreachable", "cannot reach the agent process")
        return EXIT_FOR_CODE["agent_unreachable"]
    except _EngineMalformed:
        _emit_error("internal", "internal error")
        return EXIT_FOR_CODE["internal"]
    if not _valid_status(result):
        _emit_error("internal", "internal error")
        return EXIT_FOR_CODE["internal"]
    _emit({"contract": CONTRACT_VERSION, "ok": True, "result": result})
    return 0


def _serve_identity_offline() -> int:
    """Serve offline ``identity`` from the ledger copy without starting (#257)."""
    from coding_agent.operator import paths as operator_paths

    from coding_agent.operator import offline as offline_reader

    state_dir = operator_paths.state_dir_path()
    ledger_path = operator_paths.ledger_path_for_state(state_dir)
    try:
        result = offline_reader.load_offline_identity(ledger_path)
    except (OSError, sqlite3.Error):
        _emit_error("internal", "internal error")
        return EXIT_FOR_CODE["internal"]
    _emit({"contract": CONTRACT_VERSION, "ok": True, "result": result})
    return 0


def _serve_status_offline() -> int:
    """Serve offline ``status`` as stopped with the last recorded rows (#257)."""
    from coding_agent.operator import paths as operator_paths

    from coding_agent.operator import offline as offline_reader

    state_dir = operator_paths.state_dir_path()
    ledger_path = operator_paths.ledger_path_for_state(state_dir)
    try:
        result = offline_reader.load_offline_status(ledger_path)
    except (OSError, sqlite3.Error):
        _emit_error("internal", "internal error")
        return EXIT_FOR_CODE["internal"]
    _emit({"contract": CONTRACT_VERSION, "ok": True, "result": result})
    return 0


def _serve_command_offline(request_id: str) -> int:
    """Serve offline ``command <request-id>`` from the ledger copy (#257)."""
    from coding_agent.operator import paths as operator_paths

    from coding_agent.operator import offline as offline_reader

    state_dir = operator_paths.state_dir_path()
    ledger_path = operator_paths.ledger_path_for_state(state_dir)
    try:
        command = offline_reader.load_offline_command(ledger_path, request_id)
    except (OSError, sqlite3.Error):
        _emit_error("internal", "internal error")
        return EXIT_FOR_CODE["internal"]
    if command is None:
        _emit_error(
            "not_found",
            f"no command is recorded for {request_id}",
        )
        return EXIT_FOR_CODE["not_found"]
    _emit({"contract": CONTRACT_VERSION, "ok": True, "result": {"command": command}})
    return 0


def _serve_mutation_result(
    *, op: str, request_id: str, expect_repo: str, after: int | None = None
) -> int:
    """Serve ``stop``/``resume`` from the live Worker (#249, §4, §6.2, §8).

    Exit 0 means the command is durably committed; ``replayed`` tells a
    retry it changed nothing. Guard failures map to the contract's error
    codes: ``repository_mismatch``, ``payload_mismatch`` and
    ``invalid_argument`` exit 1 and store nothing new, ``internal`` with
    ``details.stored: false`` exits 7.
    """
    body: dict[str, Any] = {"op": op, "request_id": request_id, "expect_repo": expect_repo}
    if after is not None:
        body["after"] = after
    try:
        reply = _query_engine(body)
    except _EngineUnreachable:
        _emit_error(
            "agent_unreachable",
            "cannot reach the agent process; the outcome is unknown, "
            "retry with the same request ID",
            request_id,
        )
        return EXIT_FOR_CODE["agent_unreachable"]
    except _EngineMalformed:
        _emit_error("internal", "internal error", request_id)
        return EXIT_FOR_CODE["internal"]
    if "command" in reply:
        command = reply["command"]
        if not isinstance(command, dict):
            _emit_error("internal", "internal error", request_id)
            return EXIT_FOR_CODE["internal"]
        replayed = reply.get("replayed") is True
        _emit(
            {
                "contract": CONTRACT_VERSION,
                "ok": True,
                "result": {"command": command, "replayed": replayed},
            }
        )
        return 0
    code = reply.get("error_code")
    if code == "repository_mismatch":
        repository = reply.get("repository")
        target = str(repository) if isinstance(repository, str) else ""
        _emit_error(
            "repository_mismatch",
            f"expected {expect_repo}, this instance targets {target}",
            request_id,
            {"repository": target},
        )
        return EXIT_FOR_CODE["repository_mismatch"]
    if code == "payload_mismatch":
        _emit_error(
            "payload_mismatch",
            f"request {request_id} was already used for a different command",
            request_id,
        )
        return EXIT_FOR_CODE["payload_mismatch"]
    if code == "invalid_argument":
        _emit_error(
            "invalid_argument",
            f"argument --after: invalid value {after!r}"
            if op == "stop" and after is not None
            else "invalid argument",
            request_id,
        )
        return EXIT_FOR_CODE["invalid_argument"]
    if code == "internal":
        _emit_error(
            "internal",
            "internal error",
            request_id,
            {"stored": False},
        )
        return EXIT_FOR_CODE["internal"]
    _emit_error("internal", "internal error", request_id)
    return EXIT_FOR_CODE["internal"]


def _serve_stop(request_id: str, expect_repo: str, *, after: int | None = None) -> int:
    """Serve ``stop``: close Intake (drain) through the live Worker."""
    return _serve_mutation_result(
        op="stop", request_id=request_id, expect_repo=expect_repo, after=after
    )


def _serve_resume(request_id: str, expect_repo: str) -> int:
    """Serve ``resume``: reopen Intake through the live Worker."""
    return _serve_mutation_result(
        op="resume", request_id=request_id, expect_repo=expect_repo
    )


def _serve_next(request_id: str, expect_repo: str, issue: int) -> int:
    """Serve ``next issue N``: record the priority through the live Worker.

    Exit 0 means the priority is durably committed; ``replayed`` tells a
    retry it changed nothing. Guard failures map to the contract's error
    codes: ``repository_mismatch``, ``payload_mismatch`` and
    ``rejected`` (a definitely ineligible issue, with the guard reason
    in ``details``) exit 1 and store nothing new, ``internal`` with
    ``details.stored: false`` exits 7.
    """
    try:
        reply = _query_engine(
            {
                "op": "next",
                "request_id": request_id,
                "expect_repo": expect_repo,
                "issue": issue,
            }
        )
    except _EngineUnreachable:
        _emit_error(
            "agent_unreachable",
            "cannot reach the agent process; the outcome is unknown, "
            "retry with the same request ID",
            request_id,
        )
        return EXIT_FOR_CODE["agent_unreachable"]
    except _EngineMalformed:
        _emit_error("internal", "internal error", request_id)
        return EXIT_FOR_CODE["internal"]
    if "command" in reply:
        command = reply["command"]
        if not isinstance(command, dict):
            _emit_error("internal", "internal error", request_id)
            return EXIT_FOR_CODE["internal"]
        replayed = reply.get("replayed") is True
        _emit(
            {
                "contract": CONTRACT_VERSION,
                "ok": True,
                "result": {"command": command, "replayed": replayed},
            }
        )
        return 0
    code = reply.get("error_code")
    if code == "repository_mismatch":
        repository = reply.get("repository")
        target = str(repository) if isinstance(repository, str) else ""
        _emit_error(
            "repository_mismatch",
            f"expected {expect_repo}, this instance targets {target}",
            request_id,
            {"repository": target},
        )
        return EXIT_FOR_CODE["repository_mismatch"]
    if code == "payload_mismatch":
        _emit_error(
            "payload_mismatch",
            f"request {request_id} was already used for a different command",
            request_id,
        )
        return EXIT_FOR_CODE["payload_mismatch"]
    if code == "invalid_argument":
        _emit_error(
            "invalid_argument",
            f"argument <N>: invalid value {issue!r}",
            request_id,
        )
        return EXIT_FOR_CODE["invalid_argument"]
    if code == "rejected":
        details = reply.get("details")
        contracted: dict[str, object] = (
            dict(details) if isinstance(details, dict) else {}
        )
        reason = contracted.get("reason")
        message = (
            f"issue #{issue} is not eligible: {reason}"
            if isinstance(reason, str) and reason
            else f"issue #{issue} is not eligible"
        )
        _emit_error("rejected", message, request_id, contracted or None)
        return EXIT_FOR_CODE["rejected"]
    if code == "internal":
        _emit_error(
            "internal",
            "internal error",
            request_id,
            {"stored": False},
        )
        return EXIT_FOR_CODE["internal"]
    _emit_error("internal", "internal error", request_id)
    return EXIT_FOR_CODE["internal"]


def _serve_command(request_id: str) -> int:
    """Serve ``command <request-id>`` from the live Worker (#249, §6.1).

    A stored command answers exit 0 with ``{"command": ...}``; an
    unknown ID answers ``not_found`` (exit 5).
    """
    try:
        reply = _query_engine({"op": "command", "request_id": request_id})
    except _EngineUnreachable:
        _emit_error("agent_unreachable", "cannot reach the agent process")
        return EXIT_FOR_CODE["agent_unreachable"]
    except _EngineMalformed:
        _emit_error("internal", "internal error")
        return EXIT_FOR_CODE["internal"]
    if "command" in reply:
        command = reply["command"]
        if not isinstance(command, dict):
            _emit_error("internal", "internal error")
            return EXIT_FOR_CODE["internal"]
        _emit({"contract": CONTRACT_VERSION, "ok": True, "result": {"command": command}})
        return 0
    if reply.get("error_code") == "not_found":
        _emit_error(
            "not_found",
            f"no command is recorded for {request_id}",
        )
        return EXIT_FOR_CODE["not_found"]
    if reply.get("error_code") == "internal":
        # A read path: ``command`` stores nothing, so no ``stored`` flag
        # applies (unlike ``stop``/``resume`` in ``_serve_mutation_result``).
        _emit_error("internal", "internal error")
        return EXIT_FOR_CODE["internal"]
    _emit_error("internal", "internal error")
    return EXIT_FOR_CODE["internal"]
