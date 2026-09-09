#!/usr/bin/env bash
# Run inside the image by scripts/verify-l2-fixtures.sh, with
# fixtures/l2/php bind-mounted read-only at /opt/l2-fixture.
set -euo pipefail

cp -r /opt/l2-fixture /var/lib/coding-agent/workspaces/l2-php
mkdir -p /var/lib/coding-agent/workspaces/l2-php-evidence

agent validate --project-dir /var/lib/coding-agent/workspaces/l2-php \
  --evidence-dir /var/lib/coding-agent/workspaces/l2-php-evidence || true

echo "---targeted---"
agent validate --project-dir /var/lib/coding-agent/workspaces/l2-php \
  --evidence-dir /var/lib/coding-agent/workspaces/l2-php-evidence \
  --skip-bootstrap --targeted tests/GreetingTest.php

echo "---service---"
# A real, reachable TCP listener for check_service.py to probe — proves the
# declared `services` entry is exercised against something real, not only
# round-tripped through the parser. `accept()` blocks so the socket stays
# open (and listening) until check_service.py actually connects to it.
python3 -c "
import socket
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', 9000))
s.listen(1)
s.accept()
" &
LISTENER_PID=$!
sleep 0.2
L2_FIXTURE_CACHE_URL=127.0.0.1:9000 python3 /var/lib/coding-agent/workspaces/l2-php/check_service.py
wait "$LISTENER_PID" 2>/dev/null || true
