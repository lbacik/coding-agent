#!/usr/bin/env bash
# Verifies the base image (the default `docker build .` output) and the
# test-only reference deployment image (`--target deployment`): the base
# carries only coding-agent, agent-installer and their own dependencies,
# while the deployment adds the pinned Target Project toolchains, the
# Supported Toolchain Matrix, and the baked v3 instruction package
# (`docker/deployment/`) with its prepared Skill Bundle records. Exercises
# the images the way a real bootstrap would rather than reading the
# Dockerfile, and never treats a builder-stage log as acceptance proof:
# every deployment check below re-runs the command against the finished
# image, including one induced build failure.
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
    test ! -e /etc/coding-agent/workflow.yml
    test ! -e /etc/coding-agent/skill-policy.json
    test ! -e /opt/coding-agent/skill-bundle/requirements.json
    test ! -e /opt/coding-agent/skill-bundle/manifest.json
    test ! -e /opt/coding-agent/skill-bundle/validation.json
    test ! -e /home/agent/.agents/skills
  ' >/tmp/verify-base-absent.log 2>&1; then
  pass "base carries no PHP/Composer/pnpm, Toolchain Matrix, baked instruction package, prepared bundle records, or Skill Bundle store"
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
    test -z "$(find /home/agent -maxdepth 1 -user root)"
    test -z "$(find /var/lib/coding-agent/caches -user root)"
    test "$(npm config get cache)" = "/var/lib/coding-agent/caches/npm"
    mkdir -p /var/lib/coding-agent/workspaces/verify/{py,php,ts,npm}
    cd /var/lib/coding-agent/workspaces/verify/ts
    echo "{\"name\":\"verify\"}" > package.json
    pnpm add left-pad >/dev/null 2>&1
    cd /var/lib/coding-agent/workspaces/verify/npm
    echo "{\"name\":\"verify-npm\",\"version\":\"1.0.0\"}" > package.json
    npm install --no-audit --no-fund left-pad >/dev/null 2>&1
    rm -rf node_modules
    npm ci --no-audit --no-fund >/dev/null 2>&1
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
  pass "deployment: pnpm, npm, composer and uv bootstrap real projects under a read-only root filesystem"
else
  fail "deployment toolchain bootstrap under read-only rootfs failed: $(cat /tmp/verify-rootfs.log)"
fi
rm -f /tmp/verify-rootfs.log

echo "--- deployment image: secret-free config validate against the final roots ---"
if docker run --rm "$IMAGE_TAG" bash -c '
    set -e
    unset GITHUB_TOKEN ANTHROPIC_API_KEY OPENAI_API_KEY SANDBOX_TOKEN
    agent config validate --config /etc/coding-agent/workflow.yml
  ' >/tmp/verify-secret-free.log 2>&1; then
  pass "deployment: config validate exits 0 with no Worker GitHub/model secrets set"
else
  fail "deployment: secret-free config validate failed: $(cat /tmp/verify-secret-free.log)"
fi
rm -f /tmp/verify-secret-free.log

echo "--- deployment image: prepared bundle records are baked in ---"
if docker run --rm "$IMAGE_TAG" bash -c '
    set -e
    test -f /etc/coding-agent/workflow.yml
    test -f /etc/coding-agent/skill-policy.json
    test -f /etc/coding-agent/prompts/implement-policy.md
    test -f /etc/coding-agent/prompts/review-policy.md
    test -f /opt/coding-agent/skill-bundle/requirements.json
    test -f /opt/coding-agent/skill-bundle/manifest.json
    test -f /opt/coding-agent/skill-bundle/validation.json
  ' >/tmp/verify-records.log 2>&1; then
  pass "deployment: baked workflow/prompts/policy and prepared bundle records all exist"
else
  fail "deployment: a baked or prepared record is missing: $(cat /tmp/verify-records.log)"
fi
rm -f /tmp/verify-records.log

echo "--- deployment image: final-root offline verification, not a builder log ---"
# Re-run the same check against the finished image rather than grepping the
# build log: a cached build step prints nothing (Docker only replays output
# for steps it actually executes), so the log alone can never be the
# acceptance proof here. `--network none` proves the check is offline.
if docker run --network none --rm "$IMAGE_TAG" agent verify-skill-bundle \
  --config /etc/coding-agent/workflow.yml \
  --policy /etc/coding-agent/skill-policy.json \
  --bundle-dir /opt/coding-agent/skill-bundle \
  --home /home/agent >/tmp/verify-skill-bundle.log 2>&1; then
  pass "deployment: verify-skill-bundle passes offline against the final roots of the built image"
else
  fail "deployment: verify-skill-bundle failed against the final roots: $(cat /tmp/verify-skill-bundle.log)"
fi
rm -f /tmp/verify-skill-bundle.log

echo "--- deployment image: Skill Bundle, exactly the four bundled skills are installed ---"
INSTALLED="$(docker run --rm "$IMAGE_TAG" bash -c 'ls /home/agent/.agents/skills' | sort | tr '\n' ' ')"
if [ "$INSTALLED" = "code-review codebase-design implement tdd " ]; then
  pass "deployment: installed skills are exactly implement, tdd, code-review, codebase-design"
else
  fail "deployment: unexpected installed skill set: $INSTALLED"
fi

echo "--- deployment image: relocated-identical-tree verifies, edited copy refuses, re-prepare heals ---"
# All offline (`verify-skill-bundle` plus `prepare-skill-bundle --mode
# installed`, which adopts the declared subset from the image's own store
# without the network `--mode install` needs), so `--network none` applies.
# The heal uses `--mode installed` deliberately: membership is unchanged, so
# the declared subset is re-adopted from the store rather than re-fetched.
if docker run --network none --rm --read-only --tmpfs /tmp --user 1000:1000 \
  -e HOME=/home/agent \
  -v "$VOLUME_NAME:/var/lib/coding-agent" \
  "$IMAGE_TAG" bash -c '
    set -euo pipefail
    rm -rf /tmp/vcheck && mkdir -p /tmp/vcheck/prompts /tmp/vcheck/bundle
    cp /etc/coding-agent/workflow.yml /tmp/vcheck/workflow.yml
    cp /etc/coding-agent/prompts/* /tmp/vcheck/prompts/
    cp /etc/coding-agent/skill-policy.json /tmp/vcheck/skill-policy.json
    cp /opt/coding-agent/skill-bundle/*.json /tmp/vcheck/bundle/
    agent verify-skill-bundle \
      --config /tmp/vcheck/workflow.yml \
      --policy /tmp/vcheck/skill-policy.json \
      --bundle-dir /tmp/vcheck/bundle \
      --home /home/agent
    echo "# drift" >> /tmp/vcheck/workflow.yml
    if agent verify-skill-bundle \
      --config /tmp/vcheck/workflow.yml \
      --policy /tmp/vcheck/skill-policy.json \
      --bundle-dir /tmp/vcheck/bundle \
      --home /home/agent; then
      echo "edited copy verified — it must refuse" >&2
      exit 1
    fi
    agent prepare-skill-bundle --mode installed \
      --config /tmp/vcheck/workflow.yml \
      --policy /tmp/vcheck/skill-policy.json \
      --bundle-dir /tmp/vcheck/bundle \
      --home /home/agent
    agent verify-skill-bundle \
      --config /tmp/vcheck/workflow.yml \
      --policy /tmp/vcheck/skill-policy.json \
      --bundle-dir /tmp/vcheck/bundle \
      --home /home/agent
  ' >/tmp/verify-copy-mismatch.log 2>&1; then
  pass "deployment: relocated-identical-tree verifies, byte-edited copy refuses until re-prepared"
else
  fail "deployment: copy-mismatch round-trip failed: $(cat /tmp/verify-copy-mismatch.log)"
fi
rm -f /tmp/verify-copy-mismatch.log

echo "--- deployment image: external prepared package verifies at non-baked roots ---"
# The Forge/external half of SKV3-33: an operator-prepared package lives at
# operator-chosen absolute roots, not at the baked defaults. Prepare it from
# the image's own store (`--mode installed`, offline), verify it against its
# final roots after the last copy, and show the rendered `run` invocation
# points at the explicit paths. This proves root-independence; the
# second-source (content-independence) half of SKV3-36 is proved by
# `tests/test_skill_bundle_real_installer.py` through `insteadOf` mappings.
# The `run` itself is not executed here: it needs the target token and the
# selected Workflow's model key plus live GitHub and provider access, which
# this script never fabricates (SKV3-20).
if docker run --network none --rm --read-only --tmpfs /tmp --user 1000:1000 \
  -e HOME=/home/agent \
  -v "$VOLUME_NAME:/var/lib/coding-agent" \
  "$IMAGE_TAG" bash -c '
    set -euo pipefail
    rm -rf /tmp/extpkg /tmp/extfinal
    mkdir -p /tmp/extpkg/prompts /tmp/extpkg/bundle /tmp/extfinal
    cp /etc/coding-agent/workflow.yml /tmp/extpkg/workflow.yml
    cp /etc/coding-agent/prompts/* /tmp/extpkg/prompts/
    cp /etc/coding-agent/skill-policy.json /tmp/extpkg/skill-policy.json
    agent config validate --config /tmp/extpkg/workflow.yml
    agent prepare-skill-bundle --mode installed \
      --config /tmp/extpkg/workflow.yml \
      --policy /tmp/extpkg/skill-policy.json \
      --bundle-dir /tmp/extpkg/bundle \
      --home /home/agent
    agent verify-skill-bundle \
      --config /tmp/extpkg/workflow.yml \
      --policy /tmp/extpkg/skill-policy.json \
      --bundle-dir /tmp/extpkg/bundle \
      --home /home/agent
    cp /tmp/extpkg/workflow.yml /tmp/extfinal/workflow.yml
    cp -r /tmp/extpkg/prompts /tmp/extfinal/prompts
    cp /tmp/extpkg/skill-policy.json /tmp/extfinal/skill-policy.json
    cp -r /tmp/extpkg/bundle /tmp/extfinal/bundle
    agent verify-skill-bundle \
      --config /tmp/extfinal/workflow.yml \
      --policy /tmp/extfinal/skill-policy.json \
      --bundle-dir /tmp/extfinal/bundle \
      --home /home/agent
    echo "rendered run invocation (not executed: needs live secrets):"
    echo "agent run --config /tmp/extfinal/workflow.yml --workflow implement --policy /tmp/extfinal/skill-policy.json --bundle-dir /tmp/extfinal/bundle --skill-home /home/agent"
  ' >/tmp/verify-external.log 2>&1; then
  pass "deployment: external prepared package verifies at non-baked roots after the final copy"
else
  fail "deployment: external-package proof failed: $(cat /tmp/verify-external.log)"
fi
rm -f /tmp/verify-external.log

echo "--- deployment image: a missing declared member fails the build ---"
INDUCED_CTX="$(mktemp -d)"
sed 's/include: \[implement, tdd, code-review, codebase-design\]/include: [implement, tdd, code-review, codebase-design, ghost-skill]/' \
  docker/deployment/workflow.yml > "$INDUCED_CTX/workflow.yml"
cp -r docker/deployment/prompts "$INDUCED_CTX/prompts"
cp docker/deployment/skill-policy.json "$INDUCED_CTX/skill-policy.json"
cat > "$INDUCED_CTX/Dockerfile" <<EOF
FROM $BASE_IMAGE_TAG
USER root
COPY workflow.yml /etc/coding-agent/workflow.yml
COPY prompts /etc/coding-agent/prompts
COPY skill-policy.json /etc/coding-agent/skill-policy.json
RUN install -d -o agent -g agent /opt/coding-agent/skill-bundle
USER agent
# The induced build mirrors the deployment stage's own validate/prepare/verify
# sequence (minus the Target Project toolchains, which a missing member never
# reaches): it must fail at preparation, naming the ghost member.
RUN agent config validate --config /etc/coding-agent/workflow.yml \\
    && agent prepare-skill-bundle --mode install \\
      --config /etc/coding-agent/workflow.yml \\
      --policy /etc/coding-agent/skill-policy.json \\
      --bundle-dir /opt/coding-agent/skill-bundle \\
      --home /home/agent \\
    && agent verify-skill-bundle \\
      --config /etc/coding-agent/workflow.yml \\
      --policy /etc/coding-agent/skill-policy.json \\
      --bundle-dir /opt/coding-agent/skill-bundle \\
      --home /home/agent
EOF
if docker build "$INDUCED_CTX" -t "${IMAGE_TAG}-induced" >/tmp/verify-induced.log 2>&1; then
  fail "deployment: induced missing-member build succeeded — it must fail"
elif grep -q 'ghost-skill' /tmp/verify-induced.log; then
  pass "deployment: declaring an uninstallable member fails the build, naming what was wrong"
else
  fail "deployment: induced missing-member build failed without naming ghost-skill: $(cat /tmp/verify-induced.log)"
fi
rm -rf "$INDUCED_CTX"
rm -f /tmp/verify-induced.log
docker rmi "${IMAGE_TAG}-induced" >/dev/null 2>&1 || true

if [ "$FAILED" -eq 0 ]; then
  echo "verify-image: all checks passed"
else
  echo "verify-image: FAILED" >&2
fi
exit "$FAILED"
