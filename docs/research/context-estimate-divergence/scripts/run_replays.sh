#!/bin/bash
# Run the neutral replays of the gateway transcripts through both providers, in parallel.
set -euo pipefail
S="$1"; R="$S/data/rpi5/lbacik/battles"
T=(g106-1=$R/106/1/transcript.jsonl g106-2=$R/106/2/transcript.jsonl g111-1=$R/111/1/transcript.jsonl g112-1=$R/implement/112/1/transcript.jsonl g113-1=$R/implement/113/1/transcript.jsonl)
uv run python "$S/scripts/replay.py" neutral openai 5 "$S/data/replay-openai.csv" "$S/data/tools.json" "${T[@]}" > "$S/data/replay-openai.log" 2>&1 &
uv run python "$S/scripts/replay.py" neutral anthropic 5 "$S/data/replay-anthropic.csv" "$S/data/tools.json" "${T[@]}" > "$S/data/replay-anthropic.log" 2>&1 &
wait
echo "openai: $(grep -c '^{' "$S/data/replay-openai.log") rows, anthropic: $(grep -c '^{' "$S/data/replay-anthropic.log") rows"
grep -h -m3 -E "Traceback|Error" "$S"/data/replay-*.log || true
