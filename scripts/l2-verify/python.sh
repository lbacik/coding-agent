#!/usr/bin/env bash
# Run inside the image by scripts/verify-l2-fixtures.sh, with
# fixtures/l2/python bind-mounted read-only at /opt/l2-fixture.
set -euo pipefail

cp -r /opt/l2-fixture /var/lib/coding-agent/workspaces/l2-python
mkdir -p /var/lib/coding-agent/workspaces/l2-python-evidence

agent validate --project-dir /var/lib/coding-agent/workspaces/l2-python \
  --evidence-dir /var/lib/coding-agent/workspaces/l2-python-evidence || true

echo "---targeted---"
agent validate --project-dir /var/lib/coding-agent/workspaces/l2-python \
  --evidence-dir /var/lib/coding-agent/workspaces/l2-python-evidence \
  --skip-bootstrap --targeted tests/test_greeting.py
