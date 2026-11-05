# MVP acceptance matrix

**Status: current.** This document defines what must be true before the MVP is finished, and how each thing is shown to be true. Behaviour is defined by [the MVP runtime contract](../contract/mvp-runtime-contract.md). The slices are in [the MVP implementation plan](./mvp-implementation-plan.md). [The v1 acceptance matrix](./v1-acceptance-matrix.md) is a frozen design record. Each of its rows is either carried here under its own id, or annotated there as `superseded by <id>` or `deferred`.

## How to read a row

Every row states four things besides its scenario:

- **Layer.** The section a row sits in is its layer, L1–L4. The layers are described below. A row's prefix says where the row came from, not which layer it is in.
- **Owed by.** The slice that must make the row green: M1–M8. **`met (pre-MVP)`** marks a v1 row over a module no slice touches; it is already green and runs as a regression. **`withdrawn`** marks a row the contract no longer requires; it keeps its id and wording, and nothing proves it.
- **Contract.** The section of [the MVP runtime contract](../contract/mvp-runtime-contract.md) the row proves.
- **Source.** The decision the row comes from.

### Row identifiers

Ids are stable and are never renumbered. A row whose meaning changes gets a new id; it is never rewritten under the old one.

| Prefix | Issued by | Source cited as |
| --- | --- | --- |
| `P-*` | [What is the MVP parity contract, and which v1 guarantees does it supersede?](https://github.com/lbacik/coding-agent/issues/71#issuecomment-6542177173) | #71 |
| `W-*` | [What does workflow YAML configure, and what does each ordered step guarantee?](https://github.com/lbacik/coding-agent/issues/72#issuecomment-6542567154) | #72 |
| `G-*` | [Can a LangGraph skill step reproduce the reference /implement behavior?](https://github.com/lbacik/coding-agent/issues/73#issuecomment-6542890368) | #73 |
| `REC-*` | [How do Attempts recover and publish without duplicating GitHub effects?](https://github.com/lbacik/coding-agent/issues/74#issuecomment-6543048248) | #74 |
| `L1-*`, `L2-*`, `L3-*`, `L4-*` | the v1 matrix. Each is carried **unchanged**, with its v1 wording. References inside it to v1 transitions (T*) and restart scenarios (R*) are historical. | v1 |
| `C-*` | this matrix. Each `C-*` row covers either a contract rule that no issued row proves, or a v1 row whose meaning the MVP changed. | the contract section, and the decision behind its rule; [Write the MVP runtime contract](https://github.com/lbacik/coding-agent/issues/81) (#81) where the contract made a choice the resolutions left open |

The P-, W-, G- and REC-* rows keep the wording of the resolution that issued them. The only change is the split into "Scenario" and "Expected". #74 writes branches as `agent/<N>-<n>`; that is shorthand for `agent/<issue>/<n>-<slug>` (contract §13).

## Four layers, and why they are separate

Separating the layers is what keeps the paid runs down to the few that actually buy information. A scenario about a crash between two writes learns nothing from being run against a real model. A scenario about `usage_metadata` learns nothing from being run against a real GitHub.

| Layer | What it exercises | Fixtures | Cost | When it runs |
| --- | --- | --- | --- | --- |
| **L1: Provider contract** | The model provider's own contract: tool calling, `usage_metadata`, the Provider Capability Assertion | Real provider API, smallest possible prompts | small, real | Explicitly, marked; never in the default run |
| **L2: Language and profile** | Bootstrap, test and check commands per language; evidence parsing; the profile and its refusals | Three real repositories, inside the image | none | Default run, in the image |
| **L3: Behaviour scenarios** | The Attempt graph, the step and review subgraphs, the Supervisor, the write sequence, failure and recovery | Fake tracker and scripted model, both in `tests/support/` | none | Default run |
| **L4: Real smoke** | One complete `issue → pull request` through `agent run`, Python × Anthropic | Sandbox repository, real provider | real | Manual, before a release |

**Every slice runs end to end against a scripted model before any live run** (#73).

**The fake tracker is a liability unless it is held to the real one.** The fake tracker and the GitHub adapter sit behind the same `TrackerPort` and run the same contract suite (C-TRK-1). The real side of that suite runs against the sandbox repository. Without it, L3 tests our idea of GitHub rather than GitHub.

**Record/replay is used for neither.** Against GitHub it cannot produce the sequences that matter, such as a death between two writes or a human closing a pull request. Against the provider, the cassettes go stale on the first prompt change and from then on test history.

---

## L1: Provider contract

Runs for **both** `openai:*` and `anthropic:*`.

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| L1-1 | Bind the implementer's toolset and ask for a tool call, **on the endpoint the adapter pins** | The call arrives with a well-formed argument object — a list arrives as a list, a mapping as a mapping — and the provider accepts its own history back when the assistant turn is returned verbatim ([ADR 0010](../adr/0010-the-assistant-turn-is-carried-back-verbatim.md)). False for `openai:*` on `init_chat_model`'s default endpoint, which refuses function tools together with a reasoning effort | M1 | §8, §11 | v1 |
| L1-3 | Read `usage_metadata` from a response | Tokens present and non-zero. **Cost is derivable only from a configured Price Table, per bucket** — `input_tokens` is the whole prompt including cache hits, and an Anthropic cache write reports in `ephemeral_5m_input_tokens` while `cache_creation` stays zero | M5 | §2.1, §11 | v1 |
| L1-4 | Preflight against a model lacking a required capability | The Worker refuses to start; no Attempt is opened; the process exits nonzero | M5 | §3, §18 | v1 |
| L1-4a | The Provider Capability Assertion is a **live call**, not a table lookup | Tool calling, the pinned effort level, non-zero usage reporting and a Price Table entry are each established by attempting ([ADR 0009](../adr/0009-provider-capability-is-established-by-attempting.md)). A static-table implementation fails this row | M5 | §18 | v1 |
| L1-5 | Provider returns overloaded | **Unit-level over a synthetic response**, not a run: 529/503 cannot be summoned on demand. The retry hits the **same** Pinned Model, asserted structurally from the model recorded on every response plus the absence of any fallback wiring | met (pre-MVP) | §8 | v1 |
| L1-6 | A small real implementation task, per provider | Completes, verified from the test runner's own exit code rather than the model's account; the Pinned Model, endpoint included, is recorded in the Run Ledger beside the Fingerprint | M1 | §5, §9 | v1 |

**A token ceiling is per pin, not global.** The two providers counted the identical prompt at 6818 and 4414 tokens, so the context configuration is one set per Pinned Model (§2.1), and the token ceiling is the `limits.tokens` of a Workflow that references exactly one Pinned Model (§11, [#119](https://github.com/lbacik/coding-agent/issues/119)).

---

## L2: Language and profile

Runs inside the image against three real repositories, one per language.

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| L2-1 | Bootstrap, `test_all`, `test_targeted` and `checks` per language (Python/uv/pytest, PHP/composer/PHPUnit, TypeScript/pnpm/vitest) | All run; Validation Evidence records commands, exit codes, executed counts and individual failure identifiers | M1 | §7, §12 | v1 |
| L2-2 | `test_targeted` with `{path}` | The named test file runs, and only it | M1 | §7, §9 | v1 |
| L2-3 | A suite collecting **zero** tests | Failed validation — missing evidence, not a pass, despite exit code zero | M4 | §12 | v1 |
| L2-8 | `checks: none` | Satisfied, not skipped | met (pre-MVP) | §7, §12 | v1 |
| L2-9 | `checks: []` | Rejected as ambiguous | met (pre-MVP) | §7 | v1 |
| L2-12 | `toolchain` version outside the Supported Toolchain Matrix | `failed` / `unsupported-environment`; the comment names the declared requirement beside the image's matrix | M1 | §7 | v1 |
| L2-13 | Unknown `schema` version | `failed` / `unsupported-environment` — **not** clarification | M1 | §7 | v1 |
| L2-14 | A declared service is unreachable | `failed` / `unsupported-environment`, before implementation begins | M1 | §7 | v1 |
| L2-17 | The candidate edits `project-profile.yml` during the Attempt | Validation still uses the Validation Contract pinned at the Base Revision; the pull request says so | M4 | §7, §15 | v1 |
| L2-18 | A repository with two applications, profile declaring one working area | Validation covers the declared area only; the pull request states which | M4 | §7, §15 | v1 |
| L2-19 | Skill Bundle verification, each of the six failure modes in S1, **plus two from ADR 0007** — a Role Block anchor absent or occurring more than once, and a Role Block digest mismatch — **plus three from [ADR 0012](../adr/0012-companion-files-are-injected-or-refused-never-fetched.md)**: a **Companion File** the manifest does not list, one the manifest lists and the bundle lacks, and one whose content digest has moved | The **build** fails, naming what was wrong. The companion digests are what stop a pin bump from silently retiring the per-file reading that decided which companions are injected | M1 | §3, §8 | v1 |
| L2-20 | `UV_PYTHON_DOWNLOADS=never` | uv does not fetch an interpreter; an unavailable version is an Unsupported Environment, not a download | met (pre-MVP) | §7 | v1 |
| C-RDY-1 | The profile lacks the `checks` key, or lacks the `evidence` block for a test command. *Replaces L2-10 and L2-11.* | `failed` / `missing-readiness-fact` before any model call; the comment names the missing key | M1 | §7 | #71 |
| C-RDY-2 | No profile, while prose documentation (`AGENTS.md`, `CLAUDE.md`, `docs/agents/*`) is complete. *Replaces L2-15 and L2-16.* | `failed` / `missing-readiness-fact`; no prose file is read for a Readiness Fact | M1 | §7 | #71, #72, #81 |
| C-RDY-3 | `bootstrap` fails at the baseline | `failed` / `infrastructure-failure`, class `setup`; no model call | M1 | §7, §14 | #81 |
| C-RDY-4 | `test_all` executes zero tests at the Base Revision and exits zero | The baseline is not red; the Attempt proceeds to its first step | M1 | §7 | #81 |

---

## L3: Behaviour scenarios

Fake tracker and scripted model. This is the bulk of the suite.

### Workflow Configuration and pinning

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| W-CFG-1 | `version` is missing or unknown | Exits non-zero, with no Attempt and no GitHub write; the message names the YAML path | M5 | §2.2, §2.4 | #72 |
| W-CFG-2 | There is an unknown field at any level | As W-CFG-1 | M5 | §2.2 | #72 |
| W-CFG-3 | A key is duplicated | As W-CFG-1 | M5 | §2.2 | #72 |
| W-CFG-4 | The file uses an anchor, alias, `<<` or a custom tag | As W-CFG-1 | M5 | §2.2 | #72 |
| W-CFG-5 | `--workflow` is missing or names an unknown id | As W-CFG-1. The message lists the available ids | M5 | §2.4 | #72 |
| W-CFG-6 | `skill` names something outside the Skill Bundle, or a bundle member outside the top-level allowlist (for example `code-review`) | As W-CFG-1 | M5 | §2.2 | #72 |
| W-CFG-7 | A step has both `skill` and `prompt_file`, or neither; `steps` is empty; a step id is duplicated | As W-CFG-1 | M5 | §2.1, §2.2 | #72 |
| W-CFG-8 | `prompt_file` is missing, or escapes the YAML directory through `..` or a symlink | As W-CFG-1 | M5 | §2.3 | #72 |
| W-CFG-9 | A referenced env var is unset | As W-CFG-1. The message gives the name and never a value | M5 | §2.4 | #72 |
| W-CFG-10 | A `model` reference is unknown, or the (provider, model) pair is outside the code-owned table | As W-CFG-1 | M5 | §2.1 | #72 |
| W-CFG-11 | `target.kind` is not `github` (unsupported integration) | As W-CFG-1 | M5 | §2.1 | #72 |
| W-CFG-12 | The Selection Label is empty; equal to the `ready_for_human` name or `wontfix`; matching `wayfinder:*`; or listed in the same workflow's `exclude` or `require_*` | As W-CFG-1 | M5 | §2.2 | #72 |
| W-CFG-13 | `agent config validate` is run; `agent run` is run with an invalid configuration | `agent config validate` makes no network call. `agent run` refuses before the first GitHub write (extends P-WRK-3) | M5 | §2.4, §3 | #72 |
| W-CFG-14 | A second process uses the same state directory | It is refused | M8 | §3 | #72 |
| C-CFG-1 | `token_env` or `api_key_env` holds a secret value instead of an environment-variable name | As W-CFG-1; the value is never printed | M5 | §2.1 | #72 |
| C-CFG-2 | A model, workflow or step id does not match `[a-z0-9-]+` | As W-CFG-1 | M5 | §2.2 | #72, #81 |
| C-CFG-3 | A Workflow omits `limits`, `worker` and `target.labels` | The Effective Configuration records the defaults: 60 min, $20, 4 000 000 tokens, 600 tool calls, a 60 s poll interval, a 30 s shutdown grace and `ready-for-human` | M5 | §2.1, §2.5 | #72, #81 |
| W-PIN-1 | The YAML or a prompt file is edited during an Attempt | That Attempt's behaviour does not change | M5 | §2.5 | #72 |
| W-PIN-2 | An Attempt is claimed and ends | The ledger records the Effective Configuration and its digest. The PR and the terminal comment disclose the workflow id and the digest | M5 | §2.5, §15 | #72 |
| W-PIN-3 | A restart with a renamed Selection Label | Recovery removes the pinned label name, not the current one | M8 | §2.5, §17 | #72 |
| W-PIN-4 | A Continuation | It uses the current configuration | M8 | §2.5, §16 | #72 |

### Selection and Claim

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| P-SEL-1 | An issue with both `ready-for-agent` and `ready-for-human` | Never claimed | M6 | §4 | #71 |
| P-SEL-2 | An issue assigned to anyone, including the Agent Identity when no Attempt is open | Never claimed | M6 | §4, §5 | #71 |
| P-SEL-3 | An issue with an open native blocker | Skipped, then claimed once the blocker closes | M6 | §4 | #71 |
| P-SEL-4 | A closed issue; an issue with an empty body; an issue labelled `wontfix` or `wayfinder:*`; an issue with an open marked delivery PR | None is claimed | M6 | §4 | #71 |
| P-SEL-5 | Two eligible issues | The older `created_at` is claimed first | M6 | §4 | #71 |
| C-SEL-1 | A Workflow declares `require_all`, `require_any` and `exclude` | A candidate missing a `require_all` label, carrying none of the `require_any` labels, or carrying an `exclude` label is not claimed. The filters are evaluated again at the Claim read-back, and a failure there releases the Claim. They are never evaluated at a later Gate. The Worker never writes a filter label | M6 | §4, §5 | #72 |
| P-CLM-1 | A Claim | It assigns the Agent Identity. It adds no label and writes no comment | M6 | §5 | #71 |
| P-CLM-2 | Another assignee appears before the read-back | The Worker removes only its own assignment, does not proceed with that Attempt, and takes the next candidate | M6 | §5 | #71 |
| P-CLM-3 | A process loss right after the assignment | The issue is never left assigned without an Attempt that can be finished or released | M8 | §5, §17 | #71 |
| L3-SEL-11 | The agent attempts to apply the Selection Label | Structurally impossible: no such tool exists in the model's toolset and no node performs it | M1 | §1, §18 | v1 |
| L3-CLR-19 | The Worker is asked to edit the Target Issue's body | Structurally impossible: no node and no model tool performs it, which is what makes L3-CLR-13 a human's answer | M1 | §18 | v1 |

### Context, readiness and the baseline

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| P-CTX-1 | A human comment carries an instruction | The model input contains the title and body. The instruction never reaches the model | M1 | §8 | #71 |
| W-STP-7 | A file in the Target Repository carries an instruction | It never reaches the model as a step instruction | M5 | §2.3, §8 | #72 |
| P-RDY-1 | An issue with no Acceptance Criteria | No model call; the comment names the missing fact; the Selection Label and the Worker's assignment are removed; `ready-for-human` is added | M1 | §7, §14 | #71 |
| P-RDY-2 | A missing Seam Set | Ends the same way as P-RDY-1 | M1 | §7, §14 | #71 |
| P-RDY-3 | An invalid profile, or an Unsupported Environment | Ends the same way, with its reason | M1 | §7, §14 | #71 |
| P-RDY-4 | A red `test_all` or check at the Base Revision | Ends with no model call and a comment naming the failures | M1 | §7, §14 | #71 |
| P-CMD-2 | A configured base branch other than `main` | It works, and the profile is read at its Base Revision | M5 | §2.1, §7 | #71 |
| C-WSP-1 | A workspace left dirty by an earlier Attempt, with untracked and ignored files | The next Attempt's workspace is built from the mirror by `reset --hard` to its start commit and `clean -fdx`; nothing from the earlier Attempt reaches it | M1 | §7 | #74 |
| L3-CLR-20 | The Target Issue **names the interface under test** | The Seam Set resolves from it; straight to implementation, no comment. This is what bounds L3-CLR-16 against L3-CLR-1 | M1 | §7 | v1 |
| L3-IMP-11 | `implement` is entered with **no Seam Set in state** | `confirm_seam` refuses: `failed`, non-transient, explanatory comment; **no model call is made**. An internal invariant violation, not a clarification — a correct run cannot reach it, because `evaluate_readiness` resolved the fact or routed to T3 ([ADR 0008](../adr/0008-the-tdd-seam-gate-is-a-readiness-fact.md)) | M1 | §7 | v1 |

### The Attempt graph, skills and the Pinned Prefix

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| C-GRF-1 | The Attempt graph is built for any Workflow Configuration | It has the eleven nodes of §6, in that order, whatever the Workflow says. It has no merge node, and it is compiled without a checkpointer. Asserted **structurally** on the compiled graph | M1 | §1, §6 | #73, #74 |
| G-HDR-1 | The Attempt Header is composed | It maps every slash-command reference in the activated skills to its activation tool | M1 | §8 | #73 |
| C-SKL-1 | The step's tool set is bound. *Replaces L3-IMP-3.* | All four `activate_*` tools are bound from the first turn. Each tool's description is the skill's frontmatter `description`, verbatim. A second call to the same activation tool in an Attempt returns a short "already active" acknowledgement, not the body again | M1 | §8 | [#60](https://github.com/lbacik/coding-agent/issues/60) |
| C-SKL-2 | A step's first request is composed. *Replaces L3-IMP-14.* | It holds the Pinned Prefix (Attempt Header and Target Issue), then the forced activation call, and no skill body. A skill body and its Companion Files reach the model only as that skill's activation result. That result is verbatim and is exempt from result capping. No tool in the toolset resolves a path under a skill's directory. Asserted **structurally**, never from the model's account | M1 | §8 | [#64](https://github.com/lbacik/coding-agent/issues/64), [ADR 0011](../adr/0011-the-pinned-prefix-is-a-region-not-a-rule.md), [ADR 0012](../adr/0012-companion-files-are-injected-or-refused-never-fetched.md) |
| L3-IMP-1 | `/implement` and nested `/tdd` activate | Both present in the activation registry; neither activates twice | M1 | §8 | v1 |
| L3-IMP-15 | The Attempt Header is composed | It answers the upstream text the injection leaves without a referent: `codebase-design`'s *Going deeper* companions are unavailable in an Attempt, and `tdd`'s companion examples are TypeScript while the Target Project's language may not be — principles carry, idioms do not. Both are **answers**, not edits to the injected files ([ADR 0007](../adr/0007-upstream-skill-text-is-answered-never-rewritten.md)) | M1 | §8 | v1 |
| L3-IMP-9 | The model attempts a GitHub mutation | No such tool exists | M1 | §1 | v1 |
| L3-IMP-5 | An oversized tool result | Head + tail + a pointer **naming the artifact and its full size**; the model can read a chosen slice. **Confirmed as an acceptance row a run demonstrates** — unlike `L3-IMP-3`, both pins read slices unprompted and repeatedly, the first within two turns ([#25](https://github.com/lbacik/coding-agent/issues/25)) | M1 | §11 | v1 |
| L3-IMP-13 | The **Pinned Prefix alone** crosses the context-window threshold | The Attempt refuses to open: nonzero, explanatory, **no model call**. Never a partial drop of the prefix. The same refusal applies when tool-schema/request overhead leaves no usable capacity. | M1 | §11 | v1 |
| L3-ERR-2 | An unclassified exception | **Permanent**, not transient. `failed` | M1 | §14 | v1 |

### Workflow Steps and declaring completion

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| W-STP-1 | A non-final step ends `cannot-continue` | `completion not established`. Later steps do not start, and any diff is pushed | M2 | §8 | #72 |
| W-STP-2 | Any step is opened | `report_completion` is never offered in a non-final step, and `report_step` is never offered in the final step | M2 | §8 | #72 |
| W-STP-4 | A Context-Window Handoff in a `prompt_file` step | The step reopens with a forced `activate_step` carrying identical bytes | M2 | §8, §11 | #72 |
| W-STP-5 | A step boundary | The next step opens without the previous step's history. The `report_step` justification is in the Continuation Snapshot | M2 | §8 | #72 |
| G-STP-1 | Two steps run | Step subgraphs share no message list. The second step's first request contains the Pinned Prefix and the Continuation Snapshot, and none of the first step's turns | M2 | §6, §8 | #73 |
| C-STP-1 | A non-final step ends in silence after its two continuations | The step list stops; `completion not established` / `undeclared`; later steps do not start, and any diff is pushed | M2 | §8 | #72, #81 |
| C-ERR-1 | The provider still fails after its retries, in any step. *Replaces L3-ERR-1.* | The retries hit the same Pinned Model. The step list stops and the Attempt ends `failed` / `infrastructure-failure`, class `provider`. There is no model swap and no automatic new Attempt | M2 | §8, §14 | #71 |
| L3-IMP-24 | **The pilot regression** — a scripted model explores the repository, runs an existing passing test unrelated to any Acceptance Criterion, then returns a turn with no tool calls | Two deterministic continuations are issued, each naming the uncovered criteria and the absent diff; the node then ends as `completion not established` / `undeclared`. No pull request, no `Closes`. **Fails if the run produces `verified completion` or `already satisfied`** — the two names the superseded design would have given it ([ADR 0014](../adr/0014-completion-is-declared-and-substantiated-never-inferred.md)) | M2 | §8, §9 | v1 |
| L3-IMP-25 | `report_completion` is the only non-exceptional exit | Asserted **structurally** on the loop: a no-tool-call response never by itself yields an Implementation Outcome, and no other path leaves the tool loop except a ceiling, the verification reserve or a classified failure | M2 | §9 | v1 |
| L3-IMP-26 | The Acceptance Criteria list is composed | Enumerated by `evaluate_readiness` from the Target Issue body with **no model call**, and the `report_completion` schema is generated from that list, so a claim answering fewer criteria than the Attempt holds cannot be expressed. A Target Issue naming none is a missing Readiness Fact | M2 | §7, §9 | v1 |
| C-IMP-1 | A claim names a test identifier absent from Criterion Evidence, one that did not execute, or one whose latest run did not pass. *Replaces L3-IMP-27.* | Returned with a tool result naming the failing entries; **the return is not a progress event**. After **two** returns the third call is force-accepted: `implemented but unverified` with a candidate diff, `completion not established` / `claims-unverified` without one | M2 | §9 | #71, #73, #81 |
| L3-IMP-28 | The model writes a JUnit file into `{evidence_dir}` through its shell tool, then names its tests in a claim | The write does not succeed and nothing it produced is Criterion Evidence: the directory is owned solely by the runtime's targeted-test tool. Asserted **structurally** on the bound toolset and the directory's ownership, never from the model's account | M2 | §9 | v1 |
| C-FP-1 | A `test_targeted` run whose executed code modifies a non-test workspace file (the #25 scenario) | The Run Ledger records the call's Execution Footprint with the path and its content digests before and after, and the tool result names the changed path with its change kind | M2 | §9, §12 | #27 |
| C-FP-2 | A run whose executed code adds one file and deletes another | The footprint records the added path as `added` with no before digest and the deleted path as `deleted` with no after digest; the tool result names both | M2 | §9, §12 | #27 |
| C-FP-3 | A run whose executed code changes nothing | The call records an empty footprint, and its tool result is byte-for-byte what the diagnostic adapter returned | M2 | §9 | #27 |
| C-FP-4 | A run whose executed code writes only gitignored paths (for example `__pycache__`) | The call records an empty footprint | M2 | §9, §12 | #27 |
| C-FP-5 | A path already dirty before the call | Rewritten with different content during the call, it appears in the footprint; dirty before and after with identical content, it does not | M2 | §9, §12 | #27 |
| C-FP-7 | An Attempt whose footprint is non-empty | It reaches the same Implementation Outcome and Publication Gate verdict as the same Attempt with footprint capture disabled: the footprint is never an input to either | M2 | §9, §12 | #27 |
| C-TRN-1 | The implementing Step's tool calls | The Transcript records every tool call of the step with its arguments, redacted | M2 | §17 | #27 |
| L3-IMP-29 | A claim marks a criterion explicitly `uncovered` | **Accepted**, not rejected; `completion not established` / `criteria-uncovered`; the candidate diff is preserved and the branch pushed as usual | M2 | §9 | v1 |
| L3-IMP-30 | A ceiling is crossed **after** an accepted and validated claim | Still `verified completion` — the category follows the moment of acceptance, not the moment of measurement. Crossing it **before** acceptance yields `saved partial work` with a diff, `completion not established` / `undeclared` without one | M2 | §9, §11 | v1 |
| L3-IMP-31 | A Context-Window Handoff occurs after one continuation and one rejection | The fresh node opens with both budgets restored; the Continuation Snapshot carries neither, because it carries work facts and never runtime policy state. Attempt-wide meters are unbroken across the handoff | M2 | §8, §11 | v1 |
| L3-IMP-10 | **No diff with a complete justification** — the tree is identical to the Base Revision and **every** Acceptance Criterion is covered by verified Criterion Evidence at that revision | `already satisfied`; no branch, no pull request; audit evidence retained; `failed` / `already-satisfied` in the graph. Not a Clarification Round | M1 | §9, §14 | v1 |
| L3-IMP-20 | Finalization has an incomplete or absent Validation Evidence result | Exactly one Implementation Outcome is rendered; only `verified completion` may use `PASS`; absent a diff **and** a complete justification, the result is `completion not established`, never `already satisfied` | M1 | §9 | v1 |

### Progress, context and limits

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| L3-IMP-16 | Five model responses make no qualifying progress | One `soft_stall` record; the next action is narrow implementation, snapshot preparation or verification | M2 | §11 | v1 |
| L3-IMP-17 | A suppressed unchanged targeted diagnostic returns | The same soft-stall path is entered; activity and the diagnostic do not reset progress without a relevant change | M2 | §11 | v1 |
| L3-IMP-18 | Any Attempt-wide meter reaches 70% or 80% | One durable `Gate`, then monotonic `reserve_entry`; broad exploration cannot resume | M2 | §11 | v1 |
| L3-IMP-4 | Context fills | A per-**Pinned Model** estimate crosses its configured threshold: one bounded handoff invocation creates and persists one versioned, redacted Continuation Snapshot, records its digest and context/usage trace, and a fresh node receives only the intact **Pinned Prefix** plus that snapshot. No pre-handoff **Exchange Unit** is reused or reconstructed; within each node assistant turns remain verbatim ([ADR 0013](../adr/0013-context-window-handoff.md)). | M2 | §11 | v1 |
| L3-IMP-12 | A work node continues before handoff | No **Exchange Unit** is split inside the node: no tool result survives without the assistant turn that called it, and no assistant turn without its results. The handoff receives explicit context and the fresh node receives no old unit at all. | M2 | §11 | v1 |
| L3-IMP-21 | Malformed, incomplete, over-limit or unpersistable handoff | No continuation starts; a redacted failure artifact is retained and the Attempt ends `failed` / `handoff-failure`. | M2 | §11, §14 | v1 |
| L3-IMP-23 | Handoff trace | The Attempt exposes handoff count, snapshot digest/version, context estimate, node cap, provider-reported input/output usage and cumulative Attempt-wide usage. | M2 | §11 | v1 |
| C-CTX-1 | A Continuation Snapshot exceeds 32 KiB after redaction | Refused when it is composed: `failed` / `handoff-failure`. A snapshot that is published always fits in a terminal comment | M2 | §11, §15 | #81 |
| P-LIM-1 | A wall-clock, cost, token or tool-call ceiling is crossed | The Attempt ends `failed-limit`. The branch is pushed, the Continuation Snapshot is in the comment, `ready-for-human` is added and there is no PR | M7 | §11, §14, §15 | #71 |
| C-LIM-1 | A ceiling is crossed mid tool loop, before the Delivery Intent. *Replaces L3-IMP-6 and L3-IMP-19.* | The loop stops **inside the node**, with usage already flushed. No further model call and no model-initiated tool call. Deterministic finalization commits the existing edits and pushes the branch, and runs **no** final checks | M7 | §11 | #71, #81 |
| L3-IMP-7 | Wall clock crossed during review | T12, measured from the Ledger's Attempt start; a restart does not reset it (R6) | M7 | §11 | v1 |
| P-CMD-1 | A command times out | The command's whole process group is killed | M7 | §11 | #71 |
| L3-ERR-14 | A hung repository command | Returns as a **tool result** the model can react to, via the shell tool's per-command timeout — not as a process death | M7 | §11 | v1 |

### Review

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| P-DEL-3 | No review is invoked | The Completion Claim is returned once, naming the missing review. If there is still no review, there is no PR | M3 | §9, §10, §12 | #71 |
| P-DEL-4 | A fourth review invocation in one Attempt | It is refused | M3 | §10 | #71 |
| P-DEL-5 | Review findings of any severity | They do not prevent the PR, and the review status is disclosed | M3 | §10, §15 | #71 |
| G-REV-1 | A reviewer conversation ends without `report_review` | It is not a completed review. The review status is `incomplete`, and the review gate is unmet | M3 | §10 | #73 |
| G-REV-2 | A Reviewer Brief is composed | Every Seat Assignment states that the conversation is the reviewer the Role Block describes | M3 | §10 | #73 |
| G-REV-3 | `activate_code_review` is invoked; a stop is detected at the Gate before its push | The review snapshot is pushed before either reviewer starts. A stop detected at the Gate before that push ends the Attempt at that Gate (cancellation → `abandoned`, SIGTERM → `failed`/`interrupted`), with whatever is already pushed kept | M7 | §10, §11 | #73 |
| W-STP-6 | Reviews and meters across several steps | The 3-review limit and all meters are counted across steps | M3 | §10, §11 | #72 |
| C-REV-1 | Review is never invoked; invoked with no later change; invoked and then followed by a change, including in a later step | The status is `not-invoked` and the review gate is unmet; then `fresh`; then `stale`. A `stale` status meets the review gate. Every invocation re-runs both seats against the tree at call time. The status and the invocation count are in the PR body | M3 | §10, §15 | [#62](https://github.com/lbacik/coding-agent/issues/62), #71, #81 |
| L3-REV-1 | The fan-out runs | Exactly two reviewer conversations, separate message lists, read-only toolsets | M3 | §10 | v1 |
| L3-REV-2 | A reviewer receives its instructions | Its own role's, not the whole `/code-review` skill; **no reviewer spawns another pair**. The Role Block is **byte-identical** to that slice of the installed `SKILL.md`; the Seat Assignment carries no review criteria (ADR 0007) | M3 | §10 | v1 |

### The tail: final checks and the Publication Gate

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| C-PUB-1 | The implementation stage ends with each Implementation Outcome in turn | The Delivery Snapshot is committed and pushed whenever there is a diff. Final checks run only for `verified completion`, exactly once, with no retry and no remediation | M4 | §12 | #71 |
| P-DEL-2 | A final check is red | The branch is pushed, there is no PR, the outcome is `failed`/`failing-validation`, and `ready-for-human` is added | M4 | §12, §14 | #71 |
| P-DEL-6 | The issue body is edited during the Attempt | No PR; `failed`/`drift` | M4 | §12, §14 | #71 |
| P-DEL-7 | The base branch advanced during the Attempt | The PR is still opened | M4 | §12 | #71 |
| P-DEL-8 | The tree is dirty after green final checks | No PR | M4 | §12, §14 | #71 |
| P-DEL-9 | The outcomes `implemented but unverified`, `already satisfied` and `completion not established` | None gets a PR. Each ends with its mapped reason | M4 | §9, §14 | #71 |
| C-PUB-2 | Drift, no completed review and red final checks, all in one Attempt | The reason is `drift`, the first unmet condition in the order Drift → review → final checks → clean tree. The terminal comment lists every unmet condition | M4 | §12 | #81 |
| L3-DEL-5 | The base branch has moved | No rebase, no merge; disclosed in the body, naming the commit the work was verified against | M4 | §12, §15 | v1 |
| G-TDD-1 | A pull request is opened | The PR body states, for each claimed test, whether Criterion Evidence ever recorded it failing | M4 | §9, §15 | #73 |
| C-FP-6 | The pull-request body of an Attempt with footprint events | It lists the deduplicated footprint paths, at most 50, anything beyond replaced by an explicit truncation marker giving the total; with no footprint the section states that none changed | M4 | §15 | #27 |
| L3-REV-10 | An oversized review report | Truncated with an explicit marker and a log pointer — never silently | M4 | §15 | v1 |

### Delivery and terminal writes

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| P-DEL-1 | The happy path | The PR is non-draft; its title is the issue title; its body carries `Closes #N`, the marker, the evidence and the review status. A comment links the PR, and the label and assignment are removed. No `ready-for-human` is added | M6 | §14, §15 | #71 |
| P-DEL-10 | Any Attempt | No draft PR is ever created | M6 | §14 | #71 |
| C-OUT-1 | Any Terminal Outcome | The terminal writes happen in this order: the comment, the Selection Label removal, `ready-for-human` (on every outcome without a PR), then removal of the Worker's own assignment | M6 | §14 | #71 |
| C-TRK-1 | The tracker contract suite | The fake tracker and the GitHub adapter pass the same suite; its real side runs against the sandbox repository | M6 | §13 | v1 |
| L3-DEL-14 | A human closed the pull request from this branch | **Withdrawal, not absence**: cooperative stop, comment naming it, `abandoned`. Never reopened | M6 | §13, §14 | v1 |
| L3-DEL-21 | A pull request from an `agent/` branch carrying **no** Attempt Marker in its body | Not treated as ours; the one-directional rule is not read backwards | M6 | §13, §15, §18 | v1 |
| L3-DEL-19 | One branch per Attempt | `agent/<issue>/<n>-<slug>`; no force-push capability is ever used or needed; no branch is ever deleted | M3 | §13, §18 | v1 |
| L3-DEL-20 | Commits | Author and committer are the Agent Identity's id-based noreply address; the `Attempt: #<issue>/<n>` trailer is present; unsigned; not squashed. Under v1's shared account the trailer is the **only** provenance record | M3 | §18 | v1 |
| L3-DEL-15 | Every write kind, crash injected after the write and before its confirmation | No duplicate; the receipt or the remote check makes the re-entry a no-op | M8 | §13, §17 | v1 |

### Cancellation and SIGTERM

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| P-CAN-1 | The Selection Label is removed during implementation | At the next Gate: the work is pushed, the comment is written, the Attempt ends `abandoned`, the assignment is removed and there is no PR | M7 | §11, §14 | #71 |
| P-CAN-2 | The same removal after the Delivery Intent | Publication is not stopped | M7 | §11, §12 | #71 |
| P-CAN-3 | SIGTERM during implementation | No new Claim is made. The Attempt ends `failed`/`interrupted` at its next Gate with the work pushed | M7 | §11 | #71 |
| W-STP-3 | Cancellation is detected between steps | `abandoned` | M7 | §8, §11 | #72 |

### Recovery

These replace v1 R1–R8.

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| P-REC-1 | A process loss before the Delivery Intent | At restart: `failed`/`interrupted`; pushed work intact; the assignment removed; `ready-for-human`; exactly one terminal comment | M8 | §17 | #71 |
| P-REC-2 | A process loss after the Delivery Intent, at any point around PR creation or the comment | Exactly one PR and exactly one terminal comment | M8 | §13, §17 | #71 |
| P-REC-3 | Recovery meets partial work | It never turns partial work into a PR | M8 | §17 | #71 |
| P-REC-4 | Recovery meets pushed commits | It never overwrites them | M8 | §13, §17 | #71 |
| REC-CLM-1 | Crash after the Attempt transaction, before `assign` | At restart, the Attempt is `released`, there is no GitHub write, and the issue is claimable on the next sweep as `#N/n+1` | M8 | §5, §17 | #74 |
| REC-CLM-2 | Crash after `assign`, before read-back | At restart, our assignment is removed and the Attempt is `released`. There is no comment and no label change (P-CLM-3) | M8 | §5, §17 | #74 |
| REC-CLM-3 | An Agent Identity assignment with no open Attempt in the ledger | It is never removed and keeps the issue ineligible (P-SEL-2) | M8 | §4, §5 | #74 |
| REC-INT-1 | Crash during a step with two local unpushed commits and a dirty tree | At restart: both commits are pushed fast-forward; uncommitted files are not committed, and the comment states their count; the outcome is `failed`/`interrupted`, with exactly one comment, `ready-for-human`, the Selection Label removed and the assignment removed; there is no model call (P-REC-1) | M8 | §17 | #74 |
| REC-INT-2 | As REC-INT-1, but the issue was closed during the outage | The outcome is `abandoned` | M8 | §17 | #74 |
| REC-INT-3 | As REC-INT-1, but a human pushed to `agent/<N>-<n>` during the outage | There is no push and no force; the outcome is `interrupted` with a note (P-REC-4) | M8 | §13, §17 | #74 |
| REC-INT-4 | Crash after the review-snapshot push, before its receipt | The remote sha equals the push intent's sha, so it is recognised as ours and the receipt is recorded | M8 | §13 | #74 |
| REC-INT-5 | Recovery writes a terminal comment | It carries the last Continuation Snapshot only if its commit is an ancestor of the pushed head | M8 | §15 | #74 |
| REC-INT-6 | Crash during final checks | The Delivery Snapshot is already pushed and is kept. The outcome is `interrupted` and there is no PR (P-REC-3) | M8 | §12, §17 | #74 |
| REC-OUT-1 | `failed-limit` recorded; crash after the comment, before the label writes | At restart, the labels and assignment are finished, there is no second comment, and the outcome stays `failed-limit` | M8 | §14, §17 | #74 |
| REC-OUT-2 | A readiness refusal recorded; crash before the comment | At restart, exactly one comment carries the recorded reason | M8 | §14, §17 | #74 |
| REC-DEL-1 | Crash after `open_pull_request`, before its receipt | At restart, the PR is found by head branch and marker, none is created, then the comment and labels follow (P-REC-2) | M8 | §13, §17 | #74 |
| REC-DEL-2 | Crash after the terminal comment, before the labels | Labels and assignment are finished, and there is no second comment | M8 | §13, §17 | #74 |
| REC-DEL-3 | A PR closed by a human during the outage | It is not reopened; the outcome is `abandoned` with one comment | M8 | §13, §17 | #74 |
| REC-DEL-4 | The remote branch is not at the Intent's sha | There is no PR; the outcome is `failed`/`branch-moved` with `ready-for-human` | M8 | §13, §17 | #74 |
| REC-DEL-5 | An issue with more than 100 comments, where the Worker's terminal comment is not among the first or last 100 | It is still found, and no duplicate is written | M8 | §13 | #74 |
| REC-DEL-6 | A comment by another account carries our marker | It is not taken as our receipt | M8 | §13, §18 | #74 |
| REC-CFG-1 | A restart with a changed YAML, renamed labels or another `--workflow` | The open Attempt is finished with its pinned values (W-PIN-1/3) | M8 | §2.5, §17 | #74 |
| REC-CFG-2 | An unfinished Attempt whose Effective Configuration record is missing or unreadable | Startup is refused: non-zero exit and no GitHub write | M8 | §3, §17 | #74 |
| REC-SIG-1 | SIGTERM during a 10-minute tool command | The command's process group is killed within the grace period and the Attempt ends `interrupted` while the process is alive (pushed, one comment). No new Claim is made (P-CAN-3) | M7 | §11 | #74 |
| REC-SIG-2 | SIGTERM after the Delivery Intent | Publication completes as `delivered` (P-CAN-2) | M7 | §11, §12 | #74 |
| REC-WRK-1 | An unfinished Attempt exists; recovery writes fail three times in a row | The Worker makes no new Claim while the Attempt is unfinished. Three consecutive recovery write failures halt the Worker with a non-zero exit (P-WRK-2), and there is no restart loop | M8 | §3, §17 | #74 |
| REC-WRK-2 | Crash during workspace cleanup after `finished` | The next Attempt prepares cleanly from the mirror, with no hold | M8 | §7, §17 | #74 |
| REC-WRK-3 | A Continuation reads the previous Continuation Snapshot | It round-trips through the terminal comment by marker lookup and digest. A tampered snapshot is treated as absent | M8 | §15, §16 | #74 |

### Continuation

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| P-HND-1 | The Selection Label is re-applied | Attempt `#N/2` runs on a new branch. That branch is created from the previous head and rebased on the current Base Revision. The snapshot is in the Pinned Prefix, and the criteria come from the current body | M8 | §16 | #71 |
| P-HND-2 | A rebase conflict | `failed`/`continuation-conflict`, with `ready-for-human` and no model call | M8 | §16 | #71 |
| P-HND-3 | The previous branch was deleted | The new Attempt starts from the Base Revision | M8 | §16 | #71 |
| P-HND-4 | A Continuation after a process loss, when no snapshot exists | It continues from the branch and its diff against the base | M8 | §16 | #71 |

### The Worker, identity and errors

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| P-WRK-1 | A Credential Failure | The Worker halts with a non-zero exit and writes no comment | M6 | §3, §13 | #71 |
| P-WRK-2 | Three consecutive infrastructure-class failures | The Worker halts only after the third Attempt is finalized. A restart does not loop | M6 | §3 | #71 |
| P-WRK-3 | A startup refusal: preflight, the Provider Capability Assertion or the Skill Bundle digest | The Worker exits non-zero with no Attempt and no GitHub write | M6 | §3, §18 | #71 |
| P-WRK-4 | The Worker is idle; an Attempt ends | When idle, it sleeps for the poll interval. After an Attempt, it selects again immediately | M6 | §3 | #71 |
| L3-ERR-3 | 403 with `x-ratelimit-remaining > 0` | Permission refusal; no retry; Attempt `failed`, non-transient; the Worker continues | M6 | §13 | v1 |
| L3-ERR-4 | 403/429 with `x-ratelimit-remaining: 0` | Primary rate limit; wait to `x-ratelimit-reset`; retry in place, bounded at 3 attempts / 5 minutes; the wait counts against the wall clock | M6 | §13 | v1 |
| L3-ERR-5 | Secondary rate limit **with** `Retry-After` | Honoured | M6 | §13 | v1 |
| L3-ERR-6 | Secondary rate limit **without** `Retry-After` | Exponential backoff, same bound | M6 | §13 | v1 |
| L3-ERR-8 | Restart after a Credential Failure | Preflight fails; the Worker refuses to start | M6 | §3, §18 | v1 |
| C-ERR-2 | A 401 from any GitHub API call, with the `GET /user` identity probe returning 401, succeeding, failing another way, or erroring on the network | Probe also 401 → `CredentialFailure`: the Worker halts non-zero with no write (P-WRK-1). Probe 200, any other status, or a network error → infrastructure-class `github`: the Attempt ends `failed` / `infrastructure-failure` and the Worker continues | M6 | §13 | #81 |
| L3-ERR-9 | A push touching `.github/workflows/` | `failed`, non-transient, comment naming the cause; classified by **write kind and target path**, never by error string | M3 | §13 | v1 |
| L3-ERR-10 | A base branch requiring signed commits | Push rejected; same permanent class | M3 | §18 | v1 |
| L3-ERR-10a | A `gho_` or `ghp_` token is supplied | Startup refuses; no Attempt is opened | met (pre-MVP) | §18 | v1 |
| L3-ERR-10b | A token whose `GET /user` response carries `X-OAuth-Scopes` | Startup refuses | met (pre-MVP) | §18 | v1 |
| L3-ERR-10c | A token below the configured remaining-lifetime threshold | Startup refuses | withdrawn | §18 | v1 |
| L3-ERR-10d | Startup on a valid fine-grained token | `GET /user` id resolved, `permissions.push` true, rate-limit headroom checked; **no write is performed** | met (pre-MVP) | §18 | v1 |
| L3-ERR-11 | Every registered secret, across both log streams and tool arguments and results | Replaced by `«redacted:<name>»`; the environment is never logged; git remote URLs carry no token | M8 | §18 | v1 |
| L3-ERR-12 | Child processes started by the shell tool | No secret present in their environment | M8 | §18 | v1 |
| L3-ERR-13 | Volume free space below the threshold at Attempt start | The Attempt does not start; SQLite never meets a full disk | M8 | §17 | v1 |

---

## L4: Real smoke

One combination, run manually before a release: **Python × Anthropic**, on the sandbox repository, in the image, through `agent run` with the `implement` Workflow of §2.1.

| Id | Scenario | Expected | Owed by | Contract | Source |
| --- | --- | --- | --- | --- | --- |
| L4-1 | A real issue carrying `ready-for-agent`, complete profile, clear acceptance criteria | Claimed, implemented, validated, reviewed, delivered as an open pull request with an issue comment; `delivered` | M6 | §3–§15 | v1 |
| L4-3 | The recorded cost of L4-1 | Written down. It is the first honest input to the per-Attempt cost ceiling the map still holds as a tuning constant | M6 | §11 | v1 |

PHP and TypeScript are **not** given a paid end-to-end run. What differs between languages is the toolchain, the profile and the test runner, and L2 exercises all three inside the image against real repositories. A second and third paid run would re-test the parts that are identical.

---

## Rows by slice

| Slice | Rows owed |
| --- | --- |
| **M1** Walking skeleton | P-CTX-1; P-RDY-1…4; G-HDR-1; C-GRF-1, C-SKL-1, C-SKL-2, C-WSP-1, C-RDY-1…4; L1-1, L1-6; L2-1, L2-2, L2-12, L2-13, L2-14, L2-19; L3-SEL-11, L3-CLR-19, L3-CLR-20; L3-IMP-1, 5, 9, 10, 11, 13, 15, 20; L3-ERR-2 |
| **M2** Step contract | W-STP-1, 2, 4, 5; G-STP-1; C-STP-1, C-ERR-1, C-IMP-1, C-CTX-1, C-FP-1…5, C-FP-7, C-TRN-1; L3-IMP-4, 12, 16, 17, 18, 21, 23, 24, 25, 26, 28, 29, 30, 31 |
| **M3** Review | P-DEL-3, 4, 5; G-REV-1, 2; W-STP-6; C-REV-1; L3-REV-1, 2; L3-DEL-19, 20; L3-ERR-9, 10 |
| **M4** Tail | P-DEL-2, 6, 7, 8, 9; G-TDD-1; C-PUB-1, 2; C-FP-6; L2-3, L2-17, L2-18; L3-REV-10; L3-DEL-5 |
| **M5** Workflow Configuration | W-CFG-1…13; W-PIN-1, 2; W-STP-7; P-CMD-2; C-CFG-1…3; L1-3, L1-4, L1-4a |
| **M6** GitHub adapter | P-SEL-1…5; P-CLM-1, 2; P-DEL-1, 10; P-WRK-1…4; C-SEL-1, C-OUT-1, C-TRK-1, C-ERR-2; L3-DEL-14, 21; L3-ERR-3…6, 8; L4-1, L4-3 |
| **M7** Gates, limits, cancellation and SIGTERM | P-CAN-1, 2, 3; P-LIM-1; W-STP-3; G-REV-3; P-CMD-1; C-LIM-1; L3-IMP-7; L3-ERR-14; REC-SIG-1, 2 |
| **M8** Continuation and recovery | P-HND-1…4; P-REC-1…4; P-CLM-3; W-PIN-3, 4; W-CFG-14; every other REC-* row (all except REC-SIG-1, 2, owed by M7); L3-DEL-15; L3-ERR-11, 12, 13 |
| `met (pre-MVP)` | L1-5; L2-8, L2-9, L2-20; L3-ERR-10a…d |

The P-, W-, G- and REC-* placements are those of [What is the migration cut and module layout for the LangGraph runtime?](https://github.com/lbacik/coding-agent/issues/80#issuecomment-6544199163). A v1 row is owed by the first slice that rewrites or retires a module it exercises. A v1 row that is not green today and sits over a module no slice names is owed by the slice that first builds its code path.

---

## Coverage

### Contract sections

| Section | Rows |
| --- | --- |
| §1 Layers and ownership | C-GRF-1; L3-IMP-9, L3-SEL-11 |
| §2 Workflow Configuration | W-CFG-1…13; W-PIN-1…4; W-STP-7; C-CFG-1…3; P-CMD-2; L1-3 |
| §3 The Worker's lifecycle | P-WRK-1…4; W-CFG-13, 14; REC-CFG-2; REC-WRK-1; L1-4; L2-19; L3-ERR-8 |
| §4 Selection | P-SEL-1…5; C-SEL-1; REC-CLM-3 |
| §5 Claim | P-CLM-1…3; REC-CLM-1…3; C-SEL-1; L1-6 |
| §6 The Attempt graph | C-GRF-1; G-STP-1 |
| §7 Readiness, the Project Profile and the baseline | P-RDY-1…4; P-CMD-2; C-RDY-1…4; C-WSP-1; L2-1, 2, 8, 9, 12, 13, 14, 17, 18, 20; L3-CLR-20; L3-IMP-11, 26; REC-WRK-2 |
| §8 Workflow Steps | W-STP-1, 2, 4, 5, 7; G-STP-1; G-HDR-1; P-CTX-1; C-SKL-1, 2; C-STP-1; C-ERR-1; L1-1, L1-5; L3-IMP-1, 15, 24, 31 |
| §9 Declaring completion | C-IMP-1; C-FP-1…5, C-FP-7; G-TDD-1; P-DEL-3, 9; L3-IMP-10, 20, 24, 25, 26, 28, 29, 30; L2-2 |
| §10 Review | P-DEL-3, 4, 5; G-REV-1, 2, 3; W-STP-6; C-REV-1; L3-REV-1, 2 |
| §11 Limits, cancellation and SIGTERM | P-LIM-1; P-CAN-1…3; W-STP-3, 4, 6; G-REV-3; C-LIM-1; C-CTX-1; P-CMD-1; REC-SIG-1, 2; L1-3; L3-IMP-4, 5, 7, 12, 13, 16, 17, 18, 21, 23, 30, 31; L3-ERR-14 |
| §12 Delivery Snapshot, final checks and the Publication Gate | C-PUB-1, 2; C-FP-1, 2, 4, 5, 7; P-DEL-2, 6, 7, 8; P-CAN-2; L2-1, 3, 8; L3-DEL-5; REC-INT-6; REC-SIG-2 |
| §13 External writes | L3-DEL-14, 15, 19, 21; L3-ERR-3…6, 9; C-TRK-1; C-ERR-2; P-WRK-1; P-REC-2, 4; REC-INT-3, 4; REC-DEL-1…6 |
| §14 Terminal outcomes | C-OUT-1; P-DEL-1, 2, 6, 8, 9, 10; P-RDY-1…4; P-LIM-1; P-CAN-1; C-RDY-3; C-ERR-1; L3-DEL-14; L3-ERR-2; L3-IMP-10, 21; REC-OUT-1, 2 |
| §15 Pull request and comment contents | P-DEL-1, 5; G-TDD-1; C-FP-6; W-PIN-2; C-REV-1; C-CTX-1; P-LIM-1; L2-17, 18; L3-DEL-5, 21; L3-REV-10; REC-INT-5; REC-WRK-3 |
| §16 Continuation | P-HND-1…4; W-PIN-4; REC-WRK-3 |
| §17 Recovery | P-REC-1…4; P-CLM-3; C-TRN-1; W-PIN-3; every REC-* row; L3-DEL-15; L3-ERR-13 |
| §18 Identity and authorization | P-WRK-3; L1-4, L1-4a; L3-SEL-11; L3-CLR-19; L3-DEL-19, 20, 21; L3-ERR-8, 10, 10a…d, 11, 12; REC-DEL-6 |
| §19 Deferred | none: deferred items are proved by no row. Their v1 rows are annotated `deferred` in the v1 matrix |

### ADRs

| ADR | Rows |
| --- | --- |
| [0001](../adr/0001-pinned-toolchain-matrix.md) Pinned toolchain matrix | L2-12, L2-20 |
| [0002](../adr/0002-commit-and-push-before-review.md) Commit and push before review | G-REV-3; C-PUB-1; REC-INT-4, 6 |
| [0003](../adr/0003-delivery-intent.md) Delivery Intent | P-CAN-2; REC-SIG-2; P-REC-2; REC-DEL-1…4 |
| [0004](../adr/0004-failure-identifiers-as-evidence.md) Failure identifiers as evidence | L2-1; P-RDY-4 |
| [0005](../adr/0005-agent-identity-is-the-repository-owner.md) Agent Identity is the repository owner | L3-DEL-20; L3-ERR-10a…d; REC-DEL-6 |
| [0006](../adr/0006-l2-fixtures-vendored-in-repo.md) L2 fixtures vendored in repo | every L2 row |
| [0007](../adr/0007-upstream-skill-text-is-answered-never-rewritten.md) Upstream skill text is answered, never rewritten | G-HDR-1; L2-19; L3-IMP-15; L3-REV-2; G-REV-2 |
| [0008](../adr/0008-the-tdd-seam-gate-is-a-readiness-fact.md) The TDD seam gate is a Readiness Fact | P-RDY-2; L3-CLR-20; L3-IMP-11; G-TDD-1 |
| [0009](../adr/0009-provider-capability-is-established-by-attempting.md) Provider capability is established by attempting | L1-4, L1-4a; P-WRK-3 |
| [0010](../adr/0010-the-assistant-turn-is-carried-back-verbatim.md) The assistant turn is carried back verbatim | L1-1; L3-IMP-4 |
| [0011](../adr/0011-the-pinned-prefix-is-a-region-not-a-rule.md) The Pinned Prefix is a region, not a rule | C-SKL-2; L3-IMP-13 |
| [0012](../adr/0012-companion-files-are-injected-or-refused-never-fetched.md) Companion Files are injected or refused, never fetched | C-SKL-2; L2-19; L3-IMP-15 |
| [0013](../adr/0013-context-window-handoff.md) Context-Window Handoff | L3-IMP-4, 12, 13, 21, 23, 31; C-CTX-1; W-STP-4, 5; P-HND-1 |
| [0014](../adr/0014-completion-is-declared-and-substantiated-never-inferred.md) Completion is declared and substantiated, never inferred | L3-IMP-10, 20, 24, 25, 26, 28, 29, 30, 31; C-IMP-1; C-STP-1; P-RDY-1. The pilot regression is L3-IMP-24 |
| [0015](../adr/0015-the-claim-is-an-assignment-of-the-agent-identity.md) The Claim is an assignment of the Agent Identity | P-CLM-1…3; P-SEL-2; REC-CLM-1…3 |
| [0016](../adr/0016-a-workflow-fills-only-the-implementation-stage.md) A Workflow fills only the implementation stage | C-GRF-1; W-STP-7; W-CFG-8 |
| [0017](../adr/0017-recovery-finishes-a-recorded-outcome-from-the-run-ledger.md) Recovery finishes a recorded outcome from the Run Ledger | C-GRF-1; P-REC-1, 3; REC-OUT-1, 2; REC-CFG-2; REC-INT-1 |
| [0018](../adr/0018-runtime-owned-validation-authenticates-the-invocation-not-the-code.md) Runtime-owned validation authenticates the invocation, not the code | C-FP-1…7; C-TRN-1 |

### Differences from the reference (contract §20)

**Reference defects not copied**

| Reference behaviour | Rows |
| --- | --- |
| Recovery can open a pull request for partial work | P-REC-3; REC-INT-6 |
| Recovery re-decides the outcome, while its marker deduplication keeps the original comment | REC-OUT-1, 2 |
| Recovery forces the local branch to the remote and discards unpushed commits | P-REC-4; REC-INT-1, 3 |
| A crash between claim and checkpoint leaves the issue assigned forever | P-CLM-3; REC-CLM-1, 2 |
| A sealed but unpushed commit after an interrupted final check | C-PUB-1; REC-INT-6 |
| The profile is bootstrapped from a hard-coded `main` | P-CMD-2 |
| Truncated, unfiltered comment lookups; open PRs matched by head name only | REC-DEL-5, 6; L3-DEL-21 |
| A generic crash message and a generic PR summary | P-DEL-1; P-RDY-1; G-TDD-1; W-PIN-2 |
| Dead `REVIEW_BLOCKING_SEVERITIES` | P-DEL-5 |
| A command timeout kills `sh` but not its process group | P-CMD-1; REC-SIG-1 |
| Note-only "handoff" commits turn an empty attempt into a handoff | P-HND-4; REC-WRK-3 |
| The error guard exits before finalization, causing a restart loop | P-WRK-2; REC-WRK-1 |
| A dirty tree at recovery holds until an operator acts | REC-INT-1 |
| SIGTERM re-signals and dies immediately | P-CAN-3; REC-SIG-1, 2 |
| Its own earlier push can be misread as foreign | REC-INT-4 |
| Recovery applies the current profile and environment | W-PIN-3; REC-CFG-1 |
| `ready-for-agent` + `ready-for-human` is claimed by the code | P-SEL-1 |
| Reviewer briefs are model-written | G-REV-2; L3-REV-2 |

**Signals and rules added beyond the reference**

| MVP rule | Rows |
| --- | --- |
| Selection exclusions and order | P-SEL-1…5; C-SEL-1 |
| Claim with no comment; only our own assignment removed | P-CLM-1, 2 |
| Trusted context is title and body | P-CTX-1 |
| Readiness refusals | P-RDY-1…3; C-RDY-1, 2; L2-12…14 |
| A red baseline is refused | P-RDY-4 |
| `report_completion`, runtime-owned evidence and five Implementation Outcomes | L3-IMP-24…26, 28…30; C-IMP-1; P-DEL-9 |
| At least one completed review, at most three, status disclosed | P-DEL-3, 4, 5; G-REV-1; C-REV-1 |
| Slash references answered in the Attempt Header | G-HDR-1 |
| Red → green disclosed, not enforced | G-TDD-1 |
| Drift ends `failed` / `drift` | P-DEL-6 |
| `ready-for-human` on every outcome without a PR | C-OUT-1; P-DEL-2; P-RDY-1 |
| Terminal comment contents | P-RDY-1; P-LIM-1; W-PIN-2 |
| PR body contents | P-DEL-1; G-TDD-1; W-PIN-2 |
| Cancellation from the issue | P-CAN-1, 2; W-STP-3 |
| Wall clock, cost, tokens and tool calls; no turn limit | P-LIM-1; C-CFG-3 |
| Continuation on a new branch per Attempt with the runtime's snapshot | P-HND-1…4 |
| A strict, versioned Workflow Configuration with pinning | W-CFG-1…13; W-PIN-1…4 |
| No instruction channel from the Target Project | W-STP-7 |
| A closed tool set | C-SKL-1; L3-IMP-9; L3-SEL-11 |
| Startup refusals | P-WRK-3; W-CFG-13; L1-4; L3-ERR-10a…d |
| Halt after three infrastructure-class failures; Credential Failure halts at once | P-WRK-1, 2 |

### Differences from v1 (contract §21)

**Retained**

| v1 guarantee | Rows |
| --- | --- |
| Run Ledger authority over lifecycle history | C-GRF-1; REC-OUT-1, 2 |
| The Attempt Marker, read in one direction | L3-DEL-21; REC-DEL-6 |
| Completion declared and substantiated; runtime-owned Criterion Evidence | L3-IMP-24…26, 28…31; C-IMP-1 |
| Delivery Intent as the point of no return | P-CAN-2; REC-SIG-2 |
| Commit and push before validation and review | G-REV-3; C-PUB-1 |
| Project Profile `schema: 2`; Validation Contract pinned at the Base Revision | L2-1, 8, 9, 13, 17 |
| Seam Set as a Readiness Fact | P-RDY-2; L3-CLR-20; L3-IMP-11 |
| Forced `activate_implement` and per-skill activation tools | C-SKL-1, 2; L3-IMP-1 |
| Context-Window Handoff; the Pinned Prefix as a region | L3-IMP-4, 12, 13, 21, 23, 31 |
| Verbatim assistant turns; Companion Files injected or refused | L1-1; C-SKL-2; L3-IMP-15 |
| Attempt-wide meters, per-response usage, per-bucket prices, progress Gates and the verification reserve | L1-3; L3-IMP-7, 16, 17, 18; C-LIM-1 |
| Rate-limit classification and bounded in-place retry | L3-ERR-3…6 |
| Agent Identity, token-kind refusals, preflight split, Provider Capability Assertion | L3-ERR-10a…d; L1-4, L1-4a; P-WRK-3 |
| Branch and commit trailer forms | L3-DEL-19, 20 |
| Credential Failure halts the Worker | P-WRK-1; L3-ERR-8 |
| Four of the five Terminal Outcomes and their reasons | C-OUT-1; P-DEL-9 |

**Superseded**

| v1 rule | Rows |
| --- | --- |
| §1: `thread_id` checkpoint | C-GRF-1; REC-INT-1 |
| §1, §4: a fixed `implement` node | W-STP-1…7; C-STP-1 |
| §2: ascending issue number; exclusions | P-SEL-3, 4, 5; C-SEL-1 |
| §2, §3 T16: explanatory comment | P-SEL-1 |
| §3, §10: `agent-running`; no assignment | P-CLM-1, 2; REC-CLM-3 |
| §3 T13, T14, T14a | P-REC-1…4; REC-INT-*, REC-DEL-* |
| §4 T8: unconditional blocking `review_fanout` | P-DEL-3, 4, 5 |
| #62: a review counts once invoked | G-REV-1; C-REV-1 |
| §5: outcome after validation; force-accept | C-IMP-1; C-PUB-1 |
| §5: concrete ceilings as tuning constants | C-CFG-3; P-LIM-1 |
| §5: SIGTERM unspecified | P-CAN-3; REC-SIG-1, 2 |
| §6: idempotency key and lookups | REC-INT-3, 4; REC-DEL-1…6 |
| §6: Automatic Retry Budget | C-ERR-1 |
| §7: prose fallback; clarification for missing facts | C-RDY-1, 2; P-RDY-1…3 |
| §7, §8: Baseline Failure excused; remediation | P-RDY-4; C-PUB-1; P-DEL-2 |
| §11: five Terminal Outcomes | P-DEL-10; P-DEL-9 |
| §11: terminal writes | C-OUT-1; P-DEL-1 |
| §12: PR body | P-DEL-1; G-TDD-1; W-PIN-2 |
| §12: a short issue comment | P-RDY-1; P-LIM-1 |
| ADR 0013 within an Attempt only | P-HND-1, 4; REC-WRK-3 |
| Model, prices and thresholds hard-coded | W-CFG-10; L1-3; C-CFG-3 |
