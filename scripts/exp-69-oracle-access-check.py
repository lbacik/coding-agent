"""Re-check oracle access for a harness output directory (#69).

Reads every per-run record in ``OUT_DIR/runs/``, re-runs the same Transcript
scan the harness ran (``coding_agent.experiment.oracle_access``) over the raw
Transcript in ``OUT_DIR/evidence/run-<seq>/transcript.jsonl``, and prints,
per run, whether it was checked and what matched.

The signal set is the harness's: ``lbacik/coding-agent`` (never
``-sandbox``), ``fixtures/exp-69``, plus the oracle file basenames. The
oracle basenames are read from each run record's stored
``result.oracle.file_names`` (written by the harness from the protocol's
``oracle_files``); records written before that field existed fall back to
the dotted tokens of their stored oracle command, so the check needs no
protocol file and no harness.

Exit status is 0 where every run's stored ``oracle_access_check`` block
matches the recomputation, 1 otherwise, 2 on usage errors.

Usage: uv run python scripts/exp-69-oracle-access-check.py OUT_DIR
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coding_agent.experiment.oracle_access import (  # noqa: E402
    FIXTURES_SIGNAL,
    REPO_SIGNAL,
    build_oracle_access_check,
)


def _record_file_names(record: dict[str, Any]) -> list[str]:
    """The oracle basenames one run record contributes to the signal set.

    Prefers the harness-stored ``result.oracle.file_names``; older records
    without that field fall back to the dotted tokens of their stored oracle
    command.
    """
    result = record.get("result")
    if not isinstance(result, dict):
        return []
    oracle = result.get("oracle")
    if not isinstance(oracle, dict):
        return []
    stored = oracle.get("file_names")
    if isinstance(stored, list) and all(isinstance(name, str) for name in stored):
        return [name for name in stored if name]
    command = oracle.get("command")
    if not isinstance(command, list):
        return []
    names: list[str] = []
    for token in command:
        if not isinstance(token, str):
            continue
        base = Path(token).name
        if "." not in base or base.startswith("-"):
            continue
        if base not in names:
            names.append(base)
    return names


def _oracle_basenames(records: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for record in records:
        for name in _record_file_names(record):
            if name not in names:
                names.append(name)
    return names


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    out_dir = Path(argv[1])
    runs_dir = out_dir / "runs"
    if not runs_dir.is_dir():
        print(f"no runs directory: {runs_dir}", file=sys.stderr)
        return 2
    paths = sorted(runs_dir.glob("run-*.json"))
    if not paths:
        print(f"no run records in {runs_dir}", file=sys.stderr)
        return 2
    parsed: list[tuple[str, dict[str, Any] | None]] = []
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"{path.name}: unreadable record: {exc}")
            parsed.append((path.name, None))
            continue
        if not isinstance(record, dict):
            print(f"{path.name}: record is not a mapping")
            parsed.append((path.name, None))
            continue
        parsed.append((path.name, record))
    records = [record for _, record in parsed if record is not None]
    signals = [REPO_SIGNAL, FIXTURES_SIGNAL, *_oracle_basenames(records)]
    ok = True
    for name, record in parsed:
        if record is None:
            ok = False
            continue
        seq = record.get("seq")
        status = record.get("status")
        stored = record.get("oracle_access_check")
        if not isinstance(seq, int):
            print(f"{name}: no seq")
            ok = False
            continue
        transcript = out_dir / "evidence" / f"run-{seq:03d}" / "transcript.jsonl"
        recomputed = build_oracle_access_check(transcript, signals)
        match_summary = ", ".join(
            f"{m.get('signal')}@{m.get('seq')}" for m in recomputed["matches"]
        )
        print(
            f"{name}: status={status} "
            f"checked={recomputed['checked']} "
            f"digest={recomputed['transcript_digest']} "
            f"matches={len(recomputed['matches'])}"
            + (f" [{match_summary}]" if match_summary else "")
        )
        if not isinstance(stored, dict):
            print(f"  stored block missing or not a mapping")
            ok = False
            continue
        if (
            stored.get("checked") != recomputed["checked"]
            or stored.get("transcript_digest") != recomputed["transcript_digest"]
            or stored.get("matches") != recomputed["matches"]
        ):
            print(f"  MISMATCH: stored={json.dumps(stored, sort_keys=True)}")
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
