# v1 runtime contract

**Status: current.** This document is the single authority on how the Worker behaves in v1. It supersedes, on every point where they differ, the resolution comments of [#4](https://github.com/lbacik/coding-agent/issues/4), [#5](https://github.com/lbacik/coding-agent/issues/5), [#6](https://github.com/lbacik/coding-agent/issues/6), [#7](https://github.com/lbacik/coding-agent/issues/7), [#9](https://github.com/lbacik/coding-agent/issues/9) and [#10](https://github.com/lbacik/coding-agent/issues/10).

Those tickets remain the record of *why* each rule exists, and they are worth reading for the arguments. They are no longer the record of *what the rule is*: they were written in sequence, each correcting its predecessors, and reconstructing the current contract from them requires reading them in the right order and noticing every correction. §13 lists what this document changed relative to them.

Vocabulary is fixed in [`CONTEXT.md`](../../CONTEXT.md) and is used here without redefinition.

## 1. Layers and ownership

| Layer | Owns | May write to GitHub |
| --- | --- | --- |
| **Supervisor** (plain Python, no graph) | Exclusive volume lock; polling; label algebra; candidate ordering; Fingerprint and Base Revision computation; opening the Attempt and Lease records; invoking the Attempt Graph; startup reconciliation | **No — read-only** |
| **Attempt Graph** (one thread, `thread_id = attempt_id`) | Everything from Claim to a Terminal Outcome, including every external write | Yes, only in dedicated write nodes |
| **Model** (inside work nodes) | Reasoning, workspace file edits, repository-declared commands, local commits | **No — never** |

The model's toolset contains no GitHub mutation of any kind: no label tool, no comment tool, no pull request tool. This is what makes "the agent never applies the Selection Label" structural rather than a matter of prompt compliance.

## 2. Selection and label algebra

Candidates are open issues in the one configured Target Repository carrying the Selection Label (default `ready-for-agent`), taken in **ascending issue number**.

Exclusions: `wontfix`; `ready-for-human`; any `wayfinder:*` label; **any assignee at all**; an open delivery pull request linked to the issue and bearing an Attempt Marker.

Precedence at sweep time:

1. `wontfix`, or `ready-for-human` alone → not eligible, no write.
2. Selection Label **+** `needs-info` → resume. The agent removes `needs-info` itself at Claim.
3. Selection Label **+** `ready-for-human` → not eligible; exactly one explanatory comment per label-application event.
4. `needs-info` without the Selection Label → waiting.
5. No Selection Label → invisible.

`agent-running` is agent-owned, for human visibility only, and is never an eligibility gate — so a label stranded by a crash cannot block later work.

## 3. Transition table

Durable state is the Run Ledger record for the issue. **The Worker never assigns or unassigns anyone** (§10): `agent-running` is the human-visible claim and the Run Ledger is the authoritative one.

| # | From → To | Trigger | Guard | Labels | Durable state after |
| --- | --- | --- | --- | --- | --- |
| T1 | `idle` → `claimed` | Sweep picks the lowest-numbered candidate | §2 exclusions pass; worker holds the volume lock; no live Lease | −`needs-info`, +`agent-running` | Attempt `#i/n` opened with Fingerprint and Base Revision; Lease opened |
| T2 | `claimed` → `running` | Readiness evaluation completes | All Readiness Facts resolved; toolchain satisfiable; declared services reachable | — | `running` |
| T3 | `claimed`/`running` → `awaiting-clarification` | Missing Readiness Fact or blocking ambiguity | Clarification Rounds used < 3 | −Selection Label, −`agent-running`, +`needs-info` | rounds += 1; Question Set digest stored; Lease released |
| T4 | `awaiting-clarification` → `claimed` | Someone with an authorising role re-applies the Selection Label | T1 guards; `ready-for-human` absent | −`needs-info`, +`agent-running` | Attempt `#i/n+1`, fresh Fingerprint and Base Revision; **Automatic Retry Budget reset; Clarification Rounds carried over unchanged** |
| T5 | `awaiting-clarification` → `awaiting-clarification` | At Claim: no Qualifying Answer, or only part of the Question Set resolved | Rounds used < 3 | −Selection Label, −`agent-running`, +`needs-info` | rounds += 1; new Question Set digest covering only the unresolved questions, linking the prior one |
| T6 | `awaiting-clarification` → `failed` | As T5, with rounds used = 3 | — | −Selection Label, −`agent-running`, +`ready-for-human` | `failed` / `clarification-exhausted` |
| T7 | `running` → `abandoned` | At a Gate before the Delivery Intent: issue closed, or Selection Label removed | Unconditional stop | −`agent-running` | `abandoned`; branch pushed, no pull request |
| T8 | `running` → `delivered` | Publication Gate satisfied | Fingerprint unchanged; validation clean; no blocking findings | −Selection Label, −`agent-running` | `delivered` + PR reference; PR open, not draft |
| T9 | `running` → `delivered-as-draft` | Publication Gate not satisfied but work exists | Reason is one of `drift`, `outstanding-findings`, `failing-validation` | −Selection Label, −`agent-running`, +`ready-for-human` | `delivered-as-draft` + reason + PR reference; draft PR |
| T10 | `running` → `claimed` | Execution failure classified transient | Automatic Retry Budget not exhausted | — | Attempt `#i/n+1`; budget decremented |
| T11 | `running` → `failed` | Execution failure | Non-transient, or budget exhausted | −Selection Label, −`agent-running`, +`ready-for-human` | `failed` + classification + log pointer |
| T12 | `running` → `failed-limit` | Wall clock, cost/token or tool-call ceiling crossed, seen at a Gate **or inside a work node** | — | −Selection Label, −`agent-running`, +`ready-for-human` | `failed-limit` + which limit |
| T13 | `running` → `claimed` | Lease expiry after worker restart | Resume counter for this Attempt = 0 | — | Same Attempt resumed; resume counter = 1. **If a Delivery Intent exists, the resume re-enters the delivery sequence, never implementation** |
| T14 | `running` → `failed` | Lease expiry after worker restart | Resume counter = 1 **and no Delivery Intent exists** | −Selection Label, −`agent-running`, +`ready-for-human` | `failed` / `repeated-process-loss` |
| T14a | `running` → the outcome the Intent names | Lease expiry after worker restart | Resume counter = 1 **and a Delivery Intent exists** | Per T8 / T9 | The **delivery-completion path** runs: remaining writes only, no model, no tool loop |
| T15 | any terminal → `claimed` | A human removes the stop label and re-applies the Selection Label | T1 guards | −`needs-info` if present, +`agent-running` | Attempt `#i/n+1`; **both** Clarification Rounds and Automatic Retry Budget reset, and the reset recorded |
| T16 | `idle` → `idle` | Sweep sees the Selection Label together with `ready-for-human` | — | — | Contradiction noted; exactly one explanatory comment per label-application event |

**The Worker never applies the Selection Label to any issue, ever.** Only a human does. This makes an endless loop structurally impossible, independently of whether any counter is correct.

**The two budgets are separate and protect against different things.** The Automatic Retry Budget (2 per issue) absorbs transient infrastructure failure and resets whenever a human hands the issue back. The Clarification Round budget (3 per issue) protects a human from an agent that keeps asking, so it does **not** reset on T4 — otherwise the threshold is unreachable, because a round cannot begin without the Selection Label being re-applied in the first place. T15 is the only reset, and it exists: T6 applies `ready-for-human`, which a human must lift explicitly.

## 4. Node inventory

Write nodes are marked **(W)**. Every one of them is preceded by a Gate, performs exactly one external write, and obeys §6.

```
Supervisor ──▶ [ Attempt Graph, thread_id = attempt_id ]

START
  └─▶ claim_labels (W)          +agent-running; −needs-info   (no assignment write, §10)
  └─▶ load_context              fresh checkout from the mirror; fast-forward to the pushed
                                branch head if one exists; read the Project Profile at the
                                Base Revision (the Validation Contract); load Carried
                                Decisions valid at this Fingerprint
  └─▶ evaluate_readiness        resolve Readiness Facts, the Seam Set among them; assert the
                                toolchain against the Supported Toolchain Matrix; probe
                                declared services
        ├─ facts missing ──────▶ publish_clarification (W)
        │                        └─▶ clarification_labels (W)
        │                            └─▶ END: awaiting-clarification
        ├─ environment unsatisfiable ─▶ comment_failure (W) …  failed / unsupported-environment
        └─ facts resolved ─────▶ confirm_seam   assertion only, no model: refuses to open
                                   │            implement without a Seam Set in state
                                   └─▶ implement

  └─▶ implement                 /implement + nested /tdd + tdd's Companion Files, bounded model
                                tool loop, opened with an Attempt Header carrying the Seam Set
  └─▶ commit_snapshot           Delivery Snapshot committed locally on agent/<issue>/<n>-<slug>
  └─▶ push_snapshot (W)         the Attempt's durable anchor is established here
  └─▶ validate                  harness runs the Validation Contract's commands, outside
                                the model's tool loop
        ├─ regression ─────────▶ remediate (exactly once) ─▶ commit_snapshot ─▶ …
        └─ clean ──────────────▶ review_fanout ──┬──▶ review_standards ──┐
                                                 └──▶ review_spec ───────┴──▶ review_join
              ├─ blocking findings ─▶ remediate (exactly once) ─▶ commit_snapshot ─▶ …
              └─────────────────────▶ PUBLICATION GATE
                                        └─▶ write_delivery_intent   ← point of no return
                                            └─▶ push_final (W)
                                                └─▶ open_pull_request (W)
                                                    └─▶ comment_delivery (W)
                                                        └─▶ delivery_labels (W)
                                                            └─▶ END: delivered
                                                                   | delivered-as-draft

Stop path — reachable from any Gate before write_delivery_intent:
  cooperative_stop ─▶ push_branch (W) ─▶ comment_stop (W) ─▶ stop_labels (W)
                     └─▶ END: abandoned                       (branch pushed, no pull request)

Failure path — any classified failure, or a limit:
  comment_failure (W) ─▶ failure_labels (W) ─▶ END: failed | failed-limit

Delivery-completion path — T14a, and any resume that finds a Delivery Intent:
  write_delivery_intent (already present) ─▶ push_final (W) ─▶ … ─▶ delivery_labels (W)
  No model, no tool loop, no re-implementation.
```

Ordering notes that are load-bearing rather than stylistic:

- **The clarification comment is written before the labels change**, so a human never sees `needs-info` without the questions that explain it.
- **Commit and push precede validation and review** ([ADR 0002](../adr/0002-commit-and-push-before-review.md)). `/code-review` resolves a nonempty `git diff <base>...HEAD`, so an uncommitted candidate reviews as though it did not exist; and the workspace is reconstructible, so only a pushed commit is a durable anchor. The loss bound this buys: **one process loss destroys at most one phase of an Attempt** — implementation, or validation-and-review — never the whole Attempt.
- **The seam gate is resolved before the implementer's conversation opens, and never inside it** ([ADR 0008](../adr/0008-the-tdd-seam-gate-is-a-readiness-fact.md)). `evaluate_readiness` resolves the Seam Set as a Readiness Fact, so a Target Issue naming no interface under test routes to clarification by T3 like any other missing fact, in the same Question Set as the rest. `confirm_seam` performs no model call and takes no branch a correct run can reach: it asserts the Seam Set is in state, and its refusal is an internal invariant violation, ending the Attempt `failed`, non-transient, with an explanatory comment. This is why the transition table needs no row of its own for the seam.
- **Validation precedes the review fan-out**, so a red suite does not first buy two parallel reviewer conversations.
- **The two reviewers run in separate conversations** with their own message lists and read-only toolsets, as the upstream contract requires. Each is given its role's own instructions, not the whole `/code-review` skill — handing a reviewer the full skill also hands it the instruction to spawn another pair.
- **There is no merge node.** The never-merge rule is enforced by that absence, not by the permission set, which does permit merging.

## 5. Gates and limits

A **Gate** sits between nodes and before every external write. At a Gate the Worker re-reads GitHub state, re-evaluates the Attempt's limits, and may stop cooperatively.

Limits are properties of the **Attempt**, not of the process:

- **Wall clock** measured from the Attempt's opening in the Run Ledger, never from process start. A restart does not grant a fresh budget.
- **Cost and tokens** accumulated from `usage_metadata`.
- **Tool-call count.**

**Usage is counted after every model response and flushed to the Run Ledger at that moment**, not only at Gates. A work node aborts its own tool loop the instant a ceiling is crossed rather than running to the next Gate. Without this, a long tool loop or a re-executed node spends the budget and then forgets it did. A SQLite write costs orders of magnitude less than the model call it is recording.

**Cost is computed from a configured Price Table, per token bucket.** `usage_metadata` reports token counts and never money, and no provider exposes a rate through any API, so the rates are a declared configuration input and a **Pinned Model** without an entry refuses to start ([ADR 0009](../adr/0009-provider-capability-is-established-by-attempting.md)). The buckets are not priced alike and cannot be collapsed: `input_tokens` is the whole prompt **including cache hits** rather than the uncached remainder, and a cache write may report in a provider-specific field while the normalised one stays zero. Pricing input and output alone is wrong in both directions.

**Compaction is eviction, and the Pinned Prefix is a region eviction cannot address** ([ADR 0011](../adr/0011-the-pinned-prefix-is-a-region-not-a-rule.md)). A work node's conversation is two parts: a **Pinned Prefix** — the Attempt Header, the injected skill files with the **Companion Files** that accompany them, and the Target Issue — and a history of **Exchange Units**, each an assistant turn with every tool result answering it. When the threshold is crossed, whole Exchange Units are dropped from the oldest end. Nothing is summarised, nothing is rebuilt, and no unit is split: [ADR 0010](../adr/0010-the-assistant-turn-is-carried-back-verbatim.md) forbids rewriting a turn, a summariser rewrites many at once, and a tool result parted from its call is a malformed request. The prefix is held out by construction rather than by rule, because losing it is **silent** — a provider accepts the request without it and answers normally, leaving `tdd`'s seam instruction with no referent and no trace. Where the Pinned Prefix alone crosses the threshold the Worker **refuses to open the Attempt** rather than dropping part of it, as §10 refuses a Pinned Model without a price: nothing is claimed yet, so nothing is stranded.

**An oversized tool result is capped to head, tail and a pointer**, and the pointer names the stored artifact and its full size so the model can ask for a range of it. The pointer is what makes the reading happen: given one, both pins asked for slices unprompted and repeatedly, the first within two turns.

**A Companion File is injected whole or it is unavailable, and no tool fetches one** ([ADR 0012](../adr/0012-companion-files-are-injected-or-refused-never-fetched.md)). A link inside a skill the model read once is not an offer it takes: no arm followed `tdd`'s companion links by any route, while a pointer sitting in the tool result being read was used unprompted in every run. There is no event at which a companion becomes wanted, so there is nothing to hang an on-demand offer on, and the on-demand route is removed rather than kept unused — a tool that resolves an arbitrary path under a skill's directory would make the refusal of a companion a rule something must keep obeying instead of a region nothing can reach. Which companions are injected is a property of the image, fixed at build time and asserted there against a manifest of names and content digests, because the choice was made by reading the files and a pin bump must not retire that reading in silence.

Concrete ceilings — 60-minute wall clock, cost, tokens, tool calls, the per-tool-result cap, the compaction threshold — are tuning constants set against observed runs, not part of this contract. **A token ceiling is one set per Pinned Model, not one global set**: providers disagree on the token count of an identical prompt, so a single constant means two different budgets.

## 6. External writes

**Every external write is its own node, and no node performs two.** Each is preceded by an intent record in the Run Ledger under the deterministic idempotency key `(attempt_id, write_kind, payload_digest)`. On re-entry a node checks for the receipt, then verifies remotely, and becomes a no-op when the write already landed.

| Write kind | Remote verification |
| --- | --- |
| Issue comment | The machine-readable marker `attempt=#i/n qset=h1` |
| Labels | Naturally idempotent; read back |
| Branch push | Remote ref at the expected sha |
| Pull request | An open PR from this branch bearing this Attempt's Marker |

Receipt authority is the **Run Ledger**, not the framework checkpoint, so losing the checkpoint cannot resurrect a write that already happened. Remote verification is the second line — and the only line after a Run Ledger volume loss.

**The absence of our effect and a human's reversal of our effect are different states, and only the first authorises re-writing.** A closed pull request from this branch is a withdrawal, not an absence: it routes to the cooperative stop path with an explanatory comment and `abandoned`, and is never reopened.

### The Delivery Intent

At the Publication Gate, before any delivery write, the Worker records a **Delivery Intent**: attempt id, branch, head sha, the gate's verdict, draft or ready, and the writes the verdict calls for.

It settles three things that were previously ambiguous:

1. A restart inside the delivery sequence resumes **that sequence**, from the Intent. It never returns to implementation, and it never opens a second pull request.
2. A second process loss (T14) with an Intent present does not fail the Attempt. The **delivery-completion path** runs the remaining writes and reaches the outcome the Intent names. Repeating the implementation would be both wasteful and blocked — selection excludes an issue with an open agent-authored delivery PR, so a fresh Attempt could not finish the job.
3. **Writing the Intent is the point of no return.** A stop signal arriving before it diverts the Attempt to the stop path; after it, the sequence completes. An open pull request with no explanatory comment and stale labels is worse for the human than a delivery they no longer wanted, and closing an unwanted PR costs one click.

### Rate limits

The single exception to "a failed write ends the Attempt". Classification is by **status, header value and documented error signal** — never by the mere presence of `x-ratelimit-*` headers, which are present on ordinary responses too:

| Observation | Classification | Action |
| --- | --- | --- |
| 401, confirmed by the identity probe | Credential Failure | Attempt `failed` / `credential-failure`; **the Worker halts** |
| 403 with `x-ratelimit-remaining > 0` | Permission refusal | Attempt `failed`, non-transient, explanatory comment; the Worker continues |
| 403/429 with `x-ratelimit-remaining: 0` | Primary rate limit | Wait until `x-ratelimit-reset`, retry in place |
| 403/429 carrying the documented secondary-limit signal | Secondary rate limit | Honour `Retry-After`; exponential backoff when it is absent |

In-node retry is bounded at **3 attempts and 5 minutes**, and the waiting counts against the Attempt's wall clock — there is no way to buy time by waiting. Beyond the bound the failure is transient and hands over to the Automatic Retry Budget.

## 7. Readiness, the Project Profile and the Validation Contract

The canonical source is **`docs/agents/project-profile.yml` in the Target Repository**, consulted first, with the prose fallback chain behind it (`AGENTS.md`/`CLAUDE.md` → `docs/agents/*` → project configuration → issue body). **Where both exist and disagree, the profile wins.**

```yaml
schema: 2
language: python              # python | php | typescript
working_directory: .
toolchain:
  python: "3.13"
  package_manager: uv
commands:
  bootstrap: "uv sync --frozen"
  test_all: "uv run pytest -q --junit-xml={evidence_dir}/test_all.xml"
  test_targeted: "uv run pytest -q --junit-xml={evidence_dir}/test_targeted.xml {path}"
evidence:
  format: junit-xml
  test_all: "{evidence_dir}/test_all.xml"
  test_targeted: "{evidence_dir}/test_targeted.xml"
checks:
  - name: types
    command: "uv run mypy src tests"
services: []
```

- `{path}` is substituted with a repository-relative test path; `{evidence_dir}` with a per-Attempt directory the harness creates and owns.
- **`evidence` is required for the test commands.** `junit-xml` is the only format in v1, because pytest, PHPUnit and vitest all emit it natively, so one parser serves three languages. It is a Readiness Fact: a profile without it routes to clarification, and the human fixes the gap permanently by committing the profile rather than answering the same question a third time.
- `evidence` is **optional for `checks`**. Without it a check that is red at the Delivery Snapshot cannot establish a Baseline Failure, so a red baseline no longer excuses it and the Attempt delivers as a draft.
- **`checks` is either a list of named commands or the literal scalar `none`.** An absent key is a missing Readiness Fact; `none` is the explicit declaration §3 of [#4](https://github.com/lbacik/coding-agent/issues/4) demanded; an empty list is rejected as ambiguous.
- **`services`** declares `{name, url_env}`. The deployment provides them; the agent only verifies reachability.
- **One working area per profile.** A repository with a PHP backend and a TypeScript frontend declares the one an Attempt may be judged against; the image carries all three toolchains regardless, so the neighbouring component still builds. Validation and the Publication Gate cover the declared area only, and the pull request says so. A `components` list is the documented upgrade path.
- Acceptance criteria are deliberately not a profile field: they belong to the Target Issue. **The Seam Set is excluded for the same reason and one more.** A seam is a property of the task — the boundary for "add a CLI flag" and for "fix a parser bug" differ inside one repository — so a project-level declaration would be either too coarse to authorise anything or wrong for most issues. And a profile field would be pinned into the Validation Contract at the Base Revision, which would leave an Attempt whose own task is moving a testing boundary unable to move it. A repository that genuinely documents a standing testing boundary is already served without a new mechanism: that is a **standard**, and §3 of `code-review` is what reads standards documented in the repository.

**The Validation Contract is the profile as it stands at the Base Revision, pinned at Claim.** An Attempt that edits the profile changes the contract for the next Attempt and never for its own; where the Target Issue *is* the profile change, the pull request states that it was validated with the base commands. Without this rule a candidate can go green by weakening the command that checks it.

### Two failure routes, and the line between them

| Condition | Route | Labels |
| --- | --- | --- |
| Profile absent or incomplete, or a Readiness Fact unresolvable from any source | **T3 clarification** | −Selection Label, −`agent-running`, +`needs-info` |
| Profile complete and correct, but the image cannot satisfy it | **`failed` / `unsupported-environment`** | −Selection Label, −`agent-running`, +`ready-for-human` |

A Question Set asks a human for **information**. Where the information is already correct and the **deployment** is wrong, no comment can answer, and spending Clarification Rounds on it would fail the issue for the wrong reason. Unsupported Environment covers a toolchain version outside the Supported Toolchain Matrix, an unknown `schema` version, and an unreachable declared service.

## 8. Validation Evidence and Baseline Failures

`validate` is a harness node, run **outside the model's tool loop**. Its output is Validation Evidence in the Run Ledger: the commands, their exit codes, how many tests actually executed, and **the identifier of every individual failure**.

- **Exit zero is necessary, not sufficient.** A suite collecting zero tests exits zero and looks like success. A run with no executed tests is missing evidence and fails validation.
- **The unit of a Baseline Failure is the individual failure, not the command.** A command is excused only when the set of failure identifiers at the Delivery Snapshot is a **subset** of the set at the Base Revision. Anything new in the set is a regression and blocks. Excusing a whole command lets a new broken test hide behind an old one.
- **Baseline evaluation stays lazy**: it runs only for commands that failed against the Delivery Snapshot, and only those commands, at the Base Revision.
- **No identifiers, no claim.** Where a command cannot name its individual failures, the Attempt cannot show the absence of a regression, and the safe outcome is `delivered-as-draft` / `failing-validation`.
- `checks: none` **satisfies** the check rather than skipping it.

**"Verified" means Validation Evidence produced by the harness.** The model's account of a test run is never evidence.

## 9. Clarification

- **One clarification comment per Attempt**, carrying every open question, numbered, with a machine-readable marker holding the attempt id and the Question Set digest.
- Posting it removes the Selection Label and `agent-running` and applies `needs-info`. The Lease is released. `needs-info` is what says the agent is still holding the issue; assignment is not used for this or for anything else.
- **Authorisation is the label; content is the comment.** Neither half suffices alone.
- **Qualifying Answer**, evaluated in this order, and the order is the point:
  1. Reject if the content carries an **Attempt Marker**. **Self-exclusion comes first**, because the Agent Identity is the repository owner's own account (§10) and the Worker would otherwise read its own Question Set as the answer to itself. Author id cannot do this job when the account is shared; the Marker can, and the two structural abstentions in §10 are what make it sound.
  2. Reject if created before the matching Question Set comment (Stale Answer). An edit to the issue body is equally a valid answer, and is always a human's: the Worker never edits the Target Issue's body.
  3. Reject unless the author's **effective repository role** is `admin`, `maintain`, `write` or `triage`, read from `GET /repos/{owner}/{repo}/collaborators/{username}/permission` and cached for the Attempt.

  Step 3 replaces `author_association`, which describes a relationship to the repository rather than a current permission and so admits authors without write access. The accepted role set is exactly the set that can apply the Selection Label: **whoever may authorise may answer.** A narrower set would let a triage user hand the issue back and then be ignored when they explain it.
- **A missing Seam Set is one question among the others**, not a round of its own. It rides the Attempt's single comment with the rest, because a Target Issue vague enough to name no interface under test is frequently the same one missing another fact, and asking in two rounds would spend two thirds of the budget on what one comment can carry. The question **asks** for the seam and proposes no candidates: proposing them needs a model call in a path deliberately kept model-free ([ADR 0008](../adr/0008-the-tdd-seam-gate-is-a-readiness-fact.md)).
- **Partial or absent answers** produce a new comment listing only the still-unresolved questions, with a new digest linking the previous one.
- **Budget: 3 Clarification Rounds per Target Issue**, carried across re-authorisation (§3, T4).

## 10. Identity and authorization

**In v1 the Agent Identity is the Target Repository owner's own GitHub account**, authenticating with a **fine-grained personal access token owned by that account and scoped to the single Target Repository**, with the Workflows permission withheld. The account is recognised by its **numeric account id**, resolved once at startup by `GET /user`; logins are renameable, ids are not. That id serves the commit identity and nothing else — it is not an identity test.

This is the one arrangement the platform documents as supported without a gap: the token owner writing to a repository the token owner owns. A dedicated machine account invited as a collaborator, which [#9](https://github.com/lbacik/coding-agent/issues/9) §1 selected, is a documented gap for fine-grained tokens and is deferred past v1 — see [ADR 0005](../adr/0005-agent-identity-is-the-repository-owner.md) for the trade-off and [#11](https://github.com/lbacik/coding-agent/issues/11) for the arrangement v2 has to settle. The cost of it is paid in the four rules below marked **shared-account**.

- **The Attempt Marker, not the account, is what tells the Worker's writing apart from a human's** (*shared-account*). Every artifact the Worker writes carries one in that artifact's own idiom: the machine-readable line in an issue comment, the same line in a pull request body, the `Attempt: #<issue>/<n>` trailer on a commit, the `agent/` prefix on a branch. **The rule reads in one direction only: no Marker means not ours.** A Marker does not prove authorship, because the only account that could forge one belongs to the only human authorised to answer — which is exactly why the forgery has no victim in v1. This **supersedes** the earlier rule that markers answer "was this a previous Attempt of mine" and never "is this me": under a shared account the Marker is forced to answer both, and the two abstentions below are what make that sound.
- **Two structural abstentions make "a human wrote this" decidable without an identity** (*shared-account*): the Worker **never edits the Target Issue's body**, and the Worker **never applies the Selection Label**. So an issue-body edit is always a human's answer, and the presence of the Selection Label is always a human's authorisation — neither conclusion needs to know who wrote it.
- **No assignment, ever** (*shared-account*). The Worker never assigns or unassigns anyone. Under a shared account, assigning the Agent Identity is indistinguishable from the owner assigning themselves, and unassigning at a Terminal Outcome would strip the owner's own assignment. `agent-running` is the human-visible claim; the Run Ledger and the Lease are the authoritative ones, as they already were. §2 therefore excludes an issue with **any** assignee: narrower than before, decidable, and it still protects what the old exclusion protected — an issue a human has taken.
- **The Worker shares the owner's API rate limit** (*shared-account*) with that human's own tooling. Startup therefore checks for headroom rather than assuming the budget is the Worker's alone.
- **Write scope stops short of Workflows.** A delivery touching `.github/workflows/` is rejected by the platform and ends the Attempt `failed`, non-transient, classified by **write kind and target path** rather than by error text. An agent that can rewrite the workflows verifying it can disable its own verification. The refusal is undocumented — GitHub states only which permission *grants* the ability, never what happens without it — so the Worker refuses the write itself and the platform rejection is the second line, proved by S0 rather than assumed.
- **One identity for API calls and for git.** Author and committer use the id-based noreply address `<id>+<login>@users.noreply.github.com`. Every commit carries the trailer `Attempt: #<issue>/<n>`, the same join key as the comment marker, the artifacts directory and the log file. In v1 this means every commit the Worker makes is attributed to the owner in git history, permanently and unrewritably; the trailer is the only durable record that a machine wrote it.
- **Commits are unsigned in v1.** A base branch requiring signatures rejects the push as a non-transient failure.
- **No `Co-authored-by`** for a human who answered a Question Set. A Carried Decision is consent, not authorship.
- **One branch per Attempt**, `agent/<issue>/<n>-<slug>`. Force-push is therefore never needed and the capability is never held. The agent never deletes a branch.
- **Credentials pass through one choke point**: loaded into a redaction registry at startup, filtered out of both log streams and out of tool arguments and results, never present in the environment of child processes, and used for git through a credential helper rather than a remote URL.

### Preflight

No endpoint enumerates a fine-grained token's permissions, so capability is established by attempting the write, not by reading a grant. Attempting every write at every start would litter the Target Repository, so the two concerns are split by how often they need to run.

**`agent preflight` — an operator command, run against a sandbox repository, not at startup.** It performs every write kind the contract needs and asserts the one it must *not* have: an issue comment; a label added and removed; a branch pushed; a pull request opened and closed; and a push touching `.github/workflows/` **rejected**. This is the whole of S0's proof, and it is the only place the Workflows boundary is ever demonstrated rather than assumed.

**Worker startup — non-mutating only.** `GET /user` for the numeric id and login; `GET /repos/{owner}/{repo}` for `permissions.push == true`; rate-limit headroom. A capability the token turns out to lack is not caught here: it surfaces as a permission refusal when it bites, which §6 already classifies as a non-transient failure of that Attempt alone.

**Worker startup also asserts the model, by calling it.** The **Provider Capability Assertion** establishes, in one small live call, that the Pinned Model can call a tool, honours the pinned effort level, and reports non-zero usage; it asserts besides that a Price Table entry exists for the pin. It is a call rather than a lookup for the same reason the write probes above are attempts rather than a reading of grants — and because **tool calling, the capability every work node depends on, is declared by neither provider**. Where a provider does publish a capability declaration it is read afterwards as a second sieve, never as the first. A failed assertion **exits the process nonzero with no Attempt opened**: unlike a capability the token turns out to lack, this is a property of the deployment rather than of any Target Issue, and it would fail every issue in the queue identically. See [ADR 0009](../adr/0009-provider-capability-is-established-by-attempting.md); the Pinned Model includes its endpoint, and the assistant turn it returns is carried back verbatim ([ADR 0010](../adr/0010-the-assistant-turn-is-carried-back-verbatim.md)).

Rate-limit headroom, by contrast, is checked for GitHub only. The provider surfaces no usable headroom signal through the model abstraction: a successful response carries the model, the stop reason and usage, and nothing from the wire.

**Startup also refuses the wrong kind of token**, which turns the Workflows exclusion from a rule into a check:

- the credential must carry the fine-grained token prefix `github_pat_`; a classic (`ghp_`) or OAuth (`gho_`) token is refused;
- the `GET /user` response must carry **no** `X-OAuth-Scopes` header, whose presence means a scoped token was supplied and therefore one whose `workflow` scope cannot be excluded from outside;
- where the response carries `github-authentication-token-expiration`, a token below the configured remaining-lifetime threshold is refused.

All three rest on observed API behaviour rather than on a documented guarantee, so **S0 confirms them empirically** before anything depends on them. The threshold is a tuning constant, not part of this contract.

## 11. Terminal outcomes

Five, and no more. `unsupported-environment`, `credential-failure`, `no-change-produced`, `clarification-exhausted` and `repeated-process-loss` are **classifications of `failed`**, not outcomes of their own.

| Outcome | Meaning | Artifacts |
| --- | --- | --- |
| `delivered` | Every gate passed | Open pull request with `Closes #<issue>`, issue comment |
| `delivered-as-draft` | Work exists, a gate did not pass; reason `drift`, `outstanding-findings` or `failing-validation` | Draft pull request stating the reason on its face, **without** `Closes`, issue comment, `ready-for-human` |
| `abandoned` | Cooperative stop before the Delivery Intent | Branch pushed, no pull request, explanatory comment |
| `failed` | Classified failure | Explanatory comment, `ready-for-human`; branch if one was pushed |
| `failed-limit` | A ceiling was crossed | Explanatory comment naming the limit, `ready-for-human` |

The agent never merges, never converts a Delivery Draft to ready, never deletes a branch and never force-pushes. The last three are capabilities it does not have.

**A Credential Failure halts the Worker** rather than releasing it to the next issue: a broken credential is not a property of the issue, and a Worker left running would consume the whole queue, failing each issue identically and explaining none of them, because it cannot write.

## 12. Pull request and comment contents

**Pull request body**, in order: `attempt_id`, Fingerprint and Base Revision sha; scope of the change; Validation Evidence — commands, results, executed test counts, and every excluded Baseline Failure named individually with its identifier; review summary, counts and worst finding per axis, reports separate; Carried Decisions used and assumptions made; limitations and outstanding findings; a Drift or base-movement note where either applies; the declared working area when the repository holds more than one; and `Closes #<issue>` **only for a clean `delivered`**.

**Both review reports go in the body in full, inside `<details>` blocks.** Separate comments would be two more external writes, each opening a window in which the pull request exists without its evidence. Over the size limit a report is truncated with an explicit marker and a pointer to the log — never silently.

**Issue comment**: short. A link to the pull request, the `attempt_id`, one line on the outcome.

**Base branch movement**: the agent neither rebases nor merges. It compares the base head with the Base Revision at the Publication Gate and, when they differ, says so, naming the commit the work was implemented and verified against. Rebasing would invalidate every piece of evidence collected.

## 13. What this document changed

Relative to the ticket resolutions. Changes 1–9 were settled in [#8](https://github.com/lbacik/coding-agent/issues/8); 10–14 in [#10](https://github.com/lbacik/coding-agent/issues/10), which replaced the credential arrangement and, with it, the whole basis on which the Worker recognises its own writing:

| # | Change | Supersedes |
| --- | --- | --- |
| 1 | Clarification Rounds are **not** reset by T4; only T15 resets them. The two budgets are separate. | [#4](https://github.com/lbacik/coding-agent/issues/4) §8, T4 |
| 2 | A Baseline Failure is an **individual named failure**, not a whole command; the subset test replaces the exit-code test. No identifiers means no claim of no regression. | [#7](https://github.com/lbacik/coding-agent/issues/7) §3, invariant 8 |
| 3 | The Project Profile moves to `schema: 2` and **must declare how test commands report individual results** (`junit-xml`). | [#6](https://github.com/lbacik/coding-agent/issues/6) §2 |
| 4 | The **Validation Contract** is pinned at the Base Revision; a candidate cannot weaken the commands that judge it. | New; [#6](https://github.com/lbacik/coding-agent/issues/6) and [#7](https://github.com/lbacik/coding-agent/issues/7) left it unstated |
| 5 | Qualifying Answer uses the author's **effective repository role** (`admin`/`maintain`/`write`/`triage`), not `author_association`. | [#4](https://github.com/lbacik/coding-agent/issues/4) §6, [#9](https://github.com/lbacik/coding-agent/issues/9) §3 step 3 |
| 6 | Rate limits are classified by **status, header value and documented signal**, not by header presence. | [#9](https://github.com/lbacik/coding-agent/issues/9) §6 |
| 7 | Usage is counted after **every model response** and work nodes abort their own tool loop on a ceiling; T12 is reachable inside a node. | [#5](https://github.com/lbacik/coding-agent/issues/5) §7 |
| 8 | The **Delivery Intent** makes the publication sequence resumable as itself, adds T14a, and fixes the point of no return at the Intent rather than vaguely at the Publication Gate. | [#5](https://github.com/lbacik/coding-agent/issues/5) §9, [#7](https://github.com/lbacik/coding-agent/issues/7) §8, [#9](https://github.com/lbacik/coding-agent/issues/9) scenario E |
| 9 | **One working area per Attempt**, validation scoped to it and disclosed. | [#6](https://github.com/lbacik/coding-agent/issues/6) §1 vs. its own schema |
| 10 | The Agent Identity is the **Target Repository owner's own account** with a fine-grained token scoped to that one repository, not a dedicated machine account invited as a collaborator. The machine account is deferred to [#11](https://github.com/lbacik/coding-agent/issues/11). | [#9](https://github.com/lbacik/coding-agent/issues/9) §1 |
| 11 | The **Attempt Marker** is what distinguishes the Worker's writing from a human's, in one direction (no Marker means not ours). Markers now do answer "is this me", which the earlier rule forbade, and two structural abstentions carry the soundness. | [#9](https://github.com/lbacik/coding-agent/issues/9) §1, §10 of this document as it stood |
| 12 | Qualifying Answer step 1 rejects on the **presence of an Attempt Marker**, not on `author.id == agent_identity_id`, which under a shared account would reject the only human able to answer. | [#4](https://github.com/lbacik/coding-agent/issues/4) §6, [#9](https://github.com/lbacik/coding-agent/issues/9) §3 step 1 |
| 13 | **Assignment is removed from the contract entirely**: no assign, no unassign, no `Assignment` column in §3, and §2 excludes an issue with **any** assignee. | [#4](https://github.com/lbacik/coding-agent/issues/4) §3, [#9](https://github.com/lbacik/coding-agent/issues/9) |
| 14 | **Preflight is split** into an operator command that performs every write kind on a sandbox and non-mutating startup checks, and startup **refuses a token of the wrong kind** — the check that makes the Workflows exclusion enforceable rather than merely intended. | New; [#9](https://github.com/lbacik/coding-agent/issues/9) left preflight unspecified |
| 15 | **Provider capability is established by attempting, not by declaration**, in one live call at Worker startup; the Required Capability list is tool calling, the pinned effort level, non-zero usage reporting and a Price Table entry; refusal is a nonzero exit with no Attempt opened. | New; [#24](https://github.com/lbacik/coding-agent/issues/24), [ADR 0009](../adr/0009-provider-capability-is-established-by-attempting.md) |
| 16 | The **Pinned Model includes its endpoint**, and the **assistant turn is carried back verbatim** — never rebuilt from the text and tool calls the loop can see. Anything that rewrites history, compaction included, inherits this. | New; [#24](https://github.com/lbacik/coding-agent/issues/24), [ADR 0010](../adr/0010-the-assistant-turn-is-carried-back-verbatim.md) |
| 17 | **Cost comes from a configured Price Table, priced per bucket**, and a token ceiling is one set per Pinned Model rather than one global set. | New; [#24](https://github.com/lbacik/coding-agent/issues/24) |
| 18 | **Compaction evicts whole Exchange Units and never summarises**, and the **Pinned Prefix** is held out of it structurally rather than by rule. An oversized Pinned Prefix refuses to open the Attempt. | New; [#25](https://github.com/lbacik/coding-agent/issues/25), [ADR 0011](../adr/0011-the-pinned-prefix-is-a-region-not-a-rule.md) |
| 19 | A capped tool result's **pointer names the artifact and its full size**. `L3-IMP-5`'s slice reading is a real behaviour, not a vacuous row: both pins used it unprompted. | New; [#25](https://github.com/lbacik/coding-agent/issues/25) |
| 20 | **Companion Files are injected whole or unavailable**, decided per file by reading it and asserted at build time by digest. `read_skill_resource` **no longer exists** and `L3-IMP-2` is retired with it; `L3-IMP-3`'s activation rule is unaffected. | [#5](https://github.com/lbacik/coding-agent/issues/5) §5, [#7](https://github.com/lbacik/coding-agent/issues/7); [#28](https://github.com/lbacik/coding-agent/issues/28), [ADR 0012](../adr/0012-companion-files-are-injected-or-refused-never-fetched.md) |

Carried forward from corrections the tickets already made to each other, and stated here once so nobody has to find them: `Checkpoint` is **Gate**; `delivered-with-drift` is **`delivered-as-draft`** with a reason. The `unassign` write node that earlier resolutions placed at the end of every terminal sequence **no longer exists** (§10, correction 13).
