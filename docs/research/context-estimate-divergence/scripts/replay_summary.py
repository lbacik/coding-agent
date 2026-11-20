"""Summarise replay CSVs: estimate/counted ratio overall, per size band, worst cases.
Usage: python replay_summary.py CSV [CSV ...]"""
import csv, statistics as st, sys
for path in sys.argv[1:]:
    rows = list(csv.DictReader(open(path)))
    for r in rows:
        for k in ("estimate", "counted", "bytes"): r[k] = float(r[k])
        r["ratio"] = r["estimate"] / r["counted"]
    def show(name, rs):
        if not rs: return
        q = sorted(x["ratio"] for x in rs)
        print(f"  {name:12} n={len(q):3} min={q[0]:.3f} median={st.median(q):.3f} max={q[-1]:.3f}  counted {min(x['counted'] for x in rs):.0f}..{max(x['counted'] for x in rs):.0f}  bytes/token median={st.median(x['bytes']/x['counted'] for x in rs):.2f}")
    print(path)
    show("ALL", rows)
    bands = [0, 1e4, 5e4, 1e5, 2e5, 4e5]
    for lo, hi in zip(bands, bands[1:]):
        show(f"{lo/1e3:.0f}K-{hi/1e3:.0f}K", [r for r in rows if lo <= r["counted"] < hi])
    for s in sorted({r["source"] for r in rows}):
        rs = [r for r in rows if r["source"] == s]; last = max(rs, key=lambda r: int(r["seq"]))
        show(s, rs); print(f"     last request: estimate={last['estimate']:.0f} counted={last['counted']:.0f} gap={last['counted']-last['estimate']:+.0f}")
    lo = min(rows, key=lambda r: r["ratio"]); hi = max(rows, key=lambda r: r["ratio"])
    big = max(rows, key=lambda r: abs(r["counted"] - r["estimate"]))
    print(f"  lowest ratio {lo['source']}#{lo['seq']} est={lo['estimate']:.0f} counted={lo['counted']:.0f}; highest {hi['source']}#{hi['seq']} est={hi['estimate']:.0f} counted={hi['counted']:.0f}; largest absolute gap {big['source']}#{big['seq']} {big['counted']-big['estimate']:+.0f}")
