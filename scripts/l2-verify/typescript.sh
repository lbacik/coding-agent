#!/usr/bin/env bash
# Run inside the image by scripts/verify-l2-fixtures.sh, with
# fixtures/l2/typescript bind-mounted read-only at /opt/l2-fixture.
set -euo pipefail

cp -r /opt/l2-fixture /var/lib/coding-agent/workspaces/l2-typescript
mkdir -p /var/lib/coding-agent/workspaces/l2-typescript-evidence

agent validate --project-dir /var/lib/coding-agent/workspaces/l2-typescript \
  --evidence-dir /var/lib/coding-agent/workspaces/l2-typescript-evidence || true

echo "---targeted---"
agent validate --project-dir /var/lib/coding-agent/workspaces/l2-typescript \
  --evidence-dir /var/lib/coding-agent/workspaces/l2-typescript-evidence \
  --skip-bootstrap --targeted tests/greeting.test.ts
