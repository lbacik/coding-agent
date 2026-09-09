# Runbook: provision the Agent Identity credential and sandbox

Covers [issue #12](https://github.com/lbacik/coding-agent/issues/12) (S0a). Produces the three things S3 onward depends on: a working fine-grained token, a sandbox Target Repository that reproduces the real deployment arrangement, and the labels a later slice needs to select an issue.

Read [ADR 0005](../adr/0005-agent-identity-is-the-repository-owner.md) and [contract §10](../contract/v1-runtime-contract.md#10-identity-and-authorization) before running this. The arrangement below is deliberately the *unfriendly* one: the token owner writes to a repository the token owner owns, because that is the only arrangement the deployment actually uses.

## Prerequisites

- `gh` CLI authenticated as the Target Repository owner's own GitHub account (`gh auth status`), with the default OAuth/classic token — this is only used to create the sandbox repository and its labels, never as the Agent Identity credential itself.
- A browser session logged in as that same account, to mint the fine-grained token (GitHub does not expose personal-access-token creation through any API).

## Step 1 — create the sandbox Target Repository

Owned by the same account that will own the token. Public, per ADR 0005 (the real Target Repository is a personal, public repository — a private sandbox would test a different permission surface).

```sh
gh repo create <owner>/<sandbox-repo> --public \
  --description "Sandbox Target Repository for the coding-agent Agent Identity credential (S0a)"
```

Skip this if `gh repo view <owner>/<sandbox-repo>` already succeeds — the step is idempotent.

## Step 2 — seed the labels

Exactly the labels the contract's algebra uses, plus the selection label. No `wayfinder:*` label — those belong to this repository's own tracker, not the sandbox.

```sh
gh label create "ready-for-agent"  --repo <owner>/<sandbox-repo> --color "0E8A16" --description "Fully specified, ready for an AFK agent"
gh label create "needs-info"       --repo <owner>/<sandbox-repo> --color "D93F0B" --description "Waiting on reporter for more information"
gh label create "agent-running"    --repo <owner>/<sandbox-repo> --color "1D76DB" --description "The Worker currently holds this issue"
gh label create "ready-for-human"  --repo <owner>/<sandbox-repo> --color "5319E7" --description "Requires human implementation"
```

`wontfix` ships as a GitHub default label on every new repository — check `gh label list --repo <owner>/<sandbox-repo>` before creating it again; `gh label create` fails on a name that already exists.

## Step 3 — issue the fine-grained token

This is a browser-only, human step. There is no API for it.

1. Go to `https://github.com/settings/personal-access-tokens/new` while logged in as the Target Repository owner.
2. **Resource owner**: that same account (not an organization).
3. **Repository access**: "Only select repositories" → the sandbox repository created in Step 1. Never "All repositories".
4. **Permissions** (`Repository permissions`), exactly these four — every write kind the contract's node inventory performs, and nothing else:
   - **Contents**: Read and write (branch push)
   - **Issues**: Read and write (comments, labels)
   - **Pull requests**: Read and write (open, close)
   - **Metadata**: Read (mandatory, GitHub adds this automatically)
5. **Do not grant the "Workflows" permission.** Leave it at "No access". This is the boundary S0b's preflight command asserts by attempting a rejected write — granting it here would make that assertion pass for the wrong reason.
6. **Expiration**: pick a finite expiration (GitHub's maximum is 1 year). Do not choose "No expiration" — the startup check in contract §10 assumes `github-authentication-token-expiration` is present and readable, and picking a bounded date rehearses the rotation the runbook's "when it expires" note below describes.
7. Generate the token, then immediately store it in a password manager or secret store — never in a file inside this repository, never in shell history. Copying it into an unversioned `.env` that is `.gitignore`d is acceptable for local development; committing it is not.

## Step 4 — record where it lives

Fill this in immediately after Step 3, in the comment or PR that closes out this runbook's run — not in this file, which is the reusable procedure rather than a per-run record:

- Token stored at: `<password manager entry / secret store path>`
- Expiry date: `<date from Step 3>`
- Sandbox repository: `<owner>/<sandbox-repo>`

**When it expires**: repeat Step 3 against the same sandbox repository (Steps 1–2 do not need to be redone), and update the record above.

## Verification

This runbook only provisions the credential and the sandbox; it does not prove the credential works. Proof is [S0b](https://github.com/lbacik/coding-agent/issues/13)'s `agent preflight` command, run against the sandbox repository from Step 1, using the token from Step 3. `agent preflight` performs the write probes (comment, label add/remove, branch push, PR open/close, a rejected `.github/workflows/` push) and the non-mutating startup checks (`L3-ERR-10a…d` in [the acceptance matrix](../plan/v1-acceptance-matrix.md)) that this runbook cannot itself exercise.

## Following this runbook end to end

Issue #12's acceptance criterion is that this runbook is followed by someone who did not write it, so that the document is judged on what it says rather than on what its author remembers. This account owns no organization, so there is no second person to hand it to; the substitution is to follow it in a **fresh container or VM with no access to this shell's history, no saved credentials, and no editor state** — close enough to a naive reader that gaps in the document surface the same way they would for an actual second person.

Record the outcome of that run — date, environment, and whether any step needed correction — as a comment on issue #12, not in this file.
