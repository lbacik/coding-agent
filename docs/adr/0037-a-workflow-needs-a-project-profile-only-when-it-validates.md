---
status: accepted
---

# A Workflow needs a Project Profile only when it validates

A Workflow **needs a Validation Contract** only when one of its agent steps declares the `tests` tool group, or one of its operation steps is `check-readiness` or `run-validation`. Nothing else binds against the Project Profile: `shell`, `subagents`, `advisor`, `commit-and-push` and `update-from-base` never read it. The property is computed once from the loaded Workflow, and startup, the Attempt's start and `config validate` all use it. Decided while triaging [#684](https://github.com/lbacik/coding-agent/issues/684), where a `triage` Workflow that never validates claimed an issue and then crashed because the Target Repository carried no profile. This amends [ADR 0030](0030-the-workflow-configuration-may-supply-the-project-profile.md), whose `project-profile` startup phase reported `skipped` whenever the profile came from the Target Repository.

## Considered Options

- **One Validation Contract for every Attempt** (the behaviour before #684). Rejected. Every Target Repository would have to carry agent configuration just to run triage or answer review comments, which is the coupling ADR 0030 set out to remove. It would also pin a contract that nothing judges the Attempt by.

## Consequences

- **Startup.** When the Workflow needs a Validation Contract and `target.profile` is absent, the `project-profile` phase reads `docs/agents/project-profile.yml` at the base branch tip. A missing file, or one that does not parse or fails the schema, is a Startup Refusal naming the path and revision. The read goes through GitHub, so it runs after the startup checks that establish GitHub access. The `target.profile` check stays where it is. A Workflow that does not need a contract still reports `skipped`.
- **What the pin checks.** Pinning a Validation Contract parses the profile and checks its schema, and nothing else. Toolchain fit and service reachability stay with `check-readiness`, which reports them as a `not-ready` result (Unsupported Environment) for the Workflow to route. They are never a reason the pin fails. The startup check above covers the same ground: parse and schema.
- **A missing pin at Attempt time.** If a Workflow that needs a Validation Contract finds none it can pin at the Attempt's start commit, the Attempt ends `internal-failure`. The Worker then halts with the reason `profile-unavailable`. The cause belongs to the repository, not the issue, so continuing would fail every queued Attempt the same way, just as a Credential Failure would. A restart runs the startup check again, and that check refuses until the profile is restored.
- **Workflows that do not validate.** They pin a valid repository profile when there is one, for provenance. A missing or broken profile is recorded as a fact of the Attempt and is never a refusal or a halt.
- **`config validate`.** It cannot read the Target Repository, so when the Workflow needs a contract and `target.profile` is unset it prints a notice, never a refusal.
