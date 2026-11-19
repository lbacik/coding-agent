---
status: accepted
---

# The Workflow Configuration may supply the Project Profile

A Workflow Configuration may name a Project Profile with an optional `target.profile` key. The path is relative to the configuration file's directory and is resolved by the same rules as `prompt:` locators. When the key is present, that file is the Validation Contract's source and the Target Repository's `docs/agents/project-profile.yml` is never read for readiness or validation. When it is absent, nothing changes. Decided while triaging [#473](https://github.com/lbacik/coding-agent/issues/473). This reverses the step-workflow runtime contract's rule that "the Project Profile path is not configurable" (§10.2). The MVP contract keeps that rule as a record of the old design.

Operators who deploy agents from a separate repository want the profile to live with the deployment, so the Target Repository stays agent-agnostic and needs no commit carrying coding-agent's Validation Contract before an Attempt can pass `check-readiness`.

## Considered Options

- **An environment variable naming a baked file** (`PROFILE_PATH`, as simple-coding-agent does). Rejected. It needs no schema change, but it escapes three things a Workflow Configuration key gets: static validation when the derived image is built (`config validate`); a pin in the Effective Configuration, so recovery and provenance cover the contract that was used; and freedom from drift, because a variable can change on `docker compose up -d` without a rebuild, so Workers in one Pool could run different contracts under an identical Effective Configuration digest. Environment variables carry credentials only.
- **Supplying it per Workflow.** Rejected. A profile describes the Target Repository's project, and every Workflow in one configuration file works against the same repository, so `target` is the right scope.

## Consequences

- **Pinning.** A supplied profile is pinned at Claim like the operator prompts: the Effective Configuration records it under an optional top-level `project_profile` key (`locator`, `sha256`, `size_bytes`), and its bytes join the Claim's content-addressed blobs in the Attempt Archive. The key is recorded only when the Workflow Configuration supplies the profile. The record stays `version: 2`, and every existing record still reads correctly as "profile from the Target Repository".
- **Refusals.** A supplied profile that does not resolve, decode, parse or pass the schema fails `config validate`. So does a toolchain outside the image's Supported Toolchain Matrix, whenever a matrix is available; without one, `config validate` skips the toolchain check with a notice. At startup, a `project-profile` phase runs after `toolchain-matrix` and refuses with a durable Halt; it reports `skipped` when the profile comes from the Target Repository. With the profile supplied by the deployment, a Toolchain Gap is a fact about the deployment, never a `not-ready` Attempt. `check-readiness` keeps the service-reachability and `bootstrap` checks.
- **Disclosure.** When the deployment supplies the profile, a candidate's edit to the repository's `docs/agents/project-profile.yml` changes no contract. The pull request's `profile_changed_in_candidate` disclosure (L2-17) is replaced by a line naming the Validation Contract's source and digest. A repository profile that exists but is ignored is recorded in the `check-readiness` facts (`profile_source`, `repository_profile_ignored`) and is never a refusal, so a repository can migrate away from its own profile.
