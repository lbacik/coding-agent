#!/usr/bin/env bash
# Drift gate (issue #359): proves the schema suite catches a deliberate
# contract deviation against the published v1.0.4 schemas. Corrupts the
# identity `contract` back to the pre-v1.0.4 string shape and asserts the
# validator rejects it. Exits 0 where the deviation fails as required,
# non-zero where the suite would let drift through.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

uv run --locked python -I - <<'PY'
import copy
import sys

sys.path.insert(0, "src")
from coding_agent.conformance.schemas import validate_reply

envelope = {
    "contract": "1.0",
    "ok": True,
    "result": {
        "backend": "coding-agent",
        "agent_version": "0.1.0",
        "contract": {"major": 1, "minor": 0},
        "repository": "example-owner/example-repo",
        "mode": "online",
        "process_started_at": "2026-11-07T00:00:00Z",
        "capabilities": ["next-issue", "stop-after"],
    },
}
assert validate_reply("identity", envelope) == [], "valid fixture must pass"

deviated = copy.deepcopy(envelope)
deviated["result"]["contract"] = "1.0"
problems = validate_reply("identity", deviated)
if not problems:
    print("DRIFT GATE OPEN: corrupted identity reply validated", file=sys.stderr)
    sys.exit(1)
print(f"drift caught: {problems[0]}")
PY
