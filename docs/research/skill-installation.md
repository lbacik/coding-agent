# Selective and reproducible remote skill installation

Research for [issue #3](https://github.com/lbacik/coding-agent/issues/3), inspected 2026-09-07. This document establishes facts and implementation options; it does not select the container architecture or modify the installer.

## Answer

`agent-installer` can install from pinned remote Git revisions without interaction, but version **0.5.1 cannot select an exact allowlist through its CLI**. Its noninteractive operation installs all discovered artifacts and silently excludes conflicts from installation. A small staging and verification adapter can use the existing CLI; alternatively, the installer needs new selection and strict-verification capabilities. Neither package installation nor skill installation provides a dependency resolver for skills. [CLI](https://github.com/lbacik/agent-installer/blob/dd2fff0d786ea269b952e4bfb0198f6afb4355fa/src/cli.ts), [discovery](https://github.com/lbacik/agent-installer/blob/dd2fff0d786ea269b952e4bfb0198f6afb4355fa/src/source.ts), [installation](https://github.com/lbacik/agent-installer/blob/dd2fff0d786ea269b952e4bfb0198f6afb4355fa/src/install.ts)

## Version and provenance

The npm registry metadata for `agent-installer@0.5.1` identifies its repository as `lbacik/agent-installer` and its `gitHead` as `dd2fff0d786ea269b952e4bfb0198f6afb4355fa`. It declares Node `>=20`, exposes the `agent-installer` executable, and publishes integrity `sha512-F++mGZQFKyT4cjROVoWaB2zBkKKEhJNUHQ8lrkzXyCae1jgYPkyG4iWQ5GbIYDoaGhoF7WyZfC/iZ9iT4UO+jQ==`. Its dependencies include version ranges and it has no npm shrinkwrap. Therefore, pinning the top-level npm version alone is not a complete dependency lock. Use a committed package-manager lock and a compatible pinned Node image when defining a reproducible installer environment. [Version metadata](https://registry.npmjs.org/agent-installer/0.5.1)

All installer source links below use that exact revision. The selected upstream skill snapshot inspected here is `mattpocock/skills@3cca18b368ae95cdbdebbff572ccafa662551015`; changing that revision requires repeating the dependency and instruction review.

## Supported behavior

| Concern | Verified behavior at 0.5.1 | Consequence for the agent |
| --- | --- | --- |
| Source input | Local directories or HTTPS Git URLs; remote resolution invokes `git clone --depth 1`. | Local staging need not be a Git repository. SSH URLs and GitHub `tree/...` pages are not supported source forms. Use the repository clone URL. |
| Revision selection | `--ref` fetches the supplied branch, tag, or commit and checks out `FETCH_HEAD` detached. It is rejected for local inputs. | Use a full immutable commit SHA, and verify the resolved checkout in an adapter. A branch or movable tag is not reproducible. Remote fetch still requires server availability and access. |
| Discovery | Only `<source>/skills/` is searched for skill directories; default depth is three levels below that directory, configurable with `--skill-max-depth`. | Point to a containing source root, not directly to an individual skill directory. Repositories with other layouts need staging. |
| Other artifacts | Top-level Markdown files in `<source>/prompts/` and `<source>/commands/` are also discovered. | Installing a whole remote repository may install commands as well as skills. |
| Selection | Interactive selection is supported; noninteractive `install` requires `--all`. No allowlist, target-root, manifest, or JSON-output flag exists. | Exact automated selection requires an adapter or installer change. |
| Names | Skill identity is `skill:<directory-basename>`, independent of its frontmatter name. Duplicate basenames in one scan raise an error. | Validate names across all source repositories before installing. Renaming folders may also require changing references inside skills. |
| Copying | A selected skill directory is copied recursively, followed by any invocation-policy overlay. | Nested reference files, assets, and scripts inside that directory are copied; referenced sibling directories or external tools are not fetched. |
| Destinations | Canonical skills are under the process user's `~/.agents/skills`; `~/.claude/skills` contains symlinks. Prompts go to `~/.agents/prompts` with links under `~/.claude/commands`. | Build and runtime must agree on the actual user home and absolute paths; copying only symlinks into a different home breaks exposure. |

Sources: [source resolution](https://github.com/lbacik/agent-installer/blob/dd2fff0d786ea269b952e4bfb0198f6afb4355fa/src/source-resolver.ts), [scanner](https://github.com/lbacik/agent-installer/blob/dd2fff0d786ea269b952e4bfb0198f6afb4355fa/src/source.ts), [CLI](https://github.com/lbacik/agent-installer/blob/dd2fff0d786ea269b952e4bfb0198f6afb4355fa/src/cli.ts), [copy operation](https://github.com/lbacik/agent-installer/blob/dd2fff0d786ea269b952e4bfb0198f6afb4355fa/src/install.ts), [target paths](https://github.com/lbacik/agent-installer/blob/dd2fff0d786ea269b952e4bfb0198f6afb4355fa/src/paths.ts).

## Conflicts, updates, and verification

The CLI filters installation candidates to `new` and `installed-different`. It does **not** pass `conflict` entries to the installation function that would throw for conflicts. Consequently, `install --all` can report success with selected artifacts missing. Entries present in state but absent from a new source scan are not removed by `install --all`. An exact installed set therefore cannot be inferred from exit status or the installed count. [CLI](https://github.com/lbacik/agent-installer/blob/dd2fff0d786ea269b952e4bfb0198f6afb4355fa/src/cli.ts), [reconciliation](https://github.com/lbacik/agent-installer/blob/dd2fff0d786ea269b952e4bfb0198f6afb4355fa/src/install.ts)

Managed ownership includes source identity. Remote identity contains the sanitized URL and the requested ref; changing the ref can make a same-name skill conflict with its previous installation. Local identity is the real source directory path; changing a temporary staging path can have the same effect. Credentials, query strings, and URL fragments are removed from the stored remote identity, but the stored ref is not independently resolved into a commit SHA. [resolver](https://github.com/lbacik/agent-installer/blob/dd2fff0d786ea269b952e4bfb0198f6afb4355fa/src/source-resolver.ts), [ownership comparison](https://github.com/lbacik/agent-installer/blob/dd2fff0d786ea269b952e4bfb0198f6afb4355fa/src/install.ts)

State in `~/.agents/agent-installer/state.json` records IDs, source identity/path, destination paths, source and installed hashes, and installation timestamps. Hashing covers sorted regular-file paths and contents, excludes `.agent-installer.json`, and does not cover symlinks or file permissions. Source hashes account for generated metadata overlays; they are not always hashes of the unmodified upstream tree. Also, a missing exposure symlink can still be classified `installed-same`, so rescanning alone does not guarantee that exposure exists. [state schema](https://github.com/lbacik/agent-installer/blob/dd2fff0d786ea269b952e4bfb0198f6afb4355fa/src/state.ts), [hashing](https://github.com/lbacik/agent-installer/blob/dd2fff0d786ea269b952e4bfb0198f6afb4355fa/src/hash.ts), [reconciliation](https://github.com/lbacik/agent-installer/blob/dd2fff0d786ea269b952e4bfb0198f6afb4355fa/src/install.ts)

Recommended adapter verification: compare both the filesystem inventory and managed IDs against the complete allowlist; require every entrypoint and referenced local asset; validate provenance, expected installed content, script executable bits, and symlink targets separately. Fail installation on any missing, unexpected, or conflicting artifact. Exclude installer timestamps and markers from reproducibility comparisons, while retaining them as operational metadata. This is a proposed contract, not a feature already supplied by the CLI.

## Skill completeness and invocation

At the pinned upstream revision, `/implement` explicitly invokes `/tdd` where possible and `/code-review` after implementation, then asks for a commit. It declares `disable-model-invocation: true`. TDD refers to local `tests.md` and `mocking.md`, and conditionally invokes `codebase-design` when interface/seam design needs clarification. Code review requires issue-tracker configuration and two parallel reviewer contexts. Thus, at least `implement`, `tdd`, and `code-review` are direct skill requirements, and `codebase-design` is a conditional dependency requiring an explicit bundle decision. Copying Markdown alone does not implement Git, testing, issue retrieval, confirmations, or reviewer execution. [implement](https://github.com/mattpocock/skills/blob/3cca18b368ae95cdbdebbff572ccafa662551015/skills/engineering/implement/SKILL.md), [tdd](https://github.com/mattpocock/skills/blob/3cca18b368ae95cdbdebbff572ccafa662551015/skills/engineering/tdd/SKILL.md), [code-review](https://github.com/mattpocock/skills/blob/3cca18b368ae95cdbdebbff572ccafa662551015/skills/engineering/code-review/SKILL.md)

The installer discovers directories and copies artifacts; it does not follow slash-command references, resolve skill dependencies, install Python/PHP/TypeScript tools, or validate an execution environment. A bundle manifest should record manually reviewed direct and conditional dependencies plus external capabilities. Validate the complete selected closure at each pinned revision, including relative files and repo-level instructions; no generic parser can reliably infer every dependency from prose. This recommendation follows the limited scanner/install behavior above.

For literal YAML boolean `disable-model-invocation: true`, the installer generates or merges `agents/openai.yaml` with `policy.allow_implicit_invocation: false`. It preserves other authored metadata, but malformed authored metadata blocks translation; string `"true"` does not activate translation. [translation implementation](https://github.com/lbacik/agent-installer/blob/dd2fff0d786ea269b952e4bfb0198f6afb4355fa/src/skill-invocation-policy.ts)

A custom LangGraph runtime must enforce invocation policy itself: explicitly dispatch configured `/implement` from the workflow; expose only eligible skills for model-driven discovery; reject model-driven activation of explicit-only skills. Explicit invocation remains possible through a trusted workflow or user request. Preserve the original frontmatter and inspect authored policy metadata during bundle validation; the generated Codex file is compatibility metadata, not enforcement in an OpenAI or Anthropic API call. This is a runtime recommendation, not an installer-provided runtime.

## Implementation options

### A. Allowlist staging with the existing CLI

Proposed algorithm:

1. Read a locked manifest mapping each skill name to repository URL, full commit SHA, source-relative directory, and expected content hash. Record installer version/integrity and reviewed dependencies.
2. Fetch each pinned checkout, verify its actual commit, and copy only selected complete directories into a deterministic staging root containing `skills/`. Preserve relative supporting files; reject ambiguous names and escaping or unsupported symlinks.
3. Inspect the staged discovery set before installation. A selected directory can itself contain another `SKILL.md`, which the scanner may discover as an additional skill; flatten selected roots under `skills/` and use `--skill-max-depth 1`, or explicitly validate and accommodate the resulting set. Depth limits discovery, not recursive copying of supporting assets.
4. Run `agent-installer install /opt/agent/skill-source --all --skill-max-depth 1` using the intended container user. Use an otherwise empty dedicated skill store for a fresh image; repeated staging must retain its source identity or explicitly reconcile previous managed artifacts.
5. Verify the exact inventory and content as described above. Persist an independent provenance manifest because installer state now identifies the staging directory, not the original remote sources.

Tradeoff: no upstream change, and arbitrary upstream layouts become usable; the agent owns allowlisting, dependency declarations, provenance, stale-entry handling, and verification. Flattening is only suitable after checking that relative paths do not rely on the original repository layout. The algorithm is proposed from the verified scanner and installer contracts, not experimentally implemented in this research.

### B. Extend agent-installer

Potential upstream capabilities are repeatable artifact selectors or a manifest, an explicit installation destination, structured scan output, strict failure on conflicts/missing selections, resolved-commit provenance, and explicit reconciliation of obsolete managed entries. This keeps selection closer to discovery and preserves remote source attribution, but requires a separate upstream implementation/release and migration contract. Dependency execution and LangGraph invocation still belong to the agent. No such changes are part of this research.

## Container implications and remaining decisions

Remote installation requires Node satisfying the package and resolved dependencies, npm or another package manager, Git, network access, and any HTTPS credentials required by the source. The resolver uses temporary clones and deletes them on completion; existing Git credential helpers handle HTTPS access. Local staged installation avoids remote fetching by the installer, although fetching the staged sources still needs Git/source access somewhere in the build pipeline. [npm metadata](https://registry.npmjs.org/agent-installer/0.5.1), [resolver](https://github.com/lbacik/agent-installer/blob/dd2fff0d786ea269b952e4bfb0198f6afb4355fa/src/source-resolver.ts), [README](https://github.com/lbacik/agent-installer/blob/dd2fff0d786ea269b952e4bfb0198f6afb4355fa/README.md)

Build-time installation can package a reviewed skill bundle for offline runtime loading; the installer itself need not run at startup. Startup installation enables runtime bundle selection but adds source availability, credentials, mutation, and restart-reconciliation requirements. Python, PHP, TypeScript, and individual skill scripts may need additional tools regardless of when the bundle is installed. These are engineering implications, not claims that the installer provisions those environments.

The container/environment ticket still needs to select staging versus an upstream extension, build-time versus startup installation, the initial allowlist and conditional dependency policy, artifact update/reconciliation policy, user/home paths, and language-toolchain provisioning. The runtime ticket must define explicit dispatch, dependency invocation, documentation readiness, and reviewer capabilities. Requiring missing project documentation to route a task to `needs-info` remains a product requirement independent of successful skill installation.

## Verification limits

This investigation inspected the npm version metadata and pinned upstream source files, including scanner, resolver, CLI, installer, state, hashing, and invocation-policy translation. No real skills were installed into the developer's home, no installer modifications were made, and no container integration behavior was tested. The conflict-success and discovery conclusions follow directly from control flow; implementation should cover these cases with acceptance fixtures before relying on the adapter.
