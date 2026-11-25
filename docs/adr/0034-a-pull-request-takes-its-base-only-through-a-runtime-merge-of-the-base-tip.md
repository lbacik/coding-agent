---
status: accepted
---

# A pull request takes its base only through a runtime merge of the Base Tip

A `pull-request` Workflow brings its base branch into the pull request through one new Runtime Operation, `update-from-base`, and the following `commit-and-push`, which together produce a real merge commit that fast-forwards the head branch. The operation merges the pinned **Base Tip** (never the base branch's current tip) into the workspace without committing, and ends `merged`, `conflicted` (standard conflict markers left in the working tree, with the conflicted paths in its facts) or `up-to-date` (the Base Tip is already an ancestor of the Input Head). The runtime records the Base Tip as a pending second parent in the Run Ledger. The next `commit-and-push` that creates a commit uses it and records exactly two explicit parents, the Input Head and the Base Tip. This extends the first release's closed Runtime Operation catalog (#285 point 7) by one operation, for `pull-request` Workflows only (#636). The decision records intent: it does not claim that the current code implements it.

## Why

- **Rebasing is ruled out by existing guarantees.** The runtime owns commits, the branch and `HEAD` (Ref Guard), and `commit-and-push` never force-pushes. A merge commit is the only way to update a pull request that still fast-forwards its head branch.
- **A tree-only merge does not resolve the conflict on GitHub.** A single-parent commit whose tree already contains the base leaves the merge base where it was. GitHub then recomputes the same conflict.
- **The runtime does the deterministic part.** If the model ran `git merge` in `shell` and the runtime only added the second parent, the runtime would vouch for a tree it never checked. If the model quietly dropped the base's changes, the result would claim to contain the base while reverting it. With the runtime doing the merge, every non-conflicting base change is in the tree, and the model touches only the conflicts.

## Considered options

- **An opt-in `with` key on `commit-and-push` that adds the Base Tip as a parent after a model-made merge.** Rejected for the unverifiable-tree reason above.
- **Both an operation and the `with` key.** Rejected: two routes to the same commit shape, one of them unsafe.
- **Merging the base branch's current tip.** Rejected: it is not reproducible from the Run Ledger, and it makes base-branch movement matter to the Attempt. New base commits stay disclosed, never Drift.

## Consequences

- **The merge commit is exactly the merge plus conflict resolution.** It may differ from the runtime's automatic merge result only in the conflicted paths. Any other difference makes `commit-and-push` refuse with `error`. A Workflow that also fixes tests does so in a later commit.
- **Unresolved conflicts are refused.** A line in a conflicted path that begins with exactly seven `<` (`<<<<<<<`), `=` (`=======`) or `>` (`>>>>>>>`) characters makes `commit-and-push` end `error` and name the paths. No new result is added to its catalog.
- **Parents are always explicit.** A `MERGE_HEAD` or any other merge state left by `shell` is ignored and cleared. It never becomes a parent. The incidental `git merge --no-commit` route to a merge commit is closed.
- **The `.github/workflows/` refusal applies to the Attempt's own authorship.** A path under `.github/workflows/` is refused when its content in the new commit is not taken verbatim from either parent. For a single-parent commit this is exactly the existing rule.
- **The pending parent's lifetime is bounded.** It survives `stale-head` and `error`. A second `update-from-base` while one is pending ends `error`. An Attempt that ends without pushing discards it.
- **The Base Revision stays the Claim-time record.** For a `pull-request` input the change-versus-base reader compares against the merge base of `HEAD` and the Base Tip: before the merge that is the Base Revision, after it the Base Tip. The reader therefore keeps showing only the pull request's own changes.
- **Validation needs no new rule.** Evidence is bound to tree identity, and the merge commit inherits its tree's validation result. The Validation Contract stays the profile at the start commit, so a profile change brought in from the base changes later Attempts, never this one.
- **The Base Tip becomes an Attempt fact** for `pull-request` inputs, alongside the base branch, head branch and Input Head.
