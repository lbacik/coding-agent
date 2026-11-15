# Breaking-change notice: Workflow Configuration `version: 3`

**Release:** `coding-agent 0.3.0` (first v3 release).
**Status: published.** This notice ships with the first v3 release and stays
versioned with it: later releases amend it under a new heading rather than
rewriting this one.
**Date:** 2026-11-15.
**Authority:** behaviour is defined by
[the step-workflow runtime contract](../contract/step-workflow-runtime-contract.md)
alone; this notice only summarises what breaks. Where this notice and the
contract disagree, the contract wins. The operator procedure is
[Upgrading: drain, backup and rollback](./workflow-configuration.md#upgrading-drain-backup-and-rollback).

## What breaks

1. **`version: 2` files are refused.** A file with `version: 2` exits 1 with
   a migration diagnostic before installation, live calls, Claim or GitHub
   writes. The refused file and any old installed artifacts are left
   untouched. There is no converter, no dual engine and no automatic policy
   approval. A `version: 1` file keeps the §16.4 drain note: drain MVP
   Attempts with the compatible release first. (SKV3-01, SKV3-31.)
2. **Legacy CLI options are removed.** `run --skills-home`,
   `--skill-bundle-dir`, the install/list report inputs and the ignore-file
   input fail with exit 2 and point at their replacements: `--home` for
   `prepare-skill-bundle` and `verify-skill-bundle`; `--skill-home`,
   `--config`, `--policy` and `--bundle-dir` for `run`. There is no package
   locator and no ambient discovery. (SKV3-32.)
3. **No implicit Skill Bundle.** A missing `skills` field and `skills: []`
   both mean an empty bundle; the old four-skill default never comes back.
   Every static `skill:` reference must name a declared member, and every
   declared member needs exactly one matching reviewed policy. (SKV3-04,
   SKV3-13, SKV3-14.)
4. **Secret-free build phases; selected-only runtime secrets.** `config
   validate`, `prepare-skill-bundle` and `verify-skill-bundle` pass with no
   Worker GitHub or model secrets set and never take dummy credentials.
   `run` checks only the target token and the selected Workflow's model
   key; a key used only by another Workflow does not block startup.
   (SKV3-20, SKV3-21.)
5. **Strict prompts and exact file binding.** Every referenced prompt is
   read as raw bytes (strict UTF-8, containment-checked, symlink-contained
   only); missing, unreadable, invalid-UTF-8 or escaping prompts refuse
   normally with the YAML location. `verify-skill-bundle` and `run` refuse
   any YAML, prompt or policy bytes that differ from the prepared ones,
   even when semantically equivalent. Disk edits after startup are ignored
   until a restart that passes full validation. (SKV3-06, SKV3-22,
   SKV3-25.)
6. **No weaker `run` path.** `run` repeats the full offline deployment
   verification before any secret or live check and has no skip-bundle,
   skip-credentials or build-mode switch. (SKV3-41 in contract §4 terms;
   SKV3-21, SKV3-33.)
7. **New Worker refuses incompatible open records.** Recovery validates the
   record version, core digest, reference index and every required blob and
   refuses by Attempt and fault; it never converts records, fabricates
   archives from current files, or repairs from the deployment. Drain with
   the compatible release, back up while stopped, and roll back only
   compatibly. (SKV3-28…SKV3-31.)

## Migration (summary)

1. Retain the last compatible executable/image and its deployment inputs.
2. Drain with the compatible release; confirm no unfinished/unconfirmed
   Claim or unsettled lifecycle work remains.
3. Stop the Worker; take a coordinated backup of persistent state and
   operator inputs together.
4. Change the YAML version to `3`; declare the desired Git sources, exact
   names and optional refs explicitly in `skills`. To keep the previous
   behaviour on purpose, declare the old four-skill selection explicitly —
   [`docker/deployment/workflow.yml`](../../docker/deployment/workflow.yml)
   pins `mattpocock/skills` at commit
   `3cca18b368ae95cdbdebbff572ccafa662551015` with
   `include: [implement, tdd, code-review, codebase-design]` — with a
   reviewed [`skill-policy.json`](../../docker/deployment/skill-policy.json)
   whose per-file dispositions carry forward only for matching content.
5. Run secret-free `config validate`, explicitly `prepare` with `--mode
   installed` or `--mode install` (supply `--resolution-manifest` for
   offline symbolic-ref adoption), then `verify` against the final roots.
6. Restart with runtime credentials only, keeping the mandatory live
   checks; never use dummy secrets to make build validation pass.

## Rollback

Once the new release has written new-version records or state, rolling
back means running a compatible release or restoring the coordinated
pre-upgrade backup with reconciliation of the external effects landed
meanwhile. Never attach an older Worker blindly, restore stale state while
ignoring later GitHub effects, or delete history to bypass a refusal.

## What this release does not claim

Agent Forge support is not advertised as verified until its separate
derived-image adapter integration proof runs. Coding-agent v3 ships
independently of that milestone (see
[the release evidence](../plan/v3-release-evidence.md)).
