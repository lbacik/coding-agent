#!/usr/bin/env bash
# Drift gate (issue #260): proves the schema suite catches a deliberate
# contract deviation. Corrupts one reply fixture (drops the required
# `capabilities` from an identity result) and asserts the validator
# rejects it. Exits 0 where the deviation fails as required, non-zero
# where the suite would let drift through.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

uv run --locked python -I - <<'PY'
import copy
import json
import sys

sys.path.insert(0, "src")
from coding_agent.conformance.schemas import validate_reply

with open("conformance/schemas/identity-result.schema.json", encoding="utf-8") as fh:
    print(f"schema pins: {json.load(fh)['title']}", file=sys.stderr)

envelope = {
    "contract": "1.0",
    "ok": True,
    "result": {
        "backend": "coding-agent",
        "agent_version": "0.1.0",
        "version": "0.1.0",
        "contract": "1.0",
        "repository": "example-owner/example-repo",
        "target_repository": "example-owner/example-repo",
        "mode": "worker",
        "process_started_at": "2026-11-07T00:00:00+00:00",
        "capabilities": ["next-issue", "stop-after"],
    },
}
assert validate_reply("identity", envelope) == [], "valid fixture must pass"

deviated = copy.deepcopy(envelope)
del deviated["result"]["capabilities"]
problems = validate_reply("identity", deviated)
if not problems:
    print("DRIFT GATE OPEN: corrupted identity reply validated", file=sys.stderr)
    sys.exit(1)
print(f"drift caught: {problems[0]}")
PY
