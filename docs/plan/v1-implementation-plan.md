# v1 implementation plan

The ordered route from an empty repository to a Worker that takes an issue and hands back a reviewed pull request. Behaviour is defined by [the v1 runtime contract](../contract/v1-runtime-contract.md); this document says only in what order it gets built and how each step proves itself.

Companion: [the v1 acceptance matrix](./v1-acceptance-matrix.md).

## How a slice is shaped

Each slice is **vertical and externally demonstrable**: it ends with something a human can run and watch, even where the layers beneath it are still missing. The alternative — building the Run Ledger, then the graph, then the write nodes, then the skills — produces nothing observable until the end and leaves the two largest unknowns, the `/implement` runtime contract and the `/code-review` adapter, until last.

Two rules hold across every slice:

- **A slice is done when its acceptance rows are green, not when its code is written.** The rows live in the acceptance matrix and each one names the slice that owes it.
- **A slice never fakes a layer it will later replace with a different shape.** `--issue <n>` is the input from S3 onward precisely so that S7 adds a selection loop above an unchanged interface, rather than replacing a task-file path that was only ever scaffolding.

## The slices

| # | Slice | Prerequisites | Model calls |
| --- | --- | --- | --- |
| S0 | Provisioning and credential proof | — | none |
| S1 | Image and Skill Bundle | — | none |
| S2 | Project Profile and harness validation | S1 | none |
| S3 | Skill execution on an explicit issue | S0, S2 | **yes, paid** |
| S4 | Review fan-out and Publication Gate | S3 | **yes, paid** |
| S5 | Delivery write sequence | S0, S4 | scripted, plus one paid smoke |
| S6 | Lifecycle and clarification | S5 | scripted |
| S7 | Supervisor, selection and recovery | S6 | scripted, plus the full paid run |
| S8 | Operational hardening and handoff | S7 | none |

S1 and S2 have no dependency on S0 and may be built alongside it. Everything from S3 onward waits on a credential that actually works.

---

### S0 — Provisioning and credential proof

**Scope.** Issue the Agent Identity's credential — a fine-grained token owned by the Target Repository owner, scoped to that one repository, Workflows withheld ([contract §10](../contract/v1-runtime-contract.md), [ADR 0005](../adr/0005-agent-identity-is-the-repository-owner.md)); create a sandbox Target Repository **owned by that same account**, so the sandbox reproduces the deployment arrangement rather than a friendlier one; write a runbook a second person can follow; write the `agent preflight` command that performs every write kind the contract needs, plus the startup checks.

**Why first.** Nothing about the credential is provable by reading. GitHub documents which permission *grants* a `.github/workflows/` write and never what happens without it — no status code, no message, not even that the push is rejected. The three startup token assertions rest on observed API behaviour rather than on a documented guarantee. Until one arrangement is demonstrated writing for real, every later slice rests on assumptions the documentation does not cover.

**Done when.** On the sandbox repository, under the issued credential: an issue comment posted, a label added and removed, a branch pushed, a pull request opened and closed — and a push touching `.github/workflows/` **rejected**, confirming the permission boundary is real rather than assumed. The three startup assertions are confirmed against the live API: a `gho_`/`ghp_` token is refused, the fine-grained token's `GET /user` response carries no `X-OAuth-Scopes` header, and `github-authentication-token-expiration` is present and readable. `permissions.push` reads `true`. No write probe touches assignment — the contract has none. The runbook has been followed end to end by someone who did not write it.

**Not in this slice.** Any agent logic. S0 is a runbook and a connectivity probe.

---

### S1 — Image and Skill Bundle

**Scope.** The Dockerfile: Python and uv for the agent, the three target toolchains at exactly one pinned version each, `UV_PYTHON_DOWNLOADS=never`, non-root `agent` uid 1000, read-only root filesystem apart from the volume and `/tmp`, the volume layout. Build-time installation of the Skill Bundle with `agent-installer@0.6.0` from a pinned commit SHA, installed from this repository's own lockfile on a pinned Node base. The three verification layers.

**Done when.** The build succeeds, publishes its Supported Toolchain Matrix, and **fails loudly** on each of: an allowlisted skill missing from `installed ∪ updated`; a non-empty `skipped` or `refused`; a `resolvedCommit` other than the pin; a managed artifact outside the allowlist; a missing companion file; a `/<skill>` reference in an installed `SKILL.md` that is neither allowlisted nor in the committed ignore list.

**Not in this slice.** Anything that runs inside the image beyond the verification itself.

---

### S2 — Project Profile and harness validation

**Scope.** The profile parser at `schema: 2`; the toolchain assertion against the Supported Toolchain Matrix; service reachability probing; the harness `validate` step; the JUnit XML evidence parser; the Baseline Failure subset comparison; Validation Evidence records.

**Why before the model.** S3 without S2 has nothing to judge its own output with, and the only available verdict would be the model's own account of a test run — the one thing the contract forbids calling verified. S2 is also cheap: no model calls at all, and it is where PHP and TypeScript are actually exercised.

**Done when.** Three real repositories, one per language, bootstrap and validate inside the image and produce Validation Evidence with executed test counts and individual failure identifiers. A suite that fails test A at the base and fails A **and** B at the candidate is reported as a **regression**, not a Baseline Failure. A run collecting zero tests is a failed validation. `checks: none` satisfies the check. An unknown `schema` and an out-of-matrix toolchain both route to `unsupported-environment`. A missing `evidence` block is reported as a missing Readiness Fact.

**Not in this slice.** Attempts, the Run Ledger, GitHub.

---

### S3 — Skill execution on an explicit issue

**Scope.** The first model. `agent implement --issue <n>` against the sandbox repository: mirror and workspace, Base Revision pin, Fingerprint, the Validation Contract read at the base, `/implement` with `/tdd` nested, the `read_skill_resource` tool, the explicit-activation registry, the bounded tool loop, the per-tool-result cap, the model/provider contract through `init_chat_model`, the preflight capability assertion, usage counted after every response. Ends with a Delivery Snapshot committed and pushed.

**Done when.** A small real task on the sandbox repository produces a pushed branch whose tree changes what the issue asked for, validated by S2's harness. Both providers pass the contract checks, and at least one small real implementation run has been made against each. Skill instructions are never compacted or summarised. A ceiling crossed mid-loop stops the loop rather than waiting for the next node.

**Not in this slice.** Review, pull requests, labels, comments.

---

### S4 — Review fan-out and Publication Gate

**Scope.** The `/code-review` adapter — two isolated reviewer conversations with read-only toolsets, each given its own role's instructions rather than the whole skill; the join; blocking by kind rather than by count; exactly one remediation round; the Publication Gate's verdict, including Drift and base-branch movement; the three `delivered-as-draft` reasons.

**Why it needs its own slice.** This is the largest unverified assumption in the whole design. The upstream skill was read, never executed; handing a reviewer the full skill also hands it the instruction to spawn another pair, and nothing yet proves the fan-out does not recurse or re-enter inside the implementer.

**Done when.** Two separate reports come back from one candidate; neither reviewer spawns another; review does not re-enter inside `implement`; both reviewers see the identical Base Revision and Delivery Snapshot pair; a blocking finding sends the Attempt through exactly one remediation round and re-validates and re-reviews the whole `BASE...HEAD` diff; a second unresolved round yields a draft verdict rather than a third.

**Not in this slice.** Publishing anything.

---

### S5 — Delivery write sequence

**Scope.** The `GitHubClient` port with its real and fake adapters and the contract test suite that runs against both; the Run Ledger; write nodes with intent records, receipts and remote verification; the Delivery Intent and the delivery-completion path; the pull request body and issue comment; the terminal write sequence down to `unassign`.

**Done when.** The first complete `issue → pull request` runs on the sandbox repository. Every write kind survives an injected crash in the window after the write and before its confirmation, without duplicating. A pull request closed by a human is treated as a withdrawal, never reopened. A crash after the Delivery Intent completes the sequence rather than restarting the Attempt. The fake and the real adapter pass the same contract suite.

**Not in this slice.** Clarification, selection, the label lifecycle beyond the terminal writes.

---

### S6 — Lifecycle and clarification

**Scope.** The label algebra and its precedence; readiness evaluation producing a Question Set; the Qualifying Answer rule with its ordered exclusions and the effective-role check; the Clarification Round counter and its carry-over across re-authorisation; Carried Decisions bound to a Fingerprint; the TDD seam confirmation; Drift detection; the transition table T1–T16.

**Done when.** Every row of the transition table is exercised, including T14a. Three incomplete answers and three re-authorisations reach `ready-for-human` rather than returning to the first round. The agent's own Question Set is never read as its answer. A comment from someone without an authorising role is ignored and the next comment says so.

**Not in this slice.** Polling.

---

### S7 — Supervisor, selection and recovery

**Scope.** The polling sweep, candidate ordering, the volume lock, the Lease and its heartbeat, startup reconciliation, resume counters, Run Ledger loss reconstruction from comment markers, the Credential Failure halt. This is the automatic issue selection the map deliberately deferred to last.

**Done when.** A container comes up, takes `ready-for-agent` issues in ascending order one at a time, and delivers. Every restart scenario R1–R8 behaves as the contract says. A duplicate sweep over an issue already claimed by a live Lease does nothing. A revoked credential halts the Worker instead of failing the queue one issue at a time.

**Not in this slice.** Multiple workers. That is out of scope for v1 entirely.

---

### S8 — Operational hardening and handoff

**Scope.** The redaction choke point verified against both log streams and against tool arguments and results; log and artifact retention; workspace retention rules; the free-space check; per-command timeouts; the deployment's resource limits; the service sidecars; and the handoff documentation.

**The handoff set**, of which three already exist as the output of the planning map:

| Document | Status |
| --- | --- |
| `docs/plan/v1-implementation-plan.md` | this file |
| `docs/plan/v1-acceptance-matrix.md` | exists |
| `docs/contract/v1-runtime-contract.md` | exists |
| `docs/ops/deployment.md` | S8 — compose, volume, services, limits, environment |
| `docs/ops/credentials.md` | S8 — provisioning, rotation, what to do on a Credential Failure |
| `docs/ops/triage.md` | S8 — Terminal Outcome → what a human does next |
| `docs/target-repo/project-profile.md` | S8 — how to write a profile, addressed to target repositories |

**Done when.** No registered secret appears in any log, transcript, tool argument or tool result. The volume's free-space threshold stops an Attempt before SQLite meets a full disk. The handoff set is complete, and `deployment.md` has been followed from scratch by someone who did not write it.

---

## Completion criteria for v1

All of the following. None is optional, and nothing outside the list is a gate.

1. Every row of the acceptance matrix is green.
2. On the sandbox repository: one clean `delivered`, and one `delivered-as-draft` for **each** of `drift`, `outstanding-findings` and `failing-validation`.
3. **Every Terminal Outcome has been reached at least once**, including the `failed` classifications `unsupported-environment`, `credential-failure`, `no-change-produced`, `clarification-exhausted` and `repeated-process-loss`.
4. For **every write kind**, idempotency demonstrated by an injected crash in the window after the write and before its confirmation.
5. All three languages pass the language layer of the matrix inside the image.
6. Both providers pass the provider layer.
7. `docs/ops/deployment.md` reproduced from scratch by a person who did not write it.

**Deliberately not completion criteria**: per-Attempt cost, wall-clock performance, image size, or any of the tuning constants the map records as dim. Those are set against observed runs, and the first full paid run in S7 is where the cost ceiling gets its first honest number.

## After v1

The implementation slices are ordinary GitHub issues in this repository, created when their slice begins rather than all at once — the plan will change after the first real run, and one document ages better than a wall of stale issues. They do **not** carry `ready-for-agent` while the agent cannot work them.

Once S5 has delivered a real pull request from a real issue, the agent becomes a candidate for its own remaining slices. Turning that on is a deliberate act, not a default: it means committing a `docs/agents/project-profile.yml` for this repository and applying the Selection Label by hand, issue by issue.
