#!/usr/bin/env bash
# Verifies the S1a "Done when" bullets against a real build: the Supported
# Toolchain Matrix, uid 1000, a read-only root filesystem apart from the
# volume and /tmp, and L2-20 (uv never fetches an interpreter). Exercises the
# image the way a real bootstrap would rather than reading the Dockerfile.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

IMAGE_TAG="${IMAGE_TAG:-coding-agent:verify}"
VOLUME_NAME="coding-agent-verify-$$"
FAILED=0

pass() { echo "[PASS] $1"; }
fail() { echo "[FAIL] $1" >&2; FAILED=1; }

cleanup() { docker volume rm -f "$VOLUME_NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "Building ${IMAGE_TAG}..."
docker build -t "$IMAGE_TAG" .

echo "--- uid and HOME ---"
ID_OUTPUT="$(docker run --rm "$IMAGE_TAG" id)"
if [ "$ID_OUTPUT" = "uid=1000(agent) gid=1000(agent) groups=1000(agent)" ]; then
  pass "runs as non-root agent, uid 1000"
else
  fail "expected uid 1000(agent), got: $ID_OUTPUT"
fi

echo "--- Supported Toolchain Matrix ---"
MATRIX="$(docker run --rm "$IMAGE_TAG" cat /opt/coding-agent/toolchain-matrix.json)"
echo "$MATRIX" | python3 -c "
import json, sys
matrix = json.load(sys.stdin)
toolchains = matrix['toolchains']
assert toolchains['python']['version'].startswith('3.13'), toolchains['python']
assert toolchains['php']['version'].startswith('8.4'), toolchains['php']
assert toolchains['node']['version'].startswith('22.'), toolchains['node']
assert toolchains['python']['package_manager']['name'] == 'uv'
assert toolchains['php']['package_manager']['name'] == 'composer'
assert toolchains['node']['package_manager']['name'] == 'pnpm'
" && pass "matrix publishes python 3.13 / php 8.4 / node 22 with their package managers" \
  || fail "matrix missing, malformed, or off the pinned versions"

echo "--- L2-20: UV_PYTHON_DOWNLOADS=never ---"
if docker run --network none --rm "$IMAGE_TAG" uv python install 3.12 >/tmp/verify-l2-20.log 2>&1; then
  fail "uv python install succeeded — it must refuse, not fetch"
elif grep -q 'Python downloads are not allowed' /tmp/verify-l2-20.log; then
  pass "uv refuses an out-of-matrix interpreter instead of downloading it"
else
  fail "uv failed for an unexpected reason: $(cat /tmp/verify-l2-20.log)"
fi
rm -f /tmp/verify-l2-20.log

echo "--- read-only root filesystem, volume, and /tmp ---"
docker volume create "$VOLUME_NAME" >/dev/null
if docker run --rm --read-only --tmpfs /tmp --user 1000:1000 \
  -e HOME=/home/agent \
  -v "$VOLUME_NAME:/var/lib/coding-agent" \
  "$IMAGE_TAG" bash -c '
    set -e
    mkdir -p /var/lib/coding-agent/workspaces/verify/{py,php,ts}
    cd /var/lib/coding-agent/workspaces/verify/ts
    echo "{\"name\":\"verify\"}" > package.json
    pnpm add left-pad >/dev/null 2>&1
    cd /var/lib/coding-agent/workspaces/verify/php
    echo "{\"name\":\"verify/verify\",\"require\":{\"psr/log\":\"^3.0\"}}" > composer.json
    composer install --no-interaction --no-progress -q
    composer config --global home >/dev/null
    cd /var/lib/coding-agent/workspaces/verify/py
    printf "[project]\nname=\"verify\"\nversion=\"0.1.0\"\nrequires-python=\">=3.13\"\ndependencies=[\"requests\"]\n\n[build-system]\nrequires=[\"hatchling\"]\nbuild-backend=\"hatchling.build\"\n" > pyproject.toml
    mkdir -p src/verify && touch src/verify/__init__.py
    uv sync -q
    touch /tmp/probe
    ! touch /opt/probe 2>/dev/null
  ' >/tmp/verify-rootfs.log 2>&1; then
  pass "pnpm, composer and uv bootstrap real projects under a read-only root filesystem"
else
  fail "toolchain bootstrap under read-only rootfs failed: $(cat /tmp/verify-rootfs.log)"
fi
rm -f /tmp/verify-rootfs.log

if [ "$FAILED" -eq 0 ]; then
  echo "verify-image: all checks passed"
else
  echo "verify-image: FAILED" >&2
fi
exit "$FAILED"
