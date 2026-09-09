#!/usr/bin/env bash
# THROWAWAY PROTOTYPE — see README.md.
#
# One command, no thinking required: fetches the pinned skills and the sandbox
# candidate into a scratch directory and runs all three arms.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${WORK:-$HERE/.scratch}"
SKILL_REF="3cca18b368ae95cdbdebbff572ccafa662551015"
SANDBOX="https://github.com/lbacik/coding-agent-sandbox.git"
UPSTREAM="repos/mattpocock/skills/contents/skills/engineering"

# The key may live in the repository's .env rather than the environment.
if [[ -z "${ANTHROPIC_API_KEY:-}" && -f "$HERE/../../.env" ]]; then
  set -a; source "$HERE/../../.env"; set +a
fi
: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY, or put it in the repository .env}"

mkdir -p "$WORK"

# -- the Skill Bundle, verbatim at the pin (ADR 0007) -----------------------
fetch() {  # fetch <skill>/<file>
  local dest="$WORK/bundle/$1"
  [[ -f "$dest" ]] && return 0
  mkdir -p "$(dirname "$dest")"
  gh api "$UPSTREAM/$1?ref=$SKILL_REF" --jq '.content' | base64 -d > "$dest"
}
fetch implement/SKILL.md
fetch tdd/SKILL.md
fetch tdd/tests.md
fetch tdd/mocking.md
fetch codebase-design/SKILL.md
fetch codebase-design/DEEPENING.md
fetch codebase-design/DESIGN-IT-TWICE.md
fetch code-review/SKILL.md

# -- the target repository at the base revision -----------------------------
if [[ ! -d "$WORK/sandbox/.git" ]]; then
  git clone --quiet "$SANDBOX" "$WORK/sandbox"
fi
git -C "$WORK/sandbox" fetch --quiet origin \
  'refs/heads/prototype/*:refs/remotes/origin/prototype/*'
git -C "$WORK/sandbox" branch --quiet -f base origin/prototype/s4-review-base

# -- the ticket the implementer is given ------------------------------------
if [[ ! -f "$WORK/spec.md" ]]; then
  gh issue view 5 --repo lbacik/coding-agent-sandbox --json title,body \
    --jq '"# " + .title + "\n\n" + .body' > "$WORK/spec.md"
fi

cd "$HERE"
exec uv run --quiet \
  --with langchain --with langchain-anthropic \
  python harness.py \
    --source "$WORK/sandbox" \
    --bundle "$WORK/bundle" \
    --spec "$WORK/spec.md" \
    --scratch "$WORK" \
    "$@"
