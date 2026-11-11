"""PROTOTYPE (ticket #284): throwaway static validator for the candidate Workflow YAML.

Not production code. It exists only to check that the candidate syntax lets every
accepted validation rule (#280-#283, #286) be decided statically, without
expressions, and to print what the runtime would compile.

Usage:
    python validate_prototype.py workflows.yaml            # validate, print errors
    python validate_prototype.py workflows.yaml --graph    # also print the compiled graph
    python validate_prototype.py --invalid                 # run every invalid/*.yaml case
    python validate_prototype.py workflows.yaml --json     # dump parsed config (for the demo)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- catalog

RESERVED_RESULTS = ("error", "limit-exceeded")
YAML_ENDS = {"completed": False, "blocked": False, "unfinished": True, "failed": True}  # value: reserved may route directly
RUNTIME_ONLY_OUTCOMES = ("canceled", "withdrawn", "drift", "interrupted", "internal-failure")
ALL_OUTCOMES = tuple(YAML_ENDS) + RUNTIME_ONLY_OUTCOMES
TOOL_GROUPS = ("workspace-read", "workspace-write", "shell", "tests", "subagents")
ALWAYS_PRESENT_TOOLS = ("report", "report-result", "activate-skill", "read-result")
METERS = ("cost-usd", "tokens", "tool-calls", "wall-clock-minutes")
FIELD_TYPES = ("document", "string", "integer", "boolean", "evidence-ref")
SKILL_BUNDLE = ("implement", "code-review")  # stand-in for the pinned Skill Bundle
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
REQUIRABLE = {("run-validation", "passed")}  # revision-bound evidence a publication may require

# operation -> (business results, allowed `with` keys, input type restriction, publishes)
OPERATIONS = {
    "check-readiness": (("ready", "not-ready"), set(), None, False),
    "run-validation": (("passed", "failed"), set(), None, False),
    "commit-and-push": (("pushed", "no-changes"), {"requires"}, None, True),
    "open-pull-request": (("opened", "already-open"), {"body", "draft", "requires"}, "issue", True),
    "publish-review": (("published", "stale-head"), {"body", "requires"}, "pull-request", True),
    "post-comment": (("posted",), {"body", "requires"}, None, True),
}

UNSUPPORTED_STEP_KEYS = {
    "model": "a per-step `model` is reserved for a later release; the Attempt has one Pinned Model",
    "when": "conditions/expressions are not supported; declare distinct Step Results instead",
    "if": "conditions/expressions are not supported; declare distinct Step Results instead",
    "condition": "conditions/expressions are not supported; declare distinct Step Results instead",
    "parallel": "fan-out/fan-in is deferred to a later release",
    "join": "fan-out/fan-in is deferred to a later release",
    "workflow": "nested Workflows are deferred to a later release",
    "retry": "there is no retry construct; route the result back to the step (bounded by max-executions)",
    "on-limit": "limit handlers are Routes: route `limit-exceeded`",
}


class Problems(list):
    def add(self, where: str, msg: str) -> None:
        self.append(f"{where}: {msg}")


# --------------------------------------------------------------------------- strict loading


class StrictLoader(yaml.SafeLoader):
    pass


def _no_alias(self, *a, **k):  # pragma: no cover - prototype
    raise yaml.constructor.ConstructorError(None, None, "anchors/aliases are not allowed")


def _construct_mapping(loader, node, deep=False):
    seen = {}
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise yaml.constructor.ConstructorError(None, None, "merge keys (<<) are not allowed", key_node.start_mark)
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r} (first at line {seen[key] + 1})", key_node.start_mark
            )
        seen[key] = key_node.start_mark.line
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def strict_load(text: str):
    for event in yaml.parse(text, Loader=StrictLoader):
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
            raise yaml.YAMLError("anchors/aliases are not allowed")
        tag = getattr(event, "tag", None)
        if tag and getattr(event, "implicit", (True,)) in (False, (False, False)) and tag.startswith("!"):
            raise yaml.YAMLError(f"custom tag {tag} is not allowed")
    return yaml.load(text, Loader=StrictLoader)


# --------------------------------------------------------------------------- helpers


def keys_ok(p: Problems, where: str, obj, allowed: set[str], required: set[str] = frozenset()) -> bool:
    if not isinstance(obj, dict):
        p.add(where, f"expected a mapping, got {type(obj).__name__}")
        return False
    for k in obj:
        if k not in allowed:
            hint = UNSUPPORTED_STEP_KEYS.get(k)
            p.add(where, f"unsupported field {k!r}" + (f" ({hint})" if hint else ""))
    for k in required:
        if k not in obj:
            p.add(where, f"missing required field {k!r}")
    return True


def parse_ref(ref: str):
    """`<step>.facts` or `<step>.<result>.<field>` -> tuple, else None."""
    if not isinstance(ref, str):
        return None
    parts = ref.split(".")
    if not all(ID_RE.match(x) for x in parts):
        return None
    if len(parts) == 2 and parts[1] == "facts":
        return (parts[0], "facts", None)
    if len(parts) == 3:
        return tuple(parts)
    return None


# --------------------------------------------------------------------------- workflow checks


def step_results(step: dict, input_type: str) -> list[str]:
    if "agent" in step:
        return list((step["agent"].get("results") or {}).keys()) + list(RESERVED_RESULTS)
    op = OPERATIONS.get(step.get("operation"))
    if not op:
        return list(RESERVED_RESULTS)
    res = list(op[0])
    if step["operation"] == "commit-and-push" and input_type == "pull-request":
        res.append("stale-head")
    if (step.get("with") or {}).get("requires"):
        res.append("requirement-unmet")
    return res + list(RESERVED_RESULTS)


def check_payload(p: Problems, where: str, results) -> None:
    if not isinstance(results, dict) or not results:
        p.add(where, "an agent step declares at least one Step Result")
        return
    for name, fields in results.items():
        w = f"{where}.{name}"
        if name in RESERVED_RESULTS or name in ("requirement-unmet", "stale-head"):
            p.add(w, f"{name!r} is a runtime-reserved result and cannot be declared")
        if not ID_RE.match(str(name)):
            p.add(w, "result names match [a-z0-9-]+")
        if fields is None:
            fields = {}
        if not isinstance(fields, dict):
            p.add(w, "a result payload is a mapping of field -> type")
            continue
        for f, t in fields.items():
            if isinstance(t, dict) and set(t) == {"list"} and t["list"] in FIELD_TYPES:
                continue
            if isinstance(t, dict) and set(t) == {"enum"} and isinstance(t["enum"], list) and t["enum"]:
                continue
            if t not in FIELD_TYPES:
                p.add(f"{w}.{f}", f"unknown field type {t!r} (use one of {', '.join(FIELD_TYPES)}, {{list: T}}, {{enum: [...]}})")


def field_type(steps: dict, step_id: str, result: str, field: str):
    st = steps.get(step_id) or {}
    if "agent" not in st:
        return None
    fields = ((st["agent"].get("results") or {}).get(result)) or {}
    return fields.get(field) if isinstance(fields, dict) else None


def check_ref(p: Problems, where: str, steps: dict, ref, *, document_only=False):
    parsed = parse_ref(ref)
    if not parsed:
        p.add(where, f"{ref!r} is not a reference (`<step>.facts` or `<step>.<result>.<field>`); expressions are not supported")
        return None
    sid, res, fld = parsed
    if sid not in steps:
        p.add(where, f"{ref!r} names unknown step {sid!r}")
        return None
    if res == "facts":
        if document_only:
            p.add(where, f"{ref!r}: an Execution Summary cannot be published; use a `document` field")
        return parsed
    t = field_type(steps, sid, res, fld)
    if t is None:
        p.add(where, f"{ref!r} is not a payload field of a model-declared result of {sid!r}")
        return None
    if document_only and t != "document":
        p.add(where, f"{ref!r} has type {t!r}; publication bodies come only from `document` fields (Step Artifacts)")
    return parsed


def validate_workflow(p: Problems, wid: str, wf, models: dict, base: Path) -> None:
    W = f"workflows.{wid}"
    if not keys_ok(p, W, wf, {"model", "input", "limits", "max-parallel-subagents", "outcomes", "start", "defaults", "steps"},
                   {"model", "input", "limits", "start", "steps"}):
        return
    if wf.get("model") not in models:
        p.add(f"{W}.model", f"unknown model {wf.get('model')!r}")

    # input selector
    inp = wf.get("input") or {}
    input_type = inp.get("type")
    if keys_ok(p, f"{W}.input", inp, {"type", "label", "require-all", "require-any", "exclude", "drafts", "skip-open-agent-pr"},
               {"type", "label"}):
        if input_type not in ("issue", "pull-request"):
            p.add(f"{W}.input.type", "must be `issue` or `pull-request` (exactly one input type per Workflow)")
        label = inp.get("label")
        for flt in ("require-all", "require-any", "exclude"):
            if label in (inp.get(flt) or []):
                p.add(f"{W}.input.{flt}", f"the Selection Label {label!r} cannot also be a filter")
        if "drafts" in inp and input_type != "pull-request":
            p.add(f"{W}.input.drafts", "`drafts` applies only to pull-request inputs")
        if "drafts" in inp and inp["drafts"] not in ("include", "exclude"):
            p.add(f"{W}.input.drafts", "`drafts` is `include` or `exclude`")
        if "skip-open-agent-pr" in inp and input_type != "issue":
            p.add(f"{W}.input.skip-open-agent-pr", "applies only to issue inputs")

    # Attempt Ceiling
    ceiling = wf.get("limits") or {}
    if keys_ok(p, f"{W}.limits", ceiling, set(METERS), set(METERS)):
        for m, v in ceiling.items():
            if not isinstance(v, (int, float)) or v <= 0:
                p.add(f"{W}.limits.{m}", "must be a positive number")

    for oc in (wf.get("outcomes") or {}):
        if oc not in ALL_OUTCOMES:
            p.add(f"{W}.outcomes.{oc}", f"unknown Terminal Outcome (one of {', '.join(ALL_OUTCOMES)})")
        elif not keys_ok(p, f"{W}.outcomes.{oc}", wf["outcomes"][oc], {"labels"}, {"labels"}):
            pass

    defaults = wf.get("defaults") or {}
    keys_ok(p, f"{W}.defaults", defaults, {"max-executions", "routes"})
    if "limits" in defaults:
        p.add(f"{W}.defaults", "there is no `defaults.limits`")
    d_routes = defaults.get("routes") or {}
    for k in d_routes:
        if k not in RESERVED_RESULTS:
            p.add(f"{W}.defaults.routes.{k}", "defaults may route only runtime-reserved results (`error`, `limit-exceeded`)")
    d_max = defaults.get("max-executions", 1)

    steps = wf.get("steps")
    if not isinstance(steps, dict) or not steps:
        p.add(f"{W}.steps", "a mapping of step id -> step is required")
        return
    if isinstance(steps, list):
        return
    start = wf.get("start")
    if start not in steps:
        p.add(f"{W}.start", f"{start!r} is not a step id")

    reserved_allow: dict[str, dict] = {}
    plain_allow: dict[str, dict] = {}
    routes_all: dict[str, dict] = {}
    inputs_of: dict[str, list] = {}

    for sid, st in steps.items():
        S = f"{W}.steps.{sid}"
        if not ID_RE.match(str(sid)):
            p.add(S, "step ids match [a-z0-9-]+")
        if not keys_ok(p, S, st, {"agent", "operation", "with", "max-executions", "limits", "reserved", "routes"}, {"routes"}):
            continue
        is_agent, is_op = "agent" in st, "operation" in st
        if is_agent == is_op:
            p.add(S, "a step has exactly one executor: `agent` or `operation`")
            continue
        req_inputs: list = []
        if is_agent:
            a = st["agent"]
            if keys_ok(p, f"{S}.agent", a, {"instructions", "tools", "inputs", "results"}, {"results"}):
                for i, ins in enumerate(a.get("instructions") or []):
                    if not (isinstance(ins, dict) and len(ins) == 1 and next(iter(ins)) in ("skill", "prompt")):
                        p.add(f"{S}.agent.instructions[{i}]", "each instruction is `skill: <name>` or `prompt: <file>`")
                    elif "skill" in ins and ins["skill"] not in SKILL_BUNDLE:
                        p.add(f"{S}.agent.instructions[{i}]", f"skill {ins['skill']!r} is not in the pinned Skill Bundle")
                    elif "prompt" in ins and not (base / ins["prompt"]).resolve().is_relative_to(base):
                        p.add(f"{S}.agent.instructions[{i}]", "prompt files stay inside the configuration directory")
                    elif "prompt" in ins and not (base / ins["prompt"]).is_file():
                        p.add(f"{S}.agent.instructions[{i}]", f"prompt file {ins['prompt']!r} does not exist")
                for t in a.get("tools") or []:
                    if t in ALWAYS_PRESENT_TOOLS:
                        p.add(f"{S}.agent.tools", f"{t!r} is always present and cannot be declared")
                    elif t not in TOOL_GROUPS:
                        p.add(f"{S}.agent.tools", f"unknown tool group {t!r} (GitHub writes are Runtime Operations, never model tools)")
                for i, ref in enumerate(a.get("inputs") or []):
                    optional = isinstance(ref, dict) and set(ref) == {"optional"}
                    r = ref["optional"] if optional else ref
                    parsed = check_ref(p, f"{S}.agent.inputs[{i}]", steps, r)
                    if parsed and not optional:
                        req_inputs.append((r, parsed))
                check_payload(p, f"{S}.agent.results", a.get("results"))
            if "with" in st:
                p.add(S, "`with` holds operation parameters; agent steps configure `agent`")
        else:
            op = st["operation"]
            spec = OPERATIONS.get(op)
            if not spec:
                p.add(f"{S}.operation", f"unknown Runtime Operation {op!r} (catalog: {', '.join(OPERATIONS)})")
                continue
            _, with_keys, only_type, publishes = spec
            if only_type and input_type and only_type != input_type:
                p.add(f"{S}.operation", f"{op!r} is available only in `{only_type}` Workflows (this one is `{input_type}`)")
            w = st.get("with") or {}
            keys_ok(p, f"{S}.with", w, with_keys)
            if "facts" in w:
                p.add(f"{S}.with.facts", "there is no facts-mode comment; the runtime's terminal comment already reports the Attempt facts")
            if "title" in w:
                p.add(f"{S}.with.title", "the pull-request title is a fixed runtime template over the Target Issue title")
            if op in ("open-pull-request", "publish-review", "post-comment") and "body" not in w:
                p.add(f"{S}.with", f"{op!r} needs `body`")
            if "body" in w:
                parsed = check_ref(p, f"{S}.with.body", steps, w["body"], document_only=True)
                if parsed:
                    req_inputs.append((w["body"], parsed))
            for i, rq in enumerate(w.get("requires") or []):
                pair = next(iter(rq.items())) if isinstance(rq, dict) and len(rq) == 1 else None
                if pair not in REQUIRABLE:
                    p.add(f"{S}.with.requires[{i}]", f"{rq!r} is not requirable evidence (supported: run-validation: passed)")
            if "limits" in st:
                p.add(f"{S}.limits", "`limits` apply only to agent steps; operations are bounded by code-owned timeouts")
            if st.get("reserved"):
                p.add(f"{S}.reserved", "only agent steps can be Reserved Steps")
        inputs_of[sid] = req_inputs

        mx = st.get("max-executions", d_max)
        if not isinstance(mx, int) or isinstance(mx, bool) or mx < 1:
            p.add(f"{S}.max-executions", "must be a positive integer")

        lim = st.get("limits")
        if lim is not None and is_agent:
            if keys_ok(p, f"{S}.limits", lim, set(METERS)):
                if st.get("reserved"):
                    missing = [m for m in METERS if m not in lim]
                    if missing:
                        p.add(f"{S}.limits", f"a Reserved Step declares all four meters (missing: {', '.join(missing)})")
                    reserved_allow[sid] = lim
                else:
                    plain_allow[sid] = lim
        elif st.get("reserved") and is_agent:
            p.add(f"{S}.reserved", "a Reserved Step declares a complete Step Allowance in `limits`")

        # routes
        routes = st.get("routes")
        if not isinstance(routes, dict):
            p.add(f"{S}.routes", "a mapping result -> target is required")
            continue
        results = step_results(st, input_type)
        eff = {**{k: v for k, v in d_routes.items() if k in results}, **routes}
        for k, tgt in routes.items():
            if k == "*" or k == "default":
                p.add(f"{S}.routes.{k}", "there is no wildcard route; route every result explicitly")
            elif k not in results:
                p.add(f"{S}.routes.{k}", f"{k!r} is not a possible result of this step (results: {', '.join(results)})")
        for r in results:
            if r not in eff:
                p.add(f"{S}.routes", f"result {r!r} is not routed" + ("" if r in RESERVED_RESULTS else " (business results are never defaulted)"))
        clean = {}
        for k, tgt in eff.items():
            if k not in results:
                continue
            if isinstance(tgt, list):
                p.add(f"{S}.routes.{k}", "a Route has exactly one target; fan-out is deferred")
                continue
            if isinstance(tgt, dict):
                if set(tgt) != {"end"}:
                    p.add(f"{S}.routes.{k}", "a Workflow End is written `{end: <category>}`")
                    continue
                cat = tgt["end"]
                if cat in RUNTIME_ONLY_OUTCOMES:
                    p.add(f"{S}.routes.{k}", f"`{cat}` is a runtime-only Terminal Outcome and cannot be selected")
                    continue
                if cat not in YAML_ENDS:
                    p.add(f"{S}.routes.{k}", f"unknown end category {cat!r} (one of {', '.join(YAML_ENDS)})")
                    continue
                if k in RESERVED_RESULTS and not YAML_ENDS[cat]:
                    p.add(f"{S}.routes.{k}", f"runtime-reserved {k!r} cannot route directly to `{cat}`")
                    continue
            elif tgt not in steps:
                p.add(f"{S}.routes.{k}", f"unknown target {tgt!r}")
                continue
            clean[k] = tgt
        routes_all[sid] = clean

    if p:
        return  # graph checks need a structurally valid workflow

    # limits arithmetic
    if all(m in ceiling for m in METERS):
        pool = {m: ceiling[m] - sum(a.get(m, 0) for a in reserved_allow.values()) for m in METERS}
        for m in METERS:
            if pool[m] <= 0:
                p.add(f"{W}.limits.{m}", f"Reserved Step allowances ({ceiling[m] - pool[m]}) must stay below the Attempt Ceiling ({ceiling[m]})")
        for sid, a in plain_allow.items():
            for m, v in a.items():
                if pool[m] > 0 and v > pool[m]:
                    p.add(f"{W}.steps.{sid}.limits.{m}", f"Step Allowance {v} exceeds the Work Pool {pool[m]}")

    # reachability
    seen, todo = set(), [start]
    while todo:
        s = todo.pop()
        if s in seen:
            continue
        seen.add(s)
        todo += [t for t in routes_all[s].values() if isinstance(t, str)]
    for sid in steps:
        if sid not in seen:
            p.add(f"{W}.steps.{sid}", "unreachable from `start`")

    # limit-exceeded routes must be acyclic (self-routes included)
    for sid in steps:
        chain, cur = [sid], routes_all[sid].get("limit-exceeded")
        while isinstance(cur, str):
            if cur in chain:
                p.add(f"{W}.steps.{sid}", "`limit-exceeded` Routes form a cycle: " + " -> ".join(chain[chain.index(cur):] + [cur]))
                break
            chain.append(cur)
            cur = routes_all[cur].get("limit-exceeded")
    if p:
        return

    # required-input availability: forward may-analysis of "latest result of each step"
    entry = must_analysis(start, steps, routes_all)
    for sid, reqs in inputs_of.items():
        for ref, (x, r, _f) in reqs:
            got = entry.get(sid, {}).get(x, {"<absent>"})
            if r == "facts":
                if "<absent>" in got:
                    p.add(f"{W}.steps.{sid}", f"required input {ref!r} may be absent ({x!r} has not run on some path); declare it optional")
            elif got != {r}:
                p.add(f"{W}.steps.{sid}", f"required input {ref!r} is not proven: latest {x!r} may be {sorted(got)}; declare it optional")


def must_analysis(start, steps, routes):
    """entry[s][x] = set of possible latest results of step x when s is entered."""
    absent = "<absent>"
    init = {x: {absent} for x in steps}
    entry = {start: {x: set(v) for x, v in init.items()}}
    work = [start]
    while work:
        s = work.pop()
        st = entry[s]
        outs = []
        for res, tgt in routes[s].items():
            if isinstance(tgt, str):
                outs.append((tgt, {**st, s: {res}}))
        # Execution Ceiling refusal: no new execution, s ran before, follow its limit-exceeded Route
        tgt = routes[s].get("limit-exceeded")
        prior = st[s] - {absent}
        if isinstance(tgt, str) and prior:
            outs.append((tgt, {**st, s: prior}))
        for tgt, out in outs:
            cur = entry.get(tgt)
            if cur is None:
                entry[tgt] = {x: set(v) for x, v in out.items()}
                work.append(tgt)
            else:
                changed = False
                for x, v in out.items():
                    if not v <= cur[x]:
                        cur[x] |= v
                        changed = True
                if changed:
                    work.append(tgt)
    return entry


def validate_file(path: Path):
    p = Problems()
    try:
        cfg = strict_load(path.read_text())
    except yaml.YAMLError as e:
        p.add(path.name, f"YAML rejected: {str(e).splitlines()[0] if str(e) else e}")
        return None, p
    if not keys_ok(p, "<root>", cfg, {"version", "target", "models", "workflows"}, {"version", "target", "models", "workflows"}):
        return cfg, p
    if cfg.get("version") != 2:
        p.add("version", "must be 2")
    for wid, wf in (cfg.get("workflows") or {}).items():
        validate_workflow(p, wid, wf, cfg.get("models") or {}, path.parent.resolve())
    return cfg, p


# --------------------------------------------------------------------------- compile view


def compiled_view(cfg) -> str:
    out = []
    for wid, wf in cfg["workflows"].items():
        d = (wf.get("defaults") or {}).get("routes") or {}
        out.append(f"## {wid}  (input: {wf['input']['type']}, start: {wf['start']})")
        out.append(f"add_edge(START, {wf['start']!r})")
        for sid, st in wf["steps"].items():
            results = step_results(st, wf["input"]["type"])
            eff = {r: (st["routes"].get(r) or d.get(r)) for r in results}
            pm = {r: (f"~end:{t['end']}" if isinstance(t, dict) else t) for r, t in eff.items()}
            kind = "agent" if "agent" in st else f"operation:{st['operation']}"
            out.append(f"add_node({sid!r}, registry[{kind!r}])  max-executions={st.get('max-executions', (wf.get('defaults') or {}).get('max-executions', 1))}")
            out.append(f"add_conditional_edges({sid!r}, router, {pm})")
        ends = sorted({t["end"] for st in wf["steps"].values() for t in list(st["routes"].values()) + list(d.values()) if isinstance(t, dict)})
        for e in ends:
            out.append(f"add_node('~end:{e}', record_workflow_end); add_edge('~end:{e}', END)")
        out.append("")
    return "\n".join(out)


def mermaid(cfg, wid) -> str:
    wf = cfg["workflows"][wid]
    d = (wf.get("defaults") or {}).get("routes") or {}
    lines = ["flowchart TD", f"  START((start)) --> {wf['start'].replace('-', '_')}"]
    for sid, st in wf["steps"].items():
        n = sid.replace("-", "_")
        label = f"{sid}<br/><i>{'agent' if 'agent' in st else st['operation']}</i>"
        lines.append(f"  {n}[\"{label}\"]" if "agent" in st else f"  {n}([\"{label}\"])")
        for r in step_results(st, wf["input"]["type"]):
            t = st["routes"].get(r) or d.get(r)
            dashed = r in RESERVED_RESULTS
            tgt = f"END_{t['end']}" if isinstance(t, dict) else t.replace("-", "_")
            arrow = f"-. {r} .->" if dashed else f"-- {r} -->"
            lines.append(f"  {n} {arrow} {tgt}")
    used = {ln.split("END_")[1] for ln in lines if "END_" in ln}
    for e in YAML_ENDS:
        if e in used:
            lines.append(f'  END_{e}{{{{"end: {e}"}}}}')
    return "\n".join(lines)


def main(argv):
    if "--invalid" in argv:
        bad = 0
        for f in sorted((HERE / "invalid").glob("*.yaml")):
            expect = re.search(r"^# expect: (.+)$", f.read_text(), re.M)
            _, p = validate_file(f)
            hit = bool(p) and (not expect or any(expect.group(1) in x for x in p))
            bad += not hit
            print(f"{'ok  ' if hit else 'MISS'} {f.name}")
            for x in p:
                print(f"       {x}")
        print(f"\n{bad} invalid case(s) not rejected as expected")
        return 1 if bad else 0
    path = Path(argv[0])
    cfg, p = validate_file(path)
    if "--json" in argv:
        print(json.dumps({"config": cfg, "problems": p}, indent=1))
        return 0
    for x in p:
        print(x)
    if not p:
        print(f"{path.name}: valid")
        if "--graph" in argv:
            print(compiled_view(cfg))
        if "--mermaid" in argv:
            print(mermaid(cfg, argv[argv.index("--mermaid") + 1]))
    return 1 if p else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
