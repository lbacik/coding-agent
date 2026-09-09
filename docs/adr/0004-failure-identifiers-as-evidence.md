---
status: accepted
---

# A Baseline Failure is a named failure, not a red command

A validation command is excused as a **Baseline Failure** only when the set of individual failure identifiers it produces against the Delivery Snapshot is a subset of the set it produces at the Base Revision. To make that checkable, the Project Profile moves to `schema: 2` and must declare how each test command reports its individual results — `junit-xml` in v1. Decided in [Define implementation sequence and v1 acceptance scenarios](https://github.com/lbacik/coding-agent/issues/8).

The rule it replaces compared exit codes. [Define verified pull request delivery and recovery](https://github.com/lbacik/coding-agent/issues/7) §3 excused a command that failed at both the Base Revision and the Delivery Snapshot, on the reasoning that a pre-existing failure is not this Attempt's debt. That reasoning is right about the failure and wrong about the command: a command that runs a whole suite is not a single test. If test A fails at the base, and A **and** a newly broken B fail at the candidate, both runs exit non-zero and the old rule excuses the command — publishing a regression as a clean delivery. The same hole exists for a static analyser that was already red.

This is the one hole that defeats the purpose of the entire delivery gate, because it fails in the direction of publishing.

## Considered options

**Keep the exit-code rule and accept the gap.** Rejected. The gap is not rare: a repository with one long-broken test gets a permanent exemption for its entire suite, and that repository is exactly the kind that most needs the check.

**Re-run the full suite at the Base Revision on every Attempt and diff the counts.** Rejected. Counts are not identifiers — A fixed and B broken keeps the count at one — and it pays the cost of a full base run on every Attempt, against the wall-clock ceiling, even when nothing failed.

**Have the model read the two test outputs and judge whether anything is new.** Rejected outright. It makes the model's account of a test run into evidence, which is the one thing [Define verified pull request delivery and recovery](https://github.com/lbacik/coding-agent/issues/7) §3 exists to forbid. Test output parsing is a solved problem in every one of the three runners.

**Have the agent append reporter flags to the project's commands.** Rejected. The Project Profile is a contract the Target Repository writes, not a template the agent rewrites; a repository with a wrapper script, a custom reporter or a `Makefile` target would get a mangled command. Asking the repository to declare its own reporting keeps one authority for what "run the tests" means.

## Consequences

**A repository must declare machine-readable test reporting to be worked at all.** An absent `evidence` block is a missing Readiness Fact and routes to clarification, like any other. This is a real barrier to entry, chosen deliberately: the human who answers that question fixes it permanently by committing the profile, which is the mechanism the Project Profile was introduced for.

**`junit-xml` is the only format in v1**, because pytest, PHPUnit and vitest all emit it natively, so one parser serves three languages. A fourth language, or a runner that cannot, needs a new format and a new parser — a bounded, obvious piece of work rather than a redesign.

**Where a command cannot name its failures, the Attempt cannot claim the absence of a regression.** That is stated as an outcome rather than left implicit: the safe result is `delivered-as-draft` with reason `failing-validation`, and a human judges. This is why `evidence` stays optional for `checks` — a static analyser without machine-readable diagnostics degrades to "cannot excuse a red baseline", which is inconvenient but honest, whereas the same degradation on the test command would gut the gate.

**Baseline evaluation stays lazy.** Only commands that failed against the Delivery Snapshot are re-run at the Base Revision, and only those. The change is to the comparison, not to when it happens.

**Excluded Baseline Failures are named individually in the pull request body**, with their identifiers. A reviewer can see exactly which pre-existing failures were set aside, instead of reading that a command was excused.

Reversing this decision means restoring a rule under which a new broken test can hide behind an old one. Any replacement has to say how it detects that case instead.
