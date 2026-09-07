# Upstream implement skill runtime contract

Research for [Establish upstream implement skill integration requirements](https://github.com/lbacik/coding-agent/issues/2), inspected on 2026-09-07. This document records requirements and integration options; it does not implement the agent or approve unresolved product policies.

## Evidence and scope

All Matt Pocock skill sources below were inspected at commit `3cca18b368ae95cdbdebbff572ccafa662551015`. The Agent Skills specification and client guide are live documents, accessed on the date above, not immutable versioned guarantees. Findings are source inspection, not an end-to-end execution test.

The agreed product scope is one active worker per repository, Python/PHP/TypeScript target projects, and a human manually making an issue ready again after clarification. These decisions do not establish an approved testing seam, a review baseline, a toolchain configuration, or a commit-order policy for every future issue.

## Verified upstream behavior

`implement` is a Markdown instruction set, not an executable workflow engine. It consumes user-provided work/specifications, calls `tdd` where possible at agreed seams, requires repeated type checking and individual test-file execution, a full test run at the end, `code-review`, and a commit on the current branch. It has no issue selection, claim, clarification-transport, branch creation, push, PR publication, or issue-closing procedure. Its frontmatter disables implicit invocation; its authored Codex metadata repeats that intent. Those outer workflow responsibilities belong to the custom harness. [Implement instructions][implement], [authored metadata][implement-metadata]

TDD requires confirmation of written-down testing seams with the user before writing tests. It uses one failing test followed by minimal implementation per slice, tests observable behavior through public interfaces, and places refactoring in review rather than the red/green loop. It reads existing CONTEXT/ADR material and conditionally consults `codebase-design` when the interface or seam is unresolved. Its examples and mocking guidance are separate bundled files. [TDD instructions][tdd], [test examples][tests], [mocking guidance][mocking]

Code review requires a resolving reference and nonempty `git diff <fixed-point>...HEAD`, plus the commit list. It asks for an unspecified reference. It finds the spec from commit issue references, an explicit file, or matching repository documents, then asks if none is found; an explicit answer that no spec exists permits omission of the Spec review. Missing issue-tracker configuration routes the user to setup. Standards and Spec run as separate parallel subagents. Each gets its evidence and a report limit; Standards also gets the complete smell baseline and repo rules. The two reports remain separate, with counts and worst finding per axis. Repo standards override smell heuristics. [Code-review instructions][review]

## Dependency and capability completeness

| Artifact or capability | Required handling |
| --- | --- |
| `implement` | Explicit harness activation; preserve its instructions for the run. |
| `tdd` | Available for nested activation when applicable. Include `tests.md` and `mocking.md`. |
| `code-review` | Available for nested activation, with isolated parallel Standards/Spec conversations. |
| `codebase-design` | Available for the conditional vocabulary consultation in TDD. Preserve `DEEPENING.md` and `DESIGN-IT-TWICE.md` with the directory. |
| Repository context | Read applicable instructions, CONTEXT/ADR files when present, standards, tracker configuration, and the source specification. |
| File and shell tools | Search/read/edit code and supporting resources; run repository-selected checks and inspect exit status/output. |
| Git tools | Resolve baseline, inspect diff and history, inspect working-tree state, and make authorized commits. |
| Model/tool loop | Send instructions and tool results to the selected model, execute tool requests, and continue until a completion or human-input condition. |
| Human-input transport | Persist question and decision context, publish through the outer workflow, and resume with the answer. |

The dependency relationships come from the pinned skills, not a formal dependency lockfile. `codebase-design` is a vocabulary reference in this use: TDD explicitly says not to run a design session. Its optional alternative-design guide uses three or more parallel agents when that separate exploration is requested; that is not an unconditional requirement of `/implement`. [Design vocabulary][design], [deepening reference][deepening], [alternative-design reference][design-twice]

The Agent Skills format defines a directory with YAML frontmatter and Markdown instructions, plus optional resources and scripts. Installing those files does not implement their tool calls or human interactions. Nested skill references need harness lookup/activation semantics; interpreting the text `/tdd` as a shell executable would be incorrect. A complete directory copy also matters because relative links resolve from the skill's directory. [Agent Skills specification][specification]

The client guide describes metadata discovery, catalog disclosure, full instruction activation, and resource loading on demand. It permits direct file reads or a dedicated activation tool and describes explicit invocation handled by the harness. In containers, skills must first be provisioned into the runtime filesystem. Active instructions should survive context compaction and duplicate activations should be avoided. Therefore a Python/LangGraph host is feasible in principle, independently of OpenAI or Anthropic, but compatibility requires implementation and validation of these capabilities; the sources do not certify either provider integration. [Client integration guide][client]

The guide treats opted-out skills as excluded from model-driven discovery. `/implement` can still be the harness-selected entry point for the user's configured workflow. The host must preserve this explicit/implicit distinction, rather than assuming a vendor-specific `agents/openai.yaml` is itself executable policy. [Client integration guide][client], [authored metadata][implement-metadata]

## Human interactions and previously approved inputs

| Source condition | Input that can avoid a new round trip | Limit requiring an explicit contract |
| --- | --- | --- |
| TDD seam confirmation | A human-approved issue/comment/document naming the exact public interfaces and test scope. | Upstream does not prescribe a machine-readable approval format. An inferred seam or a ready label alone does not demonstrate approval. |
| Review baseline missing | A baseline supplied in the task/run contract and resolved to a commit. | An arbitrary default chosen by the model is not a user-supplied fixed point. |
| Review spec missing | The originating issue reference in commits or an explicit, accessible specification file. | The skip path requires a human answer that no spec exists; missing access is not that answer. |
| Tracker setup missing | Existing valid `docs/agents/issue-tracker.md`. | The skill's setup instruction is outside coding execution. Provisioning requirements must be documented. |
| New ambiguity during work | A still-applicable prior recorded decision that directly answers it. | General ambiguity handling is an outer-agent policy, not specified by `/implement`. |

These interpretations distinguish existing evidence of agreement from an invented approval. The product must decide how it recognizes the authorized human and binds approval to the relevant issue/specification version. Manual re-ready can be the resume trigger already agreed with the user; the agent must still evaluate whether the answers resolve the pending questions. A changed seam or materially changed specification can invalidate earlier consent. These are integration recommendations, not additional upstream clauses. [TDD instructions][tdd], [Code-review instructions][review]

## Review versus commit ordering

There is a real integration tension: `/implement` requests review before its commit, while `/code-review` reviews the history ending at `HEAD`. Uncommitted edits are absent from that comparison. A valid but older nonempty diff could be reviewed while the latest edits remain invisible. [Implement instructions][implement], [Code-review instructions][review], [Git diff semantics][git-diff]

The following are options, not decisions:

1. **Explicit checkpoint commits before review.** Arrange for the implementation snapshot to be committed before invoking the unmodified reviewer. Any subsequent correction needs another committed snapshot and appropriate review. This preserves the review command but adds a pre-review commit convention; the final commit step may be a no-op when nothing changed. Record this interpretation rather than pretending upstream specifies it.
2. **An approved review adaptation for working-tree changes.** Change the review contract to compare the intended baseline with the complete candidate tree, explicitly including untracked additions. This changes upstream review semantics and requires a documented adapter or maintained fork.
3. **An isolated committed review snapshot.** Materialize the candidate in a separate review worktree/branch, so both reviewers see a committed `HEAD`, then commit the same tree on the working branch after review. This can preserve the working branch's ordering but adds snapshot identity and synchronization complexity; it is not supplied by either skill.

Whichever option is chosen, reviewers and final publication need an identifiable candidate tree, fixed baseline, and source spec. A changed tree after review is different evidence. One active issue worker can still fan out to two read-only reviewer conversations; that does not require two writers or two independently claimed tasks. These are architectural implications, not an implemented guarantee.

## Python, PHP, and TypeScript targets

The inspected skills do not restrict target language; TypeScript examples illustrate testing behavior, not a requirement to write TypeScript. Their generic check instructions need a repository-specific execution contract. Candidate examples below are capabilities, not mandatory dependencies or selected project defaults.

| Target | Example evidence-backed capabilities | Configuration still needed |
| --- | --- | --- |
| Python | pytest can run a file, selected test, or suite; mypy can check configured files/directories. | Python version, environment/bootstrap command, actual test/type-check tools, targeted/full commands and services. |
| PHP | PHPUnit can run one source file or configured suites; PHPStan provides static analysis. | PHP/extensions, Composer dependencies, project-selected test/analyzer versions and commands. |
| TypeScript | TypeScript `noEmit` permits type checking without output files. | Node/package manager, lockfile/bootstrap, selected test runner with targeted/full commands, TypeScript project configuration. |

Sources: [pytest invocation][pytest], [mypy invocation][mypy], [PHPUnit CLI][phpunit], [PHPStan getting started][phpstan], [TypeScript noEmit][typescript].

Recommendation: keep the agent's Python/uv environment separate from the target project's toolchain contract. Record working directory, bootstrap, targeted tests, full tests, type checks, and required services per supported repository. Missing checks need an explicit policy; absence of an installed checker is not a successful check. Running language-specific commands was outside this research ticket.

## Decisions still needed

1. Which review/commit-order option is the MVP contract?
2. What exact human-authored evidence establishes approved TDD seams, and when must that approval be renewed?
3. Where does each repository declare its bootstrap/check commands and the policy for missing or unavailable checks?
4. What conditions in the two review reports block PR publication, and how are required fixes/reviews bounded? Upstream reports findings but does not define a PR acceptance gate.

These questions remain for planning. This research resolves what upstream requires and where custom policy must be explicit, without selecting those policies.

[implement]: https://github.com/mattpocock/skills/blob/3cca18b368ae95cdbdebbff572ccafa662551015/skills/engineering/implement/SKILL.md
[implement-metadata]: https://github.com/mattpocock/skills/blob/3cca18b368ae95cdbdebbff572ccafa662551015/skills/engineering/implement/agents/openai.yaml
[tdd]: https://github.com/mattpocock/skills/blob/3cca18b368ae95cdbdebbff572ccafa662551015/skills/engineering/tdd/SKILL.md
[tests]: https://github.com/mattpocock/skills/blob/3cca18b368ae95cdbdebbff572ccafa662551015/skills/engineering/tdd/tests.md
[mocking]: https://github.com/mattpocock/skills/blob/3cca18b368ae95cdbdebbff572ccafa662551015/skills/engineering/tdd/mocking.md
[review]: https://github.com/mattpocock/skills/blob/3cca18b368ae95cdbdebbff572ccafa662551015/skills/engineering/code-review/SKILL.md
[design]: https://github.com/mattpocock/skills/blob/3cca18b368ae95cdbdebbff572ccafa662551015/skills/engineering/codebase-design/SKILL.md
[deepening]: https://github.com/mattpocock/skills/blob/3cca18b368ae95cdbdebbff572ccafa662551015/skills/engineering/codebase-design/DEEPENING.md
[design-twice]: https://github.com/mattpocock/skills/blob/3cca18b368ae95cdbdebbff572ccafa662551015/skills/engineering/codebase-design/DESIGN-IT-TWICE.md
[specification]: https://agentskills.io/specification
[client]: https://agentskills.io/client-implementation/adding-skills-support
[git-diff]: https://git-scm.com/docs/git-diff
[pytest]: https://docs.pytest.org/en/stable/how-to/usage.html
[mypy]: https://mypy.readthedocs.io/en/stable/running_mypy.html
[phpunit]: https://docs.phpunit.de/en/12.5/textui.html
[phpstan]: https://phpstan.org/user-guide/getting-started
[typescript]: https://www.typescriptlang.org/tsconfig/noEmit.html
