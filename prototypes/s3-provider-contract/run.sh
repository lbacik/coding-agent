#!/usr/bin/env bash
# THROWAWAY PROTOTYPE — see README.md.
#
# One command. Loads the keys from the repository .env if they are not already
# in the environment, then runs every probe against both pins.
#
#   ./run.sh                                    everything
#   ./run.sh --only tool_call                   one probe
#   ./run.sh --provider openai                  one pin
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "${ANTHROPIC_API_KEY:-}" || -z "${OPENAI_API_KEY:-}" ]] && [[ -f "$HERE/../../.env" ]]; then
  set -a; source "$HERE/../../.env"; set +a
fi
: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY, or put it in the repository .env}"
: "${OPENAI_API_KEY:?set OPENAI_API_KEY, or put it in the repository .env}"

cd "$HERE"
exec uv run --quiet \
  --with langchain --with langchain-anthropic --with langchain-openai --with pydantic \
  python "${SCRIPT:-probe.py}" "$@"
