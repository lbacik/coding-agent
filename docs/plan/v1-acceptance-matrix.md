# v1 acceptance matrix

What must be true before v1 is finished, and how each thing is shown to be true. Behaviour is defined by [the v1 runtime contract](../contract/v1-runtime-contract.md); the slices are in [the v1 implementation plan](./v1-implementation-plan.md).

## Four layers, and why they are separate

Separating them is what keeps the paid runs down to the few that actually buy information: a scenario about a crash between two write nodes learns nothing from being run against a real model, and a scenario about `usage_metadata` learns nothing from being run against a real GitHub.

| Layer | What it exercises | Fixtures | Cost | When it runs |
| --- | --- | --- | --- | --- |
| **L1 — Provider contract** | The model provider's own contract: tool calling, structured output, `usage_metadata`, context, preflight refusal | Real provider API, smallest possible prompts | small, real | Explicitly, marked; never in the default run |
| **L2 — Language and profile** | Bootstrap, test and check commands per language; evidence parsing; baseline comparison | Three real repositories, inside the image | none | Default run, in the image |
| **L3 — Behaviour scenarios** | The transition table, the write machinery, failure and recovery | Fake GitHub adapter, scripted model | none | Default run |
| **L4 — Real smoke** | One complete `issue → pull request`, Python × Anthropic | Sandbox repository, real provider | real | Manual, before a release |

**The fake GitHub is a liability unless it is held to the real one.** Both adapters sit behind the same `GitHubClient` port and both run the same contract suite; the real side of that suite runs against the S0 sandbox repository. Without it, L3 tests our idea of GitHub rather than GitHub.

**Record/replay is used for neither.** Against GitHub it cannot produce the sequences that matter — a death between two writes, a human closing a pull request. Against the provider the cassettes go stale on the first prompt change and then test history.

---

## L1 — Provider contract

Runs for **both** `openai:*` and `anthropic:*`. Owed by S3.

| Id | Scenario | Expected |
| --- | --- | --- |
| L1-1 | Bind the implementer's toolset and ask for a tool call | The call arrives with a well-formed argument object |
| L1-2 | Request structured output for the classifier role | Parses to the declared schema |
| L1-3 | Read `usage_metadata` from a response | Tokens present and non-zero; cost derivable |
| L1-4 | Preflight against a model lacking a required capability | The Worker refuses to start; no Attempt is opened |
| L1-5 | Provider returns overloaded | Retried against the **same** pinned model with backoff; no model swap at any point |
| L1-6 | A small real implementation task, per provider | Completes; the pinned model is recorded in the Run Ledger beside the Fingerprint |

---

## L2 — Language and profile

Runs inside the image against three real repositories, one per language. Owed by S1 and S2.

| Id | Scenario | Expected |
| --- | --- | --- |
| L2-1 | Bootstrap, `test_all`, `test_targeted` and `checks` per language (Python/uv/pytest, PHP/composer/PHPUnit, TypeScript/pnpm/vitest) | All run; Validation Evidence records commands, exit codes, executed counts and individual failure identifiers |
| L2-2 | `test_targeted` with `{path}` | The named test file runs, and only it |
| L2-3 | A suite collecting **zero** tests | Failed validation — missing evidence, not a pass, despite exit code zero |
| L2-4 | Base fails test A; candidate fails A **and** B | **Regression.** Blocks. The command is not excused |
| L2-5 | Base fails test A; candidate fails A only | Baseline Failure. Does not block. A is named individually in the pull request body |
| L2-6 | A command that cannot name its individual failures, red at both base and candidate | No Baseline Failure can be established; `delivered-as-draft` / `failing-validation` |
| L2-7 | Baseline evaluation is lazy | The base is re-run only for commands that failed at the Delivery Snapshot, and only those |
| L2-8 | `checks: none` | Satisfied, not skipped |
| L2-9 | `checks: []` | Rejected as ambiguous |
| L2-10 | `checks` key absent | Missing Readiness Fact → clarification |
| L2-11 | `evidence` block absent for a test command | Missing Readiness Fact → clarification |
| L2-12 | `toolchain` version outside the Supported Toolchain Matrix | `failed` / `unsupported-environment`; the comment names the declared requirement beside the image's matrix |
| L2-13 | Unknown `schema` version | `failed` / `unsupported-environment` — **not** clarification |
| L2-14 | A declared service is unreachable | `failed` / `unsupported-environment`, before implementation begins |
| L2-15 | Profile file and prose documentation disagree | The profile wins |
| L2-16 | No profile, prose documentation complete | Readiness resolves from the fallback chain |
| L2-17 | The candidate edits `project-profile.yml` during the Attempt | Validation still uses the Validation Contract pinned at the Base Revision; the pull request says so |
| L2-18 | A repository with two applications, profile declaring one working area | Validation covers the declared area only; the pull request states which |
| L2-19 | Skill Bundle verification, each of the six failure modes in S1, **plus two more from ADR 0007**: a Role Block anchor absent or occurring more than once, and a Role Block digest mismatch | The **build** fails, naming what was wrong |
| L2-20 | `UV_PYTHON_DOWNLOADS=never` | uv does not fetch an interpreter; an unavailable version is an Unsupported Environment, not a download |

---

## L3 — Behaviour scenarios

Fake GitHub adapter and scripted model. This is the bulk of the suite.

### Selection and labels — owed by S6, S7

| Id | Scenario | Expected |
| --- | --- | --- |
| L3-SEL-1 | Two eligible issues | The lower number is claimed first (T1) |
| L3-SEL-2 | Selection Label + `needs-info` | Resume; the agent removes `needs-info` itself at Claim |
| L3-SEL-3 | Selection Label + `ready-for-human` | Not eligible; **exactly one** explanatory comment per label-application event (T16) |
| L3-SEL-4 | `needs-info` without the Selection Label | Nothing happens |
| L3-SEL-5 | `wontfix`, or any `wayfinder:*` label | Not eligible; no write |
| L3-SEL-6 | The issue has **any** assignee | Not eligible; the Worker never assigns or unassigns anyone |
| L3-SEL-7 | An open delivery pull request bearing an Attempt Marker is linked | Not eligible |
| L3-SEL-8 | A stranded `agent-running` from a crashed process | Does **not** block eligibility |
| L3-SEL-9 | **Duplicate polling** — a sweep runs while a live Lease holds the issue | Nothing happens; no second Attempt |
| L3-SEL-10 | A second sweep after the Lease expired, resume counter 0 | The same Attempt resumes (T13), not a new one |
| L3-SEL-11 | The agent attempts to apply the Selection Label | Structurally impossible: no such tool exists in the model's toolset and no node performs it |

### Readiness and clarification — owed by S6

| Id | Scenario | Expected |
| --- | --- | --- |
| L3-CLR-1 | **Clear task**, everything documented | Straight to implementation (T2), no comment |
| L3-CLR-2 | **Missing documentation** — no test command anywhere | One comment, only the unresolved facts, listing the sources searched; `needs-info`; **no automatic retry** (T3) |
| L3-CLR-3 | **Clarification and explicit resume** | A comment alone does not resume; only re-application of the Selection Label does (T4) |
| L3-CLR-4 | **Incomplete answer** — 2 of 3 questions answered | New comment carrying question 3 alone, new digest linking the previous one; rounds += 1 (T5) |
| L3-CLR-5 | Three incomplete answers and three re-authorisations | Reaches `ready-for-human` at rounds = 3 (T6). **Does not return to round 1** |
| L3-CLR-6 | After T6, a human re-applies the Selection Label without removing `ready-for-human` | Not eligible; one explanatory comment (T16) |
| L3-CLR-7 | After T6, a human removes `ready-for-human` and re-applies the Selection Label | Claimed; **both** budgets reset, and the reset recorded (T15) |
| L3-CLR-8 | A transient failure retry during `awaiting-clarification` | Automatic Retry Budget reset by T4; **Clarification Rounds unchanged** |
| L3-CLR-9 | The agent's own Question Set comment is in the list | Rejected on the **Attempt Marker**, at step 1, before the role check that would otherwise admit it — and it must be, because the author id is the answering human's own |
| L3-CLR-10 | An answer from a `read`-role user | Ignored; the next comment says so |
| L3-CLR-11 | An answer from a `triage`-role user | **Accepted** — whoever may authorise may answer |
| L3-CLR-12 | An answer predating the Question Set | Stale Answer; ignored |
| L3-CLR-13 | The issue body is edited instead of a comment being posted | A valid answer; the Fingerprint is re-read at Claim |
| L3-CLR-14 | A comment from a bot author | Rejected; `author_association` is never the primary test |
| L3-CLR-18 | A human comment carrying no Attempt Marker, from the account that is also the Agent Identity | **Accepted** — the Marker's absence is what admits it; an id test would reject the only human able to answer |
| L3-CLR-19 | The Worker is asked to edit the Target Issue's body | Structurally impossible: no node and no model tool performs it, which is what makes L3-CLR-13 a human's answer |
| L3-CLR-15 | An unrelated comment arrives mid-Attempt | Not read during the Attempt |
| L3-CLR-16 | No valid Carried Decision for the TDD seam | Routes to clarification, not to a guess |
| L3-CLR-17 | A Carried Decision recorded under a Fingerprint that has since changed | Not used; a fresh gate is required |

### Implementation and budgets — owed by S3, S4

| Id | Scenario | Expected |
| --- | --- | --- |
| L3-IMP-1 | `/implement` and nested `/tdd` activate | Both present in the activation registry; neither activates twice |
| L3-IMP-2 | A companion file is requested | Served through `read_skill_resource`, resolved relative to the skill's own directory |
| L3-IMP-3 | `codebase-design` | Activatable only nested from `tdd`, never by model-driven discovery |
| L3-IMP-4 | Context fills | Oldest turns compacted; **skill instructions and the Attempt header never compacted or summarised** |
| L3-IMP-5 | An oversized tool result | Head + tail + a pointer to the artifact; the model can read a chosen slice |
| L3-IMP-6 | The token ceiling is crossed mid tool loop | The loop stops **inside the node**; T12 `failed-limit`; usage already flushed |
| L3-IMP-7 | Wall clock crossed during review | T12, measured from the Ledger's Attempt start; a restart does not reset it (R6) |
| L3-IMP-8 | A node re-executes after a restart | Usage is not double-counted, because it was flushed per model response |
| L3-IMP-9 | The model attempts a GitHub mutation | No such tool exists |
| L3-IMP-10 | **No diff** — `/implement` completes with a tree identical to the Base Revision | `failed` / `no-change-produced`; no branch, no pull request; `ready-for-human`. Not a Clarification Round |

### Review and the Publication Gate — owed by S4

| Id | Scenario | Expected |
| --- | --- | --- |
| L3-REV-1 | The fan-out runs | Exactly two reviewer conversations, separate message lists, read-only toolsets |
| L3-REV-2 | A reviewer receives its instructions | Its own role's, not the whole `/code-review` skill; **no reviewer spawns another pair**. The Role Block is **byte-identical** to that slice of the installed `SKILL.md`; the Seat Assignment carries no review criteria (ADR 0007) |
| L3-REV-3 | Review never re-enters inside `implement` | Asserted structurally |
| L3-REV-4 | Both reviewers see the same evidence | Identical Base Revision and Delivery Snapshot pair |
| L3-REV-5 | Reports are joined | Kept separate, with counts and worst finding per axis |
| L3-REV-6 | A correctness or spec-mismatch finding | **Blocks** |
| L3-REV-7 | A style or taste finding | Does not block |
| L3-REV-8 | **Failed tests** — a regression at validation | Exactly one remediation round; a new Delivery Snapshot; validation and **both** reviews re-run against the whole `BASE...HEAD` diff |
| L3-REV-9 | A second unresolved round | No third; `delivered-as-draft` |
| L3-REV-10 | An oversized review report | Truncated with an explicit marker and a log pointer — never silently |

### Delivery and recovery — owed by S5

| Id | Scenario | Expected |
| --- | --- | --- |
| L3-DEL-1 | A clean delivery | `delivered`; open pull request with `Closes #<issue>`; issue comment; Selection Label and `agent-running` removed; **no assignment write of any kind** (T8) |
| L3-DEL-2 | Drift at the Publication Gate | `delivered-as-draft` / `drift`; both Fingerprints named; **no `Closes`**; `ready-for-human` (T9) |
| L3-DEL-3 | Blocking findings survive remediation | `delivered-as-draft` / `outstanding-findings`; the findings verbatim in the body |
| L3-DEL-4 | Validation still red after remediation | `delivered-as-draft` / `failing-validation`; failing commands with their baseline comparison |
| L3-DEL-5 | The base branch has moved | No rebase, no merge; disclosed in the body, naming the commit the work was verified against |
| L3-DEL-6 | **Partial pull request publication** — crash after `open_pull_request`, before the comment | Resume re-enters, finds the open PR from the branch bearing this Attempt's Marker, no-ops, then writes the comment and labels (R3) |
| L3-DEL-7 | Crash after the push, response lost | Receipt or remote ref-and-sha match; no-op |
| L3-DEL-8 | Crash after the clarification comment, before the label change | The comment is not duplicated; the round is not double-counted; labels are applied on resume (R1) |
| L3-DEL-9 | **Restart** mid-implementation | Fresh checkout, fast-forward to the pushed branch head, re-enter `implement`; at most one phase lost (R2) |
| L3-DEL-10 | A second process loss, **no** Delivery Intent | T14: `failed` / `repeated-process-loss` |
| L3-DEL-11 | A second process loss **with** a Delivery Intent | **T14a**: the delivery-completion path runs the remaining writes; no model, no re-implementation; the outcome the Intent names |
| L3-DEL-12 | A stop signal before the Delivery Intent is written | Diverts to the stop path: branch pushed, no pull request, one comment, `abandoned` (T7, R8) |
| L3-DEL-13 | A stop signal after the Delivery Intent is written | The sequence completes; the human closes the pull request |
| L3-DEL-14 | A human closed the pull request from this branch | **Withdrawal, not absence**: cooperative stop, comment naming it, `abandoned`. Never reopened |
| L3-DEL-15 | Every write kind, crash injected after the write and before its confirmation | No duplicate; the receipt or the remote check makes the re-entry a no-op |
| L3-DEL-16 | Run Ledger volume lost, prior agent comments present | Attempt and round counts rebuilt from the markers; remote verification carries the whole idempotency burden (R5) |
| L3-DEL-17 | Reconstruction meets comments bearing an unfamiliar Agent Identity id | The first comment discloses that numbering was rebuilt across an identity change |
| L3-DEL-21 | A pull request from an `agent/` branch carrying **no** Attempt Marker in its body | Not treated as ours; the one-directional rule is not read backwards |
| L3-DEL-18 | Reconstruction is impossible | Treated as fresh, and the first comment discloses it |
| L3-DEL-19 | One branch per Attempt | `agent/<issue>/<n>-<slug>`; no force-push capability is ever used or needed; no branch is ever deleted |
| L3-DEL-20 | Commits | Author and committer are the Agent Identity's id-based noreply address; the `Attempt: #<issue>/<n>` trailer is present; unsigned; not squashed. Under v1's shared account the trailer is the **only** provenance record |

### Identity, errors and the environment — owed by S5, S7, S8

| Id | Scenario | Expected |
| --- | --- | --- |
| L3-ERR-1 | **Model/API failure** — provider overloaded | Node `RetryPolicy` against the same pinned model, then the Automatic Retry Budget, then `failed`. No model swap (R7) |
| L3-ERR-2 | An unclassified exception | **Permanent**, not transient. `failed` |
| L3-ERR-3 | 403 with `x-ratelimit-remaining > 0` | Permission refusal; no retry; Attempt `failed`, non-transient; the Worker continues |
| L3-ERR-4 | 403/429 with `x-ratelimit-remaining: 0` | Primary rate limit; wait to `x-ratelimit-reset`; retry in place, bounded at 3 attempts / 5 minutes; the wait counts against the wall clock |
| L3-ERR-5 | Secondary rate limit **with** `Retry-After` | Honoured |
| L3-ERR-6 | Secondary rate limit **without** `Retry-After` | Exponential backoff, same bound |
| L3-ERR-7 | 401 mid-Attempt | Credential Failure: Run Ledger and log only, no comment possible, labels untouched, **the Worker halts** (exit non-zero) |
| L3-ERR-8 | Restart after a Credential Failure | Preflight fails; the Worker refuses to start |
| L3-ERR-10a | A `gho_` or `ghp_` token is supplied | Startup refuses; no Attempt is opened |
| L3-ERR-10b | A token whose `GET /user` response carries `X-OAuth-Scopes` | Startup refuses |
| L3-ERR-10c | A token below the configured remaining-lifetime threshold | Startup refuses |
| L3-ERR-10d | Startup on a valid fine-grained token | `GET /user` id resolved, `permissions.push` true, rate-limit headroom checked; **no write is performed** |
| L3-ERR-9 | A push touching `.github/workflows/` | `failed`, non-transient, comment naming the cause; classified by **write kind and target path**, never by error string |
| L3-ERR-10 | A base branch requiring signed commits | Push rejected; same permanent class |
| L3-ERR-11 | Every registered secret, across both log streams and tool arguments and results | Replaced by `«redacted:<name>»`; the environment is never logged; git remote URLs carry no token |
| L3-ERR-12 | Child processes started by the shell tool | No secret present in their environment |
| L3-ERR-13 | Volume free space below the threshold at Attempt start | The Attempt does not start; SQLite never meets a full disk |
| L3-ERR-14 | A hung repository command | Returns as a **tool result** the model can react to, via the shell tool's per-command timeout — not as a process death |

---

## L4 — Real smoke

One combination, run manually before a release: **Python × Anthropic**, on the sandbox repository, in the image, through the Supervisor. Owed by S7.

| Id | Scenario | Expected |
| --- | --- | --- |
| L4-1 | A real issue carrying `ready-for-agent`, complete profile, clear acceptance criteria | Claimed, implemented, validated, reviewed, delivered as an open pull request with an issue comment; `delivered` |
| L4-2 | A real issue with a missing Readiness Fact | One clarification comment; `needs-info`; the Worker moves on |
| L4-3 | The recorded cost of L4-1 | Written down. It is the first honest input to the per-Attempt cost ceiling the map still holds as a tuning constant |

PHP and TypeScript are **not** given a paid end-to-end run. What differs between languages is the toolchain, the profile and the test runner, and L2 exercises all three inside the image against real repositories. A second and third paid run would re-test the parts that are identical.

---

## Coverage

| Contract element | Rows |
| --- | --- |
| T1–T16, T14a | L3-SEL-1…11, L3-CLR-1…8, L3-DEL-1, 2, 10, 11, 12; L3-IMP-6, 7, 10 |
| R1–R8 | L3-DEL-8 (R1), 9 (R2), 6 (R3), 10 (R4), 16 (R5); L3-IMP-7 (R6); L3-ERR-1 (R7); L3-DEL-12 (R8) |
| Delivery failure and recovery table | L3-DEL-1…20, L3-ERR-3…10 |
| Corrections 1–14 of the runtime contract §13 | 1 → L3-CLR-5, 8; 2 → L2-4, 5, 6; 3 → L2-11; 4 → L2-17; 5 → L3-CLR-10, 11, 14; 6 → L3-ERR-3…6; 7 → L3-IMP-6, 8; 8 → L3-DEL-11, 13; 9 → L2-18; 10 → S0; 11 → L3-DEL-21, L3-SEL-7; 12 → L3-CLR-9, 18, 19; 13 → L3-SEL-6, L3-DEL-1, 6; 14 → L3-ERR-10a…d, S0 |
| The nine scenarios [#8](https://github.com/lbacik/coding-agent/issues/8) required | clear task → L3-CLR-1, L4-1; clarification and explicit resume → L3-CLR-3; missing documentation → L3-CLR-2; incomplete answer → L3-CLR-4; failed tests → L3-REV-8; model/API failure → L3-ERR-1; duplicate polling → L3-SEL-9; restart → L3-DEL-9; partial publication → L3-DEL-6, 11 |
