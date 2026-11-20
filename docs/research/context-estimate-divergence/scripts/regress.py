"""Attribute provider input tokens to request content types by least squares.

input_tokens ~ intercept + sum_k tokens_per_byte_k * bytes_k, fitted with
non-negative coefficients; the intercept absorbs what the estimate never sees
(provider framing, tool names/descriptions, hidden tool-use prompts) plus the
constant tool-schema bytes. Also reports per-turn incremental ratios.
Usage: python regress.py CSV [CSV ...]
"""
import csv, sys
import numpy as np
from scipy.optimize import nnls
FEATS = ["prefix", "runtime_msg", "ai_text", "ai_reasoning", "ai_calls", "tool_code", "tool_search", "tool_exec", "tool_other", "tool_schemas"]
for path in sys.argv[1:]:
    rows = list(csv.DictReader(open(path)))
    y = np.array([float(r["input_tokens"]) for r in rows])
    feats = [f for f in FEATS if np.ptp([float(r[f]) for r in rows]) > 0]
    X = np.array([[float(r[f]) for f in feats] + [1.0] for r in rows])
    coef, _ = nnls(X, y)
    pred = X @ coef
    rel = np.abs(pred - y) / y
    print(f"\n== {path}: n={len(y)}  fit rel. error median={np.median(rel):.3%} max={rel.max():.3%}")
    print(f"  intercept tokens: {coef[-1]:.0f}")
    tot = {f: sum(float(r[f]) for r in rows) for f in FEATS}
    allb = sum(tot.values())
    for f, c in zip(feats, coef):
        tpb = c
        print(f"  {f:13} tokens/byte={tpb:.4f}  bytes/token={(1/tpb if tpb else float('inf')):6.2f}  share of bytes={tot[f]/allb:6.1%}")
    # last request of each conversation: makeup share
    last = {}
    for r in rows: last[(r["source"], r["conversation"])] = r
    print("  makeup of the largest request per conversation (byte share):")
    for k, r in last.items():
        b = {f: float(r[f]) for f in FEATS}; t = sum(b.values())
        top = ", ".join(f"{f}={v/t:.0%}" for f, v in sorted(b.items(), key=lambda kv: -kv[1]) if v / t >= 0.03)
        print(f"   {'/'.join(k):28} in={float(r['input_tokens']):>8.0f} est={float(r['estimate']):>8.0f} ratio={float(r['ratio']):.3f}  {top}")
    # incremental: delta estimate / delta input between consecutive requests of a conversation
    inc = []
    prev = {}
    for r in rows:
        k = (r["source"], r["conversation"])
        if k in prev:
            de = float(r["estimate"]) - float(prev[k]["estimate"]); di = float(r["input_tokens"]) - float(prev[k]["input_tokens"])
            if di > 500: inc.append(de / di)
        prev[k] = r
    inc = np.array(inc)
    if len(inc):
        print(f"  per-turn delta ratio (turns adding >500 tokens): n={len(inc)} p10={np.percentile(inc,10):.3f} median={np.median(inc):.3f} p90={np.percentile(inc,90):.3f} min={inc.min():.3f} max={inc.max():.3f}")
