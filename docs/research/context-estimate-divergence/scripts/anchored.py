"""Error of an anchored measure: previous reported input_tokens + bytes/4 of what was added since.

For consecutive requests n-1, n of one conversation:
  anchored_n = input_tokens(n-1) + (estimate(n) - estimate(n-1))
The delta covers the previous response and the tool results/runtime messages after it.
Usage: python anchored.py CSV [CSV ...] (CSV from analyse.py)"""
import csv, statistics as st, sys
for path in sys.argv[1:]:
    rows = list(csv.DictReader(open(path))); prev = {}; rel = []; ab = []
    for r in rows:
        k = (r["source"], r["conversation"])
        if k in prev:
            p = prev[k]
            anchored = float(p["input_tokens"]) + float(r["estimate"]) - float(p["estimate"])
            err = anchored - float(r["input_tokens"])
            rel.append(err / float(r["input_tokens"])); ab.append(err)
        prev[k] = r
    q = sorted(rel); a = sorted(ab, key=abs)
    print(f"{path}: n={len(q)} relative error median={st.median(q):+.2%} p5={q[int(.05*len(q))]:+.2%} p95={q[int(.95*len(q))-1]:+.2%} min={q[0]:+.2%} max={q[-1]:+.2%}; abs tokens worst={a[-1]:+.0f} median|err|={st.median(abs(x) for x in ab):.0f}")
