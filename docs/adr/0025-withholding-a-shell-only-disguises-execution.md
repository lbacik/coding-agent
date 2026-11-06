---
status: accepted
---

# Withholding a shell only disguises execution; the implementer gets one, guarded by effect, not by command text

The implementing Workflow Step gets a general `run_command` tool. Its runs count for nothing as evidence. Alongside it there are two narrow runtime-owned tools: `run_check`, which runs a named Validation Contract check exactly as pinned, and `read_change`, which the implementer now binds too. No command allowlist governs `run_command`. What it may not disturb is protected by watching effects: the Execution Footprint for the workspace, and a **Ref Guard** for the Attempt's branch and HEAD. Decided in [Should the implementer get a shell, or narrow runtime-owned tools for checks and git history?](https://github.com/lbacik/coding-agent/issues/196).

## Context

The MVP implementer had no shell. Its only model-reachable execution of project code was `test_targeted`. [ADR 0018](./0018-runtime-owned-validation-authenticates-the-invocation-not-the-code.md) had already established that this absence protects nothing: `write_file` plus `test_targeted` is arbitrary execution with the Worker's privileges, so a shell adds neither capability nor reach.

The absence still had a cost, and it was functional:

- The Validation Contract's `checks` (type checks, linters) could not run inside the tool loop. The model first met them in `final_checks`, which has no remediation round, so a type error became `failed` / `failing-validation`.
- No git history was readable. The file tools skip `.git/`, and the implementer did not even hold `read_change`.
- No ad-hoc command could be run to observe behaviour, such as a request to a local server.

Each of these was still reachable, just in disguise. A model that wants `mypy`, `git log` or `curl` can write a test that runs it through `subprocess` and call `test_targeted`. That route has costs of its own:

- the output is visible only when the probe test fails;
- probe files are left in the tree and have to be cleaned up;
- the probes show up in Criterion Evidence;
- the Transcript records "wrote a test and ran it" instead of the command the model actually meant.

## Decision

- **`run_command`.** A command string run through `sh -c` in the Project Profile's working directory.
  - Same environment rules as validation commands: credential-free (contract §18), process-group kill on a code-owned timeout. The model may ask for a shorter timeout, never a longer one.
  - Network access is allowed, as a stated limit.
  - The result (exit code, stdout, stderr) is redacted and capped like any tool result.
  - Every call records an Execution Footprint.
  - **Nothing it runs counts as evidence.** Only `test_targeted` produces Criterion Evidence, and only `final_checks` produces Validation Evidence.
- **`run_check`.** Takes a check `name`. The schema is an enum generated per Attempt from the Validation Contract's `checks`. Under `checks: none` the tool is not bound.
  - It runs exactly the pinned command, so an in-loop check cannot differ from the one `final_checks` will run. A model that types its own variant of the command through `run_command` could get a falsely green result.
  - It is **diagnostic only**. The result goes to the model and the Run Ledger and is an input to nothing: not to the Completion Claim checks, not to the Implementation Outcome, not to the Publication Gate. `final_checks` stays the one authoritative run.
- **`read_change`.** The Sub-agents' existing tool is bound for the implementer too. It shows what the Delivery Snapshot would commit, untracked files included. Git history (log, show, blame) is read through `run_command`. No dedicated git readers are added.
- **Ref Guard.** Commits, the branch and HEAD belong to the runtime: the Delivery Snapshot, review snapshot X, the `Attempt:` trailer and one branch per Attempt. After every tool call that executes project code (`run_command`, `run_check`, `test_targeted`), the runtime compares the branch and HEAD with their state before the call.
  - If either moved, it restores both, resetting HEAD softly to the previous commit so the working tree is kept.
  - It records the event in the Run Ledger and says so in the tool result.

  The Execution Footprint cannot see this, because it never watches `.git/` and a commit leaves the tree unchanged.
- **The Attempt Header** names each tool's role. It says that commits are made by the runtime, and that in-loop checks are diagnostic while `final_checks` decides.
- **Unchanged:** Sub-agents keep their fixed read-only set with no shell ([#193](https://github.com/lbacik/coding-agent/issues/193) owns that boundary). Every call counts once against `limits.tool_calls`. The cooperative threat model of ADR 0018 stands.

## Considered options

**Narrow tools only.** Rejected. Every new need would mean amending the toolset, releasing and rerunning, and meanwhile the model improvises through probe tests. This hides the execution without preventing it.

**A shell only.** Rejected. Checks run through a shell carry no guarantee of being the pinned command, and the runtime cannot recognise them as a check run.

**Dedicated git readers** (`git_log`, `git_show`, `git_blame`). Rejected once a shell exists. The model knows git better than any schema we would write, the result is capped either way, and the Ref Guard covers mutation.

**A command allowlist or denylist** (`git:*` style permissions). Rejected for the MVP. A pattern over command text is not a boundary: `sh -c`, `env`, a script written with `write_file`, or `subprocess` from a test all evade it. ADR 0018 already places the boundary elsewhere. Protection keyed to effects cannot be evaded by rephrasing the command. Operator-configured permissions per Workflow remain possible later, under the "tool-set customisation" item of contract §19.

**Gating the claim on in-loop checks.** Rejected. It would create a second, weaker `final_checks` and make the Implementation Outcome depend on something other than Criterion Evidence ([ADR 0014](./0014-completion-is-declared-and-substantiated-never-inferred.md)).

**Hiding `.git` from executed commands.** Rejected. It breaks git reads and any project tooling that relies on the repository, and a restore after the fact gives the model the same feedback.

## Consequences

- The contract's "no shell" wording in §8 is superseded. The ADR 0014 note that the implementing Step has no shell tool now reads in the light of this ADR.
- Reviewer Sub-agents and the implementer now differ: only the implementer can execute project code. That was already true through `test_targeted`.
- The Ref Guard restores refs; it does not undo `git stash`, `git checkout -- <path>` or similar tree changes. Those are changes to the working tree and appear in the Execution Footprint like any other.
- **Reversing this** means removing `run_command` and accepting probe tests as the observation channel again. Nothing else depends on the shell.
