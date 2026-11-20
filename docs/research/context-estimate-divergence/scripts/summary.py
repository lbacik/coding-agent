"""Summarise a CSV from analyse.py: ratio spread overall, per source, per context size band."""
import csv, statistics as st, sys
rows = [r for r in csv.DictReader(open(sys.argv[1]))]
for r in rows:
    for k in r:
        if k not in ("source", "conversation"):
            r[k] = float(r[k])
def show(name, rs):
    q = sorted(x["ratio"] for x in rs)
    if not q: return
    pct = lambda p: q[min(len(q) - 1, int(p * len(q)))]
    print(f"{name:28} n={len(q):4} min={q[0]:.3f} p10={pct(.1):.3f} median={st.median(q):.3f} p90={pct(.9):.3f} max={q[-1]:.3f}  in_tokens {min(x['input_tokens'] for x in rs):.0f}..{max(x['input_tokens'] for x in rs):.0f}")
show("ALL", rows)
for s in sorted({r["source"] for r in rows}):
    show("source " + s, [r for r in rows if r["source"] == s])
for c in sorted({(r["source"], r["conversation"]) for r in rows}):
    rs = [r for r in rows if (r["source"], r["conversation"]) == c]
    if len(rs) > 3: show("conv " + "/".join(c), rs)
bands = [0, 5e3, 1e4, 2e4, 5e4, 1e5, 2e5, 4e5]
for lo, hi in zip(bands, bands[1:]):
    show(f"band {lo/1e3:.0f}K-{hi/1e3:.0f}K", [r for r in rows if lo <= r["input_tokens"] < hi])
