#!/usr/bin/env bash
# Verifies issue #18's "Done when" (L2-1, L2-2) against a real build: the
# three fixtures under fixtures/l2/{python,php,typescript} bootstrap and
# validate for real, inside a container from this image, via `agent
# validate` (S2c). Exercises `bootstrap`, `test_all`, every named `check`
# and `test_targeted {path}` with the real toolchains — never a fake
# CommandRunner. Each fixture is bind-mounted read-only and copied onto the
# volume by its scripts/l2-verify/<lang>.sh script before running, exactly
# as a real checkout would sit under /var/lib/coding-agent/workspaces;
# nothing here is baked into the image itself.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

IMAGE_TAG="${IMAGE_TAG:-coding-agent:verify}"
VOLUME_NAME="coding-agent-verify-l2-$$"
FAILED=0

pass() { echo "[PASS] $1"; }
fail() { echo "[FAIL] $1" >&2; FAILED=1; }

cleanup() { docker volume rm -f "$VOLUME_NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "Building ${IMAGE_TAG}..."
docker build -t "$IMAGE_TAG" .

docker volume create "$VOLUME_NAME" >/dev/null

run_fixture() {
  local lang="$1"
  docker run --rm --read-only --tmpfs /tmp --user 1000:1000 \
    -e HOME=/home/agent \
    -v "$VOLUME_NAME:/var/lib/coding-agent" \
    -v "$(pwd)/fixtures/l2/${lang}:/opt/l2-fixture:ro" \
    -v "$(pwd)/scripts/l2-verify/${lang}.sh:/opt/verify.sh:ro" \
    "$IMAGE_TAG" bash /opt/verify.sh
}

echo "--- L2-1/L2-2: Python (uv, pytest), checks: none ---"
OUT="$(run_fixture python 2>&1)" || true
echo "$OUT"
if echo "$OUT" | grep -q '\[PASS\] bootstrap' \
  && echo "$OUT" | grep -q "test_all:.*exit=1 executed=2 failures=\['tests.test_extra::test_shout'\]" \
  && echo "$OUT" | grep -q 'test_targeted:.*exit=0 executed=1 failures=\[\]'; then
  pass "python: bootstrap ran, test_all counted 2 and named the real failure, test_targeted ran only test_greeting.py"
else
  fail "python: L2-1/L2-2 output did not match"
fi

echo "--- L2-1/L2-2: PHP (composer, PHPUnit), working_directory != root, a declared service ---"
OUT="$(run_fixture php 2>&1)" || true
echo "$OUT"
if echo "$OUT" | grep -q '\[PASS\] bootstrap' \
  && echo "$OUT" | grep -q "test_all:.*exit=1 executed=2 failures=\['Tests.ExtraTest::testShout'\]" \
  && echo "$OUT" | grep -q '\[PASS\] syntax' \
  && echo "$OUT" | grep -q 'test_targeted:.*exit=0 executed=1 failures=\[\]' \
  && echo "$OUT" | grep -q 'service-reachable: True'; then
  pass "php: bootstrap ran under working_directory=app, test_all and the syntax check ran, test_targeted ran only GreetingTest.php, the declared service was probed reachable"
else
  fail "php: L2-1/L2-2 output did not match"
fi

echo "--- L2-1/L2-2: TypeScript (pnpm, vitest), a real tsc check ---"
OUT="$(run_fixture typescript 2>&1)" || true
echo "$OUT"
if echo "$OUT" | grep -q '\[PASS\] bootstrap' \
  && echo "$OUT" | grep -q "test_all:.*exit=1 executed=2 failures=\['tests/extra.test.ts::shout'\]" \
  && echo "$OUT" | grep -q '\[PASS\] types' \
  && echo "$OUT" | grep -q 'test_targeted:.*exit=0 executed=1 failures=\[\]'; then
  pass "typescript: bootstrap ran, test_all counted 2 and named the real failure, the tsc check passed, test_targeted ran only greeting.test.ts"
else
  fail "typescript: L2-1/L2-2 output did not match"
fi

if [ "$FAILED" -eq 0 ]; then
  echo "verify-l2-fixtures: all checks passed"
else
  echo "verify-l2-fixtures: FAILED" >&2
fi
exit "$FAILED"
