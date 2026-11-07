#!/usr/bin/env bash
# Producer conformance suite (issue #260): the operator-contract checks
# that only run against the built image — real process death, `docker
# exec` / `docker run` invocation, the read-only root filesystem, the
# discovery labels and the socket permissions — plus the schema and
# drift gates that also run in-process under pytest.
#
# Usage: ./conformance/producer-suite.sh [--image TAG]
# Skips Docker sections gracefully where no daemon is reachable (this
# repository's own agent container has none); CI runs them all.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

IMAGE_TAG="coding-agent:verify"
while [ $# -gt 0 ]; do
  case "$1" in
    --image=*) IMAGE_TAG="${1#--image=}" ;;
    --image) IMAGE_TAG="${2:?--image needs a value}"; shift ;;
    *) IMAGE_TAG="$1" ;;
  esac
  shift
done

FAILED=0
pass() { echo "[PASS] $1"; }
fail() { echo "[FAIL] $1" >&2; FAILED=1; }
skip() { echo "[SKIP] $1"; }

have_docker() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }

echo "=== schema gate (no Docker needed) ==="
if uv run --locked pytest -q tests/test_conformance_schemas.py tests/test_conformance_harness.py; then
  pass "in-repo conformance suites are green"
else
  fail "in-repo conformance suites failed"
fi

echo "=== drift gate: a deliberate deviation must fail ==="
if ./conformance/check-drift.sh >/tmp/producer-drift.log 2>&1; then
  pass "deliberate contract deviation fails as required"
else
  fail "drift gate did not fail on deviation: $(cat /tmp/producer-drift.log)"
fi
rm -f /tmp/producer-drift.log

if ! have_docker; then
  skip "no Docker daemon: image checks need CI or a human with Docker"
  if [ "$FAILED" -ne 0 ]; then exit 1; fi
  exit 0
fi

echo "=== building ${IMAGE_TAG} ==="
docker build -t "$IMAGE_TAG" .

VOLUME_NAME="conformance-producer-$$"
cleanup() { docker volume rm -f "$VOLUME_NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker volume create "$VOLUME_NAME" >/dev/null

echo "--- docker exec --user agent reaches agentctl ---"
if docker run -d --name "conformance-worker-$$" --read-only --tmpfs /tmp \
    -v "$VOLUME_NAME:/var/lib/coding-agent" "$IMAGE_TAG" sleep 300 >/dev/null; then
  CONTAINER="conformance-worker-$$"
  cleanup_container() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
  trap 'cleanup_container; cleanup' EXIT
  if docker exec --user agent "$CONTAINER" agentctl --contract 1 status >/tmp/producer-exec.log 2>&1; then
    # A bare container has no Worker listening: agent_unreachable (exit 6)
    # proves exec, the user and the binary all resolved past the envelope.
    pass "docker exec --user agent reaches agentctl"
  else
    code=$?
    if [ "$code" -eq 6 ]; then
      pass "docker exec --user agent reaches agentctl (agent_unreachable, no listener)"
    else
      fail "docker exec agentctl failed: $(cat /tmp/producer-exec.log)"
    fi
  fi
  rm -f /tmp/producer-exec.log

  echo "--- real process death: SIGKILL then SIGTERM ---"
  if docker kill -s KILL "$CONTAINER" >/dev/null 2>&1; then
    pass "docker kill -s KILL lands on the container"
  else
    fail "docker kill -s KILL failed"
  fi
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  if docker run -d --name "$CONTAINER" --read-only --tmpfs /tmp \
      -v "$VOLUME_NAME:/var/lib/coding-agent" "$IMAGE_TAG" sleep 300 >/dev/null \
    && docker stop "$CONTAINER" >/dev/null 2>&1; then
    pass "docker stop lands on the container"
  else
    fail "docker stop failed"
  fi
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  trap cleanup EXIT
else
  fail "could not start a throwaway container from ${IMAGE_TAG}"
fi

echo "--- docker run --read-only invocation ---"
if docker run --rm --read-only --tmpfs /tmp --user 1000:1000 \
    -v "$VOLUME_NAME:/var/lib/coding-agent" \
    "$IMAGE_TAG" agentctl --contract 1 status >/tmp/producer-run.log 2>&1; then
  fail "bare agentctl status unexpectedly succeeded: $(cat /tmp/producer-run.log)"
else
  code=$?
  if [ "$code" -eq 6 ]; then
    pass "docker run --read-only reaches agentctl (agent_unreachable, no listener)"
  else
    fail "docker run agentctl exited $code, want 6: $(cat /tmp/producer-run.log)"
  fi
fi
rm -f /tmp/producer-run.log

echo "--- labels: deployment only, never the image ---"
if docker image inspect "$IMAGE_TAG" | grep -q "io.github.lbacik.agent-operator"; then
  fail "image carries discovery labels; they belong on the compose worker service"
else
  pass "image carries no discovery labels"
fi

echo "--- read-only root filesystem, /tmp writable ---"
if docker run --rm --read-only --tmpfs /tmp --user 1000:1000 \
    -v "$VOLUME_NAME:/var/lib/coding-agent" \
    "$IMAGE_TAG" bash -c 'touch /tmp/probe && ! touch /opt/probe 2>/dev/null' >/dev/null 2>&1; then
  pass "root filesystem is read-only with /tmp writable"
else
  fail "read-only root filesystem check failed"
fi

echo "--- socket permissions are covered in-process ---"
if uv run --locked pytest -q -k "socket_permissions or discovery_labels" >/dev/null 2>&1; then
  pass "socket 0600 / dir 0700 and discovery-label assertions are green"
else
  fail "socket/label assertions failed"
fi

if [ "$FAILED" -ne 0 ]; then exit 1; fi
echo "producer suite: all green"
