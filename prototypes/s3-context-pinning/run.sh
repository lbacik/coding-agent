#!/usr/bin/env bash
# THROWAWAY PROTOTYPE — see README.md.
#
#   ./run.sh                                  both arms, both pins
#   ./run.sh --arms KEEP --providers openai   one cell
#   SCRIPT=context.py ./run.sh                the rules, no model calls, free
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "${ANTHROPIC_API_KEY:-}" || -z "${OPENAI_API_KEY:-}" ]] && [[ -f "$HERE/../../.env" ]]; then
  set -a; source "$HERE/../../.env"; set +a
fi

cd "$HERE"
ARGS=("$@")
if [[ "${SCRIPT:-harness.py}" == "harness.py" ]]; then
  : "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY, or put it in the repository .env}"
  : "${OPENAI_API_KEY:?set OPENAI_API_KEY, or put it in the repository .env}"
  ARGS=(--bundle "${BUNDLE:-$HOME/.agents/skills}" --scratch "${SCRATCH:-/tmp/s3-context-pinning}" "$@")
fi

exec uv run --quiet \
  --with langchain --with langchain-anthropic --with langchain-openai --with pydantic \
  python "${SCRIPT:-harness.py}" "${ARGS[@]}"
