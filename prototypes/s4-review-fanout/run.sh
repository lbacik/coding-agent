#!/usr/bin/env bash
# THROWAWAY PROTOTYPE — see README.md.
#
# One command, no thinking required: fetches everything the harness needs into
# a scratch directory and runs all four conversations.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${WORK:-$HERE/.scratch}"
SKILL_REF="3cca18b368ae95cdbdebbff572ccafa662551015"
SANDBOX="https://github.com/lbacik/coding-agent-sandbox.git"

# The key may live in the repository's .env rather than the environment.
if [[ -z "${ANTHROPIC_API_KEY:-}" && -f "$HERE/../../.env" ]]; then
  set -a; source "$HERE/../../.env"; set +a
fi
: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY (or put it in the repo's .env)}"

mkdir -p "$WORK"

if [[ ! -d "$WORK/sandbox/.git" ]]; then
  git clone --quiet "$SANDBOX" "$WORK/sandbox"
fi
git -C "$WORK/sandbox" fetch --quiet origin \
  'refs/heads/prototype/*:refs/remotes/origin/prototype/*'
git -C "$WORK/sandbox" checkout --quiet -B candidate origin/prototype/s4-review-candidate
git -C "$WORK/sandbox" branch --quiet -f base origin/prototype/s4-review-base

if [[ ! -f "$WORK/code-review-SKILL.md" ]]; then
  gh api "repos/mattpocock/skills/contents/skills/engineering/code-review/SKILL.md?ref=$SKILL_REF" \
    --jq '.content' | base64 -d > "$WORK/code-review-SKILL.md"
fi

if [[ ! -f "$WORK/spec.md" ]]; then
  gh issue view 5 --repo lbacik/coding-agent-sandbox --json title,body \
    --jq '"# " + .title + "\n\n" + .body' > "$WORK/spec.md"
fi

cd "$HERE"
exec uv run --quiet \
  --with langchain --with langchain-anthropic \
  python harness.py \
    --workspace "$WORK/sandbox" \
    --skill "$WORK/code-review-SKILL.md" \
    --spec "$WORK/spec.md" \
    --base base \
    --head candidate \
    "$@"
