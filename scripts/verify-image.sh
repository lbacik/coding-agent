#!/usr/bin/env bash
# Verifies the base image (the default `docker build .` output) and the
# test-only reference deployment image (`--target deployment`): the base
# carries only coding-agent, agent-installer and their own dependencies,
# while the deployment adds the pinned Target Project toolchains, the
# Supported Toolchain Matrix, and the Skill Bundle's three verification
# layers (L2-19), including one induced build failure. Exercises the images
# the way a real bootstrap would rather than reading the Dockerfile.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

BASE_IMAGE_TAG="${BASE_IMAGE_TAG:-coding-agent:base}"
IMAGE_TAG="${IMAGE_TAG:-coding-agent:verify}"
VOLUME_NAME="coding-agent-verify-$$"
FAILED=0

pass() { echo "[PASS] $1"; }
fail() { echo "[FAIL] $1" >&2; FAILED=1; }

cleanup() { docker volume rm -f "$VOLUME_NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "Building base image ${BASE_IMAGE_TAG}..."
docker build -t "$BASE_IMAGE_TAG" .
echo "Building reference deployment image ${IMAGE_TAG}..."
docker build --target deployment -t "$IMAGE_TAG" .

echo "--- base image: uid and HOME ---"
ID_OUTPUT="$(docker run --rm "$BASE_IMAGE_TAG" id)"
if [ "$ID_OUTPUT" = "uid=1000(agent) gid=1000(agent) groups=1000(agent)" ]; then
  pass "base runs as non-root agent, uid 1000"
else
  fail "base: expected uid 1000(agent), got: $ID_OUTPUT"
fi
HOME_OUTPUT="$(docker run --rm "$BASE_IMAGE_TAG" bash -c 'echo $HOME')"
if [ "$HOME_OUTPUT" = "/home/agent" ]; then
  pass "base HOME is /home/agent"
else
  fail "base: expected HOME /home/agent, got: $HOME_OUTPUT"
fi

echo "--- base image: agent CLIs, installer, and uv downloads ---"
if docker run --rm --read-only --tmpfs /tmp --user 1000:1000 \
  -e HOME=/home/agent \
  -v "$VOLUME_NAME:/var/lib/coding-agent" \
  "$BASE_IMAGE_TAG" bash -c '
    set -e
    agent --help >/dev/null
    agentctl --help >/dev/null
    agent-installer --version >/dev/null
    touch /tmp/probe
    ! touch /opt/probe 2>/dev/null
  ' >/tmp/verify-base-clis.log 2>&1; then
  pass "base: agent/agentctl/agent-installer run as agent under a read-only rootfs with /tmp writable"
else
  fail "base CLI/read-only-rootfs check failed: $(cat /tmp/verify-base-clis.log)"
fi
rm -f /tmp/verify-base-clis.log

if docker run --network none --rm "$BASE_IMAGE_TAG" uv python install 3.12 >/tmp/verify-l2-20-base.log 2>&1; then
  fail "base: uv python install succeeded — it must refuse, not fetch"
elif grep -q 'Python downloads are not allowed' /tmp/verify-l2-20-base.log; then
  pass "base: uv refuses an out-of-matrix interpreter instead of downloading it"
else
  fail "base: uv failed for an unexpected reason: $(cat /tmp/verify-l2-20-base.log)"
fi
rm -f /tmp/verify-l2-20-base.log

if docker run --network none --rm "$IMAGE_TAG" uv python install 3.12 >/tmp/verify-l2-20.log 2>&1; then
  fail "deployment: uv python install succeeded — it must refuse, not fetch"
elif grep -q 'Python downloads are not allowed' /tmp/verify-l2-20.log; then
  pass "deployment: uv refuses an out-of-matrix interpreter instead of downloading it"
else
  fail "deployment: uv failed for an unexpected reason: $(cat /tmp/verify-l2-20.log)"
fi
rm -f /tmp/verify-l2-20.log

echo "--- base image: no deployment artifacts ---"
if docker run --rm "$BASE_IMAGE_TAG" bash -c '
    set -e
    ! command -v php >/dev/null
    ! command -v composer >/dev/null
    ! command -v pnpm >/dev/null
    test ! -e /opt/coding-agent/toolchain-matrix.json
    test ! -e /opt/coding-agent/skill-bundle/install-report.json
    test ! -e /opt/coding-agent/skill-bundle/list-report.json
    test ! -e /home/agent/.agents/skills
  ' >/tmp/verify-base-absent.log 2>&1; then
  pass "base carries no PHP/Composer/pnpm, Skill Bundle, or Toolchain Matrix"
else
  fail "base image leaks a deployment artifact: $(cat /tmp/verify-base-absent.log)"
fi
rm -f /tmp/verify-base-absent.log

echo "--- base image: the default command needs no Workflow Configuration ---"
if docker run --rm "$BASE_IMAGE_TAG" >/tmp/verify-base-cmd.log 2>&1; then
  if grep -qi 'usage\|commands' /tmp/verify-base-cmd.log; then
    pass "default command prints CLI help and exits 0 with no config mounted"
  else
    fail "default command exited 0 but printed no CLI help: $(cat /tmp/verify-base-cmd.log)"
  fi
else
  fail "default command failed with no config mounted: $(cat /tmp/verify-base-cmd.log)"
fi
rm -f /tmp/verify-base-cmd.log

echo "--- base image: agent run refuses at startup, naming the missing Matrix ---"
docker volume create "$VOLUME_NAME" >/dev/null
if docker run --rm --read-only --tmpfs /tmp --user 1000:1000 \
  -e HOME=/home/agent \
  -v "$VOLUME_NAME:/var/lib/coding-agent" \
  -v "$(pwd)/examples:/etc/coding-agent:ro" \
  "$BASE_IMAGE_TAG" agent run --config /etc/coding-agent/workflows.yaml \
    --workflow implement >/tmp/verify-base-refusal.log 2>&1; then
  fail "base: agent run succeeded without a Toolchain Matrix — it must refuse"
elif grep -qi 'toolchain-matrix\|Supported Toolchain Matrix' /tmp/verify-base-refusal.log; then
  pass "base: agent run refuses at startup, naming the missing Matrix"
else
  fail "base: agent run refused without naming the Matrix: $(cat /tmp/verify-base-refusal.log)"
fi
rm -f /tmp/verify-base-refusal.log

echo "--- deployment image: Supported Toolchain Matrix ---"
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
" && pass "deployment matrix publishes python 3.13 / php 8.4 / node 22 with their package managers" \
  || fail "deployment matrix missing, malformed, or off the pinned versions"

echo "--- deployment image: read-only root filesystem, volume, and /tmp ---"
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
  pass "deployment: pnpm, composer and uv bootstrap real projects under a read-only root filesystem"
else
  fail "deployment toolchain bootstrap under read-only rootfs failed: $(cat /tmp/verify-rootfs.log)"
fi
rm -f /tmp/verify-rootfs.log

echo "--- deployment image: Skill Bundle, the three verification layers pass reproducibly ---"
# Re-run the same check against the finished image rather than grepping the
# build log: a cached build step prints nothing (Docker only replays output
# for steps it actually executes), so the log alone can't be trusted here.
if docker run --rm "$IMAGE_TAG" agent verify-skill-bundle \
  --install-report /opt/coding-agent/skill-bundle/install-report.json \
  --list-report /opt/coding-agent/skill-bundle/list-report.json \
  --home /home/agent \
  --ignore-file /opt/coding-agent/skill-bundle-ignore.txt >/tmp/verify-skill-bundle.log 2>&1; then
  pass "deployment: install-report, list-report and closure checks all pass against the built image"
else
  fail "deployment: verify-skill-bundle failed against the built image: $(cat /tmp/verify-skill-bundle.log)"
fi
rm -f /tmp/verify-skill-bundle.log

echo "--- deployment image: Skill Bundle, exactly the four bundled skills are exposed ---"
EXPOSED="$(docker run --rm "$IMAGE_TAG" bash -c 'ls /home/agent/.claude/skills' | sort | tr '\n' ' ')"
if [ "$EXPOSED" = "code-review codebase-design implement tdd " ]; then
  pass "deployment: exposed skills are exactly implement, tdd, code-review, codebase-design"
else
  fail "deployment: unexpected exposed skill set: $EXPOSED"
fi

echo "--- deployment image: Skill Bundle, L2-19, an induced bundle-shrinkage failure ---"
INDUCED_DOCKERFILE="$(mktemp)"
sed '/--only skill:codebase-design \\/d' Dockerfile > "$INDUCED_DOCKERFILE"
INDUCED_LOG="$(docker build --target deployment -f "$INDUCED_DOCKERFILE" -t "${IMAGE_TAG}-induced" . 2>&1 || true)"
rm -f "$INDUCED_DOCKERFILE"
docker rmi "${IMAGE_TAG}-induced" >/dev/null 2>&1 || true
if echo "$INDUCED_LOG" | grep -q 'missing from installed/updated: skill:codebase-design' \
  && echo "$INDUCED_LOG" | grep -q 'did not complete successfully'; then
  pass "deployment: dropping a Skill Bundle member fails the build, naming what was wrong"
else
  fail "deployment: induced bundle-shrinkage build did not fail the way L2-19 requires"
fi

if [ "$FAILED" -eq 0 ]; then
  echo "verify-image: all checks passed"
else
  echo "verify-image: FAILED" >&2
fi
exit "$FAILED"
