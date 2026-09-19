# What control surface do native Codex and Claude Code workers expose to a host runtime?

Research for [issue #67](https://github.com/lbacik/coding-agent/issues/67), part of the planning map
[#57](https://github.com/lbacik/coding-agent/issues/57). Blocks
[#68](https://github.com/lbacik/coding-agent/issues/68).

This document establishes facts only. It decides nothing about scope, compliance classes or whether
native workers are supported; that is #68's decision. Where a mechanism does not exist, this document
says so and stops there rather than proposing a substitute.

## What was being tested

The execution contract settled by [#60](https://github.com/lbacik/coding-agent/issues/60)–[#63](https://github.com/lbacik/coding-agent/issues/63)
and recorded in [ADR 0014](../adr/0014-completion-is-declared-and-substantiated-never-inferred.md)
rests on three things the runtime imposes on the model's loop because it *owns* that loop:

1. a **forced first tool call** — `activate_implement` via provider-level `tool_choice` on every fresh
   work node;
2. an **evidence directory the model cannot write**, so `test_targeted` output is Criterion Evidence
   rather than the model's account of a test run;
3. **`report_completion` as the only non-exceptional exit**, so a turn without tool calls never by
   itself ends the implementation stage.

A native worker runs its own loop. The question is what a host embedding one can impose.

Subjects, both installed locally and exercised directly:

- **Codex CLI** `codex-cli 0.155.1` (`/opt/homebrew/bin/codex`), macOS arm64.
- **Claude Code** `2.1.278` (`/Users/lukasz/.local/bin/claude`) plus
  `@anthropic-ai/claude-agent-sdk@0.3.278`.

Primary sources only: the binaries' own `--help` output, a JSON schema the Codex binary generates
itself, the Agent SDK's published `.d.ts`, live runs of `codex exec`, and vendor documentation
fetched as raw markdown. No blog posts and no recollection.

## Verdict

| # | Property the contract needs | Codex CLI 0.155.1 | Claude Code 2.1.278 |
| --- | --- | --- | --- |
| 1 | Force the model's **first tool call** | **No mechanism.** No `tool_choice` equivalent in the CLI, the config reference or the generated protocol schema | **No mechanism.** No `tool_choice` equivalent in `--help`, the binary's flag strings or SDK `Options` |
| 1′ | Guarantee no *other* tool **executes** first | **Yes**, OS/runtime-enforced via a `PreToolUse` deny hook (verified by running it) | **Yes** for execution, via `permissions.deny` / `PreToolUse` deny — **except `EndConversation`, which is documented as unblockable** |
| 2 | An evidence directory the model **cannot write** | **Yes, guaranteed.** OS-enforced via a permission profile's `deny` (verified: `EPERM` from shell *and* from a separately spawned `python3`) | **Yes, guaranteed**, but only by combining two layers: OS sandbox `filesystem.denyWrite` for shell, `Edit(//…/**)` deny for the built-in file tools |
| 2′ | Same via the legacy sandbox keys | **No.** `sandbox_workspace_write` has no deny primitive, only `writable_roots` | **No.** `Write(path)` rules are accepted and *never consulted*; Bash rules are documented as "not a security boundary" |
| 3 | Distinguish a **declared** completion from a silent stop, from the worker's own reporting | **No.** `turn.completed` carries `usage` and nothing else | **No.** Both report `terminal_reason: "completed"` |
| 3′ | Distinguish a **limit** or an **error** exit | Error: yes (`turn.failed`). Limit: **no such limit exists** | **Yes**, fully — 19-value `terminal_reason` union separates limits, errors, interrupts, hook stops |
| 3″ | Refuse a silent stop and force continuation | **Yes**, `Stop` hook `{"decision":"block"}` (verified live); host chooses its own cap | **Yes**, `Stop` hook `{"decision":"block"}` — but **capped by the product at 8 consecutive blocks** |
| 3‴ | A turn/iteration ceiling | **Does not exist.** No `--max-turns`; host must impose a wall-clock timeout | **Yes**, `--max-turns` and `--max-budget-usd`, each with its own terminal reason |
| — | Usage reporting | Tokens incl. cached; **no cost field** | Tokens, cache, per-model breakdown, `total_cost_usd` (an estimate) |
| — | Tool calls in the machine-readable stream | Complete, with arguments and results | Complete, with arguments and results |

The one-line answer: **guarantee 1 is unavailable on both workers and cannot be reconstructed; guarantee 2
is available on both and is genuinely OS-enforced; guarantee 3 splits — neither worker reports the
distinction ADR 0014 needs, but both let a host refuse a silent stop, Codex without a product-imposed
cap and Claude Code with a hard one at 8.**

## Codex CLI

Subject: `codex-cli 0.155.1` at `/opt/homebrew/bin/codex`. Canonical documentation has moved:
`github.com/openai/codex/docs/config.md` is now a stub linking to `developers.openai.com/codex/…`,
which 308-redirects to `learn.chatgpt.com/docs/…`. Raw markdown is served by appending `.md`. All
citations below are the post-redirect canonical URLs.

Two primary sources beyond the docs were used and are worth naming, because they settle questions the
prose leaves open:

- **The binary generates its own protocol schema.**
  `codex app-server generate-json-schema --out <DIR>` emits
  `codex_app_server_protocol.schemas.json` (697 KB), `ClientRequest.json` and
  `ServerNotification.json`. This is the host-facing protocol, written by the product.
- **`codex sandbox -C <dir> -P <profile> -- <cmd>`** applies the sandbox with **no model in the
  loop**, which isolates the OS boundary from any question of model cooperation.

### 1. Forced first tool call — the mechanism does not exist

There is no `tool_choice` equivalent anywhere on the host-facing surface.

- `codex --help` and `codex exec --help` (full output read) contain no `--tool-choice`,
  `--require-tool` or `--first-tool`. The only tool-adjacent flag is `--search`.
- Grepping every file of the **generated** app-server schema for `tool_choice` / `toolChoice` returns
  **zero matches**. The protocol a host speaks has no way to constrain tool selection.
- The [config reference](https://learn.chatgpt.com/docs/config-file/config-reference) has no such key.
- `mcp_servers.<id>.required` is unrelated — it means "make startup fail if this enabled server can't
  initialize" ([extend/mcp](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)), i.e. it
  constrains server startup, not tool invocation.

**What does exist is gating, and gating is a different property.** A `PreToolUse` hook can deny a
tool call before it executes, and this was verified by running it, not read off a page. With a
`[[hooks.PreToolUse]]` command hook returning
`{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny",…}}` and the prompt
"Create a file named hello.txt containing the word HI, using the shell":

- the hook received `tool_name=Bash`, `tool_input={"command": "printf 'HI' > hello.txt"}` — name and
  arguments visible to the host;
- **no `command_execution` item appeared in the JSONL stream at all** and no `hello.txt` was created;
- the model's final message was "Couldn't create hello.txt: the environment blocked the shell write
  because implementation access isn't enabled" — the denial reason propagated back.

[hooks.md](https://learn.chatgpt.com/docs/hooks.md) states the coverage: "PreToolUse and PostToolUse
can observe more than shell and MCP calls. Most local function tools use the same hook path, so you
can match their tool name, inspect their JSON arguments, and, for PreToolUse, block or rewrite the
call." `updatedInput` can rewrite arguments, but only alongside `permissionDecision: "allow"`.

**Plainly: a host cannot guarantee that `activate_implement` is the first call the model emits. It
can guarantee that no other tool *executes* before that call happens. That is a strictly weaker
property than provider-level `tool_choice` — it constrains effects, not the model's first decision —
and prompt or `AGENTS.md` instruction remains likely, never guaranteed.**

### 2. An evidence directory the model cannot write — guaranteed, and verified

**The mechanism is permission profiles, not the legacy sandbox keys.**
[permissions.md](https://learn.chatgpt.com/docs/permissions.md) defines three filesystem access
levels — `read`, `write`, and `deny` ("Denies both reads and writes under the path") — with
"deny takes precedence over write, and write takes precedence over read", and narrower rules winning:
"Inside an active profile, narrower deny rules stay in force even when a broader path is readable or
writable". A profile is selected with `default_permissions = "<name>"`, or one of the built-ins
`:read-only`, `:workspace`, `:danger-full-access`.

The legacy keys cannot do this. `sandbox_mode` plus `[sandbox_workspace_write]`
(`writable_roots`, `network_access`, `exclude_tmpdir_env_var`, `exclude_slash_tmp`) has **no deny or
exclusion primitive at all** — it can only *add* writable roots, so it cannot carve an unwritable hole
inside the workspace. And the two systems do not compose: "Permission profiles do not compose with the
older sandbox settings. Configure either `default_permissions` and `[permissions]`, or `sandbox_mode` /
`sandbox_workspace_write`, but not both."

**Empirical verification.** With

```toml
default_permissions = "hostrt"
[permissions.hostrt.filesystem]
"/" = "read"
"<W>" = "write"
"<W>/evidence" = "deny"
```

and `codex sandbox -C <W> -P hostrt -- <cmd>` (no model involved):

| Test | Command | Result |
| --- | --- | --- |
| A | `sh -c "echo IN > <W>/work/x.txt"` | allowed |
| B | `sh -c "echo EV > <W>/evidence/e.txt"` | `Operation not permitted` |
| C | `sh -c "cat <W>/evidence/seed.txt"` | `Operation not permitted` (deny blocks reads too) |
| D | `python3 -c "open('<W>/evidence/py.txt','w')"` | `PermissionError [Errno 1] Operation not permitted` |
| E | `sh -c "echo X > $HOME/codex_escape_test.txt"` | `Operation not permitted`; file absent afterwards |
| F | `curl -m 8 https://example.com` | blocked (network off by default) |

Test D is the decisive one: `EPERM` raised at the syscall level inside a **separately spawned**
`/usr/bin/python3` is kernel enforcement (Seatbelt on macOS), not a convention the model is asked to
respect. The same profile was then run through the real agent loop with a prompt explicitly asking the
model to try python, tee, cp and mv; the model reported it could not write and `evidence/` was
unchanged.

[sandboxing.md](https://learn.chatgpt.com/docs/sandboxing.md) states the coverage: "The sandbox applies
to spawned commands, not just to built-in file operations. If the agent runs tools like `git`, package
managers, or test runners, those commands inherit the same sandbox boundaries."

**Escape hatches, all host-controlled at launch:** `--sandbox danger-full-access`,
`--dangerously-bypass-approvals-and-sandbox` ("Skip all confirmation prompts and execute commands
without sandboxing. EXTREMELY DANGEROUS", `codex exec --help`), and `--add-dir <DIR>` ("Additional
directories that should be writable alongside the primary workspace"). Approvals do not themselves
bypass the sandbox; with `--ask-for-approval never` there is no human to escalate to —
"Execution failures are immediately returned to the model" (`codex --help`). An enterprise
`requirements.toml` can pin `allowed_permission_profiles`, where "omitted profiles are denied,
including omitted built-ins and profiles added in future Codex versions."

**Plainly: an evidence directory the model cannot write is available on Codex CLI as a real,
OS-enforced guarantee, provided the host uses a permission profile rather than the legacy keys and
passes neither `--add-dir` for that path nor the bypass flags.**

### 3. `report_completion` as the only non-exceptional exit — the stream cannot tell, the hook can refuse

**From the `codex exec --json` stream alone, the distinction ADR 0014 needs is not reported.**
The event types are `thread.started`, `turn.started`, `turn.completed`, `turn.failed`, `item.started`,
`item.completed`, `item.updated`, `error`
([non-interactive-mode.md](https://learn.chatgpt.com/docs/non-interactive-mode.md)). A ground-truth run
ended with:

```
{"type":"turn.completed","usage":{"input_tokens":32531,"cached_input_tokens":24064,
 "cache_write_input_tokens":0,"output_tokens":63,"reasoning_output_tokens":0}}
```

`turn.completed` carries **`usage` and nothing else** — no `reason`, no `stop_reason`, no
`finish_reason`. A declared completion and a model that simply stopped emit the identical event. The
app-server `TurnStatus` enum (`completed`, `interrupted`, `failed`, `inProgress`) confirms it: it
separates an explicit interruption, but not "declared" from "just stopped".

The sharpest illustration: in the `PreToolUse`-deny test, where the model was blocked and achieved
nothing, the run **still** emitted `turn.completed` with exit code 0. **`turn.completed` means "the
loop stopped without erroring", not "the task succeeded"** — which is precisely the conflation ADR 0014
removes.

- **Errors are distinguishable**: `turn.failed` with `error.message`, exit code 1 (observed on a 401).
  App-server adds `TurnError { message, additionalDetails, codexErrorInfo, misalignment }`, "only
  populated when the Turn's status is failed".
- **A turn/iteration limit does not exist.** `codex exec --help` (full output read) has no
  `--max-turns`; the config reference has no turn or iteration key; grepping the generated schemas for
  `max_turns`/`maxTurns` returns zero matches. `interrupted` corresponds to an explicit
  `TurnInterruptParams`, not an automatic cap. A host must impose its own wall-clock timeout.

**A `Stop` hook can refuse the stop and force continuation.** [hooks.md](https://learn.chatgpt.com/docs/hooks.md):
"To keep Codex going, return `{"decision": "block", "reason": "..."}`" — and, importantly for how the
continuation is shaped, "`decision: "block"` doesn't reject the turn. Instead, it tells Codex to
continue and automatically creates a new continuation prompt that acts as a new user prompt, using your
`reason` as that prompt text." Exit code 2 with the reason on stderr is equivalent.

Verified live. With a `[[hooks.Stop]]` command hook returning
`{"decision":"block","reason":"HOST_RUNTIME: you did not call report_completion. …"}` on the first
invocation and `{}` on the second, against the prompt "Say hello. Do nothing else.", the hook fired
twice and the model produced a second agent message responding to the injected text — the
host-authored continuation reached the model and the loop resumed. The captured `Stop` payload carries
`session_id`, `turn_id`, `transcript_path`, `cwd`, `model`, `permission_mode`, `last_assistant_message`
and `stop_hook_active`. The second invocation differed only in `"stop_hook_active": true`; that field
is the host's loop guard, and **the cap is the host's to choose — Codex does not impose one.**

Two consequences worth recording:

- The forced continuation happened **inside the same `turn_id`**. The JSONL stream emitted exactly one
  `turn.started` and one `turn.completed` for the whole thing. **The continuation is invisible in the
  exec stream; only the hook sees it.** A host that reasons about work nodes from the stream alone will
  not see its own continuations.
- Because the stream cannot distinguish a declared completion, the `Stop` hook is not an optimisation
  here — it is the only place the distinction can be made at all.

**Other exit-observation channels.** `notify` ([config-advanced.md](https://learn.chatgpt.com/docs/config-file/config-advanced.md))
invokes a program with a single JSON argv (`type`, only `agent-turn-complete` supported; `thread-id`,
`turn-id`, `cwd`, `input-messages`, `last-assistant-message`) — **observation only, it cannot force
continuation**. `-o, --output-last-message <FILE>` writes the final message. `--output-schema <FILE>`
constrains the shape of the final response. `codex exec resume <SESSION_ID> [PROMPT]` / `--last` offers
host-driven continuation at the cost of a new process.

**Plainly: Codex CLI does not report whether its loop ended by a declaration or by silence. A host can
refuse a silent stop through the `Stop` hook and inject a runtime-composed continuation prompt, with no
product-imposed cap, which reconstructs the *effect* of ADR 0014's continuation budget — but the
classification must be the host's own bookkeeping, kept in the hook, not read off the worker's output.**

### 4. What Codex reports

**Usage — tokens yes, cost no.** `turn.completed.usage` carries `input_tokens`, `cached_input_tokens`,
`cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`. There is **no cost or pricing
field anywhere**. The app-server protocol is richer: `TokenUsageBreakdown` adds `totalTokens`, and
`ThreadTokenUsage { last, total, modelContextWindow }` is streamed via `thread/tokenUsage/updated` —
`totalTokens` and `modelContextWindow` exist there but **not** in the exec JSONL `usage` object.

**Tool calls — complete, with arguments and results.** Observed in the exec stream: `command_execution`
items with `command` (`"/bin/zsh -lc 'cat a.txt'"`), `aggregated_output` (`"hello\n"`), `exit_code`,
`status` moving `in_progress` → `completed`, delivered as `item.started` then `item.completed`. The
generated `ThreadItem` schema enumerates `userMessage`, `hookPrompt`, `agentMessage`,
`functionCallOutput`, `plan`, `reasoning`, `commandExecution`, `fileChange`, `mcpToolCall`,
`dynamicToolCall`, `collabAgentToolCall`, `subAgentActivity`, `webSearch`, `imageView`, `sleep`,
`imageGeneration`, `enteredReviewMode`, `exitedReviewMode`, `contextCompaction`. `mcpToolCall` carries
`{server, tool, arguments, result, status, error, durationMs, readOnlyHint, …}`, so MCP tool arguments
and results are in the machine-readable stream.

**Termination reason — partial.** `turn.failed` + `error.message` for failures; `turn.completed` with
no reason field for everything else.

**Operational gotchas that cost the investigation a run each**, recorded because a host embedding
Codex will hit them:

- **Hooks require persisted trust.** `--dangerously-bypass-hook-trust` ("Run enabled hooks without
  requiring persisted hook trust for this invocation", `codex --help`) injects two `item.completed`
  events of `type: "error"` into the stream. Harmless, but a host that keys on `type == "error"` will
  see false positives. Persist hook trust instead.
- **`codex exec` blocks on stdin** when stdin is neither a TTY nor closed (stderr:
  `Reading additional input from stdin...`); it hung indefinitely in a backgrounded run. Redirect
  `< /dev/null`.
- A custom `CODEX_HOME` needs `auth.json` copied in, or every request 401s.

## Claude Code

Subject: `claude` CLI **2.1.278** (`/Users/lukasz/.local/bin/claude`, resolving to
`/Users/lukasz/.local/share/claude/versions/2.1.278`) in print/headless mode, plus
`@anthropic-ai/claude-agent-sdk` **0.3.278** (published `sdk.d.ts` read from the npm tarball).

Documentation note: `docs.claude.com/en/docs/claude-code/*` now 301-redirects to
`code.claude.com/docs/en/*`; the page index is `https://code.claude.com/docs/llms.txt`. All
citations below are the post-redirect canonical URLs, fetched as raw Mintlify `.md` source.

The sentence that frames all three answers is the product's own:

> "Permission rules are enforced by Claude Code, not by the model. Instructions in your prompt or
> `CLAUDE.md` shape what Claude tries to do, but they don't change what Claude Code allows."
> — [permissions.md](https://code.claude.com/docs/en/permissions.md)

A host can constrain what Claude Code will *let* the model do. It cannot constrain what the model
*chooses to emit*. That asymmetry is exactly where requirement 1 fails and requirement 2 succeeds.

### 1. Forced first tool call — the mechanism does not exist

There is no `tool_choice` equivalent on any host-facing surface.

- `claude --help` (v2.1.278, full output read) exposes no `--tool-choice`, `--force-tool` or
  `--first-tool` option of any name.
- `strings -a /Users/lukasz/.local/share/claude/versions/2.1.278` does not contain a
  `--tool-choice` flag string, while it does contain the undocumented-but-real `--max-turns`,
  `--sandbox`, `--permission-prompt-tool` and `--max-budget-usd`, so the absence is a finding and
  not a failed grep.
- `@anthropic-ai/claude-agent-sdk@0.3.278`, `package/sdk.d.ts`: zero occurrences of `toolChoice`.
  The `Options` type carries `allowedTools`, `disallowedTools`, `permissionMode`, `canUseTool`,
  `hooks`, `maxTurns`, `additionalDirectories` — and no tool-choice field.

The binary does contain an internal `tool_choice` construction for its own small single-shot model
calls (observable only through the log string `tool_choice {type:'tool', name:'${…}'} demoted to
auto: extended thinking is active`). It is reachable by no flag and no SDK option, and it is
auto-demoted to `auto` under extended thinking. Which internal callers use it is **unverified**; it
is not a host surface either way.

**Hooks do not close the gap.** Source: [hooks.md](https://code.claude.com/docs/en/hooks.md).

| Hook | Can it force a tool *call*? | What it actually does |
| --- | --- | --- |
| `SessionStart` | No | Context only — `additionalContext`, `initialUserMessage`, `watchPaths`, `sessionTitle`, `reloadSkills`. The docs state it has "no blocking or decision control". |
| `UserPromptSubmit` | No | `decision: "block"` kills the prompt; `additionalContext` injects text. The docs state it "can't replace the prompt; it only injects `additionalContext` alongside it". |
| `PreToolUse` | No | Runs "after Claude creates tool parameters" — i.e. only once the model has already chosen a tool. `updatedInput` can rewrite a call's arguments; it cannot invent a call or change which tool was invoked. |

The strongest primitive is `SessionStart` → `initialUserMessage` ("String used as the first user
message of the session. Applies in non-interactive mode with the `-p` flag, where it becomes the
first turn even if no prompt is provided"). That is text injection. It makes the desired first call
**likely**. It guarantees nothing.

**"Deny everything except the target tool" is buildable and still not a guarantee.** Tool-name
wildcards in `permissions.deny` (`"*"`, `"mcp__*"`) and a `PreToolUse` hook returning
`permissionDecision: "deny"` both exist. Three cited reasons they do not amount to a forced first
call:

1. Nothing prevents a **text-only assistant turn**. The model can simply not call a tool. The only
   lever against that is a `Stop` hook, which is post-hoc and capped (§3).
2. **`EndConversation` cannot be blocked.**
   [tools-reference.md](https://code.claude.com/docs/en/tools-reference.md): "The tool never prompts
   for permission, and PreToolUse hooks don't run for it. While any other tool remains, you can't
   block it either: deny and ask rules naming `EndConversation` have no effect, and neither
   `--disallowedTools` nor a `--tools` list can remove it." A tool-shaped exit always exists.
3. A denial is returned to the model as text (`permissionDecisionReason`, "for `"deny"`, shown to
   Claude"). It is a nudge in the transcript, not a control-flow constraint.

**What the host does get is detection, not prevention.** The first `tool_use` block of a run is
visible in `--output-format stream-json`, and `PreToolUse`/`PostToolUse` fire on it, so a host can
verify after the fact that the first call was the designated one and fail the run otherwise. There
is also a mid-loop control point: a `PreToolUse` hook may return `permissionDecision: "defer"`,
which in `-p` mode ends the process with `stop_reason: "tool_deferred"` plus a `deferred_tool_use`
payload for `--resume` — but only after the model has already picked a tool, and "`defer` only
works when Claude makes a single tool call in the turn" (hooks.md).

**Plainly: a host cannot guarantee that a designated tool runs before the model's first free turn.
It can make it likely by injection, prevent everything else from *executing*, and reject the run
afterwards. Enforcement by rejection, never by construction.**

### 2. An evidence directory the model cannot write — achievable, but only at the OS layer

Claude Code documents two enforcement layers with explicitly different coverage:

> "Permission rules control which tools Claude Code can use and are evaluated before any tool runs.
> They apply to every tool… Sandboxing provides OS-level enforcement that restricts what shell
> commands can access at the filesystem and network level. It applies only to Bash, PowerShell, and
> Monitor commands and their child processes. … The operating system enforces the sandbox boundary
> on the running process, so it holds regardless of what the model chose to run and even if an
> allowed command does more than its name suggests."
> — [sandboxing.md](https://code.claude.com/docs/en/sandboxing.md)

**Permission rules — a guarantee for the built-in file tools.**
[permissions.md](https://code.claude.com/docs/en/permissions.md):

- Path rules are consulted **only** for `Edit(path)` and `Read(path)`. Verbatim: "If you write a
  path rule for `Write`, `NotebookEdit`, `Glob`, or the legacy `MultiEdit` tool instead, Claude Code
  accepts the rule but never consults it, and warns at startup." A `Write({evidence_dir}/**)` deny
  is a silent no-op; the correct rule is `Edit(//abs/evidence/**)`.
- An `Edit` deny covers Write/Edit/MultiEdit; a `Read` deny also blocks Edit and Write on the same
  path "including creating a new file there". `NotebookEdit` is not covered by a `Read` deny and
  needs its own `Edit` deny.
- Deny beats everything: "An allow rule can't carve an exception out of a deny rule"; deny rules
  block "in every mode, including `bypassPermissions`"
  ([permission-modes.md](https://code.claude.com/docs/en/permission-modes.md)); and "Claude Code
  evaluates deny and ask rules regardless of what a PreToolUse hook returns".
- Symlinks match if either the link or its target matches.

**Permission rules — explicitly *not* a boundary for shell.** The docs say so in the product's own
words, in a section titled "What a Bash rule doesn't match": `Bash(rm *)` stops `rm -rf build/` but
does not stop `/bin/rm -rf build/` or `bash -c 'rm -rf build/'`; and "A Bash rule matches the command
text Claude writes… it isn't a security boundary around the program." Redirect targets (`>`, `>>`,
`2>`, `<`), `tee` targets, compound commands, subshells and `$()` *are* parsed and checked against
Edit/Read rules — but "they don't apply … to arbitrary subprocesses that read or write files
indirectly, like a Python or Node script that opens files itself. For OS-level enforcement that
blocks all processes from accessing a path, enable the sandbox." So `python -c "open('…','w')"` or a
nested `sh -c` writing into `{evidence_dir}` is outside the analysis.

**The OS sandbox is the real guarantee.** [sandboxing.md](https://code.claude.com/docs/en/sandboxing.md):
Seatbelt on macOS, bubblewrap on Linux/WSL2; **native Windows unsupported**. The relevant key is
`sandbox.filesystem.denyWrite`. Restrictions "apply to all scripts, programs, and subprocesses
spawned by commands". It can be delivered without touching a settings file:
`claude --settings '{"sandbox": {"enabled": true, "allowUnsandboxedCommands": false}}'` (that exact
command is in the docs).

Four caveats a host must close, all cited from the same page:

1. **It fails open by default**: "if the sandbox cannot start because dependencies are missing or
   the platform is unsupported, Claude Code shows a warning and runs commands without sandboxing.
   To make this a hard failure instead, set `sandbox.failIfUnavailable` to `true`."
2. **The escape hatch is on by default**: `dangerouslyDisableSandbox` lets the model retry a blocked
   command outside the sandbox. `allowUnsandboxedCommands: false` disables it — and
   `sandbox.excludedCommands` must then also be audited, since listed commands still run unsandboxed.
3. `sandbox.filesystem.disabled: true` turns the layer off, but is honored only from user, managed
   and `--settings` sources — "Project settings in `.claude/settings.json` and
   `.claude/settings.local.json` can't, so a checked-out project can't switch filesystem isolation
   off." A host should therefore deliver the config through managed settings or `--settings`.
4. **Scope**: "Built-in file tools: Read, Edit, and Write use the permission system directly rather
   than running through the sandbox." Both layers are required, not either.

Other mechanisms asked about, briefly: permission **modes** never make a path unwritable, they only
decide whether you are prompted (`--permission-mode dontAsk` auto-denies everything that would
prompt). `--allowedTools` does **not** restrict the model to those tools — the SDK reference states
"This does not restrict Claude to only these tools"; `--tools` shrinks the built-in surface and
`--disallowedTools "mcp__*"` is needed for MCP. A `PreToolUse` deny hook sees only command text and
the docs themselves say "use the permission system rather than a hook to enforce a hard allow or
deny". The SDK `canUseTool` callback has a documented hole: "Auto-approved tools never reach
`canUseTool`… so permission checks you put there are silently bypassed for that tool"
([agent-sdk/permissions.md](https://code.claude.com/docs/en/agent-sdk/permissions.md)).
`additionalDirectories`/`--add-dir` only ever *grant* access. `--restricted` (in `--help`) removes
Bash/PowerShell/REPL/WebFetch, ignores user/project/local settings files and "refuses
bypassPermissions" — the strongest single flag for a no-shell worker, though its semantics beyond the
help text are **unverified**.

**Plainly: an evidence directory the model cannot write IS achievable on Claude Code, as a real
OS-enforced guarantee, on macOS/Linux/WSL2 — `sandbox.enabled` + `filesystem.denyWrite` +
`failIfUnavailable: true` + `allowUnsandboxedCommands: false` + empty `excludedCommands`, delivered
via managed settings or `--settings`, combined with an `Edit(//…/evidence/**)` deny for the built-in
file tools. Permission rules alone are a guarantee for the file tools and best-effort for shell.**

### 3. A designated completion tool as the only non-exceptional exit — classifiable, not enforceable

**Classifying the exit is a genuine guarantee.** The final `result` message
(`--output-format json`, or the last line of `stream-json`; `SDKResultMessage` in TypeScript,
`ResultMessage` in Python) carries `terminal_reason`, documented as "Why the query loop terminated".
Its exhaustive union, read from `sdk.d.ts` of `@anthropic-ai/claude-agent-sdk@0.3.278`:

```
'blocking_limit' | 'rapid_refill_breaker' | 'prompt_too_long' | 'image_error' | 'model_error'
| 'api_error' | 'malformed_tool_use_exhausted' | 'aborted_streaming' | 'aborted_tools'
| 'stop_hook_prevented' | 'hook_stopped' | 'tool_deferred' | 'max_turns' | 'background_requested'
| 'completed' | 'budget_exhausted' | 'structured_output_retry_exhausted'
| 'tool_deferred_unavailable' | 'turn_setup_failed'
```

`subtype` is `"success"` or one of `error_max_turns`, `error_during_execution`,
`error_max_budget_usd`, `error_max_structured_output_retries`. The
[Python SDK reference](https://code.claude.com/docs/en/agent-sdk/python.md) warns that the order of
inspection matters: "To tell failures apart, check `terminal_reason` before `subtype`. When the
final request fails, such as on an API error, Claude Code reports `subtype` `"success"` with the
cause in `terminal_reason`."

Against the four cases the contract needs to tell apart:

| Case | Signal | Guaranteed? |
| --- | --- | --- |
| Model **declared** completion by calling the designated tool | Not a `result` field. Derived from the `tool_use` block in `stream-json`, a `PostToolUse` hook, or a receipt the tool itself writes | Detection is reliable, but it is the host's own bookkeeping |
| Model **stopped silently** with text | `terminal_reason: "completed"`, `subtype: "success"`, `is_error: false` | Yes |
| **Limit** reached | `max_turns` / `error_max_turns`; `budget_exhausted` / `error_max_budget_usd`; also `prompt_too_long`, `blocking_limit`, `rapid_refill_breaker` | Yes |
| **Error or interrupt** | `api_error`, `model_error`, `turn_setup_failed`, `malformed_tool_use_exhausted`, `image_error`; interrupts as `aborted_streaming` / `aborted_tools` | Yes |

Note the critical detail for ADR 0014: a **declared** completion and a **silent** stop both report
`terminal_reason: "completed"`. The runtime-visible field does not separate them; the host must add
its own record of the tool call. That is the same separation ADR 0014 makes, achieved by host-side
bookkeeping rather than by the worker's own reporting. `terminal_reason` is `None` on older CLI
versions, on local slash-command results, and "on synthesized error results emitted when the session
fails fatally", so it must be read defensively.

Process exit codes are coarse: 0 on success, non-zero on failure; SIGTERM yields 143 and "Claude
Code leaves the turn that was in progress unfinished and records no result for it"
([headless.md](https://code.claude.com/docs/en/headless.md)). The `result` message, not the exit
code, is the authoritative signal.

**Forcing the exit is not a guarantee.** The `Stop` hook (hooks.md) fires "when Claude finishes
responding" and `{"decision": "block", "reason": "…"}` "prevents Claude from stopping". It works in
`-p` and in the SDK, and its input carries `last_assistant_message`, so a host can refuse a silent
stop and push the model back into the loop with a runtime-composed reason — structurally the same
move as ADR 0014's deterministic continuations. Its documented limits:

- **Capped**: "Claude Code overrides the hook and ends the turn after 8 consecutive blocks."
- **Post-hoc**: the turn has already ended when the hook runs.
- **Does not fire on user interrupt**; API errors raise `StopFailure` instead, which has no decision
  control at all.
- Timeout (v2.1.273+) counts as no decision: "The agent or subagent stops as if that callback had
  allowed it."

So the realistic shape on Claude Code is: a `Stop` hook demands the designated tool, the runtime
overrides it after at most 8 blocks, and the host then classifies the run from `terminal_reason`
(`stop_hook_prevented` / `hook_stopped` versus `completed`) plus its own receipt of the tool call.
Enforcement by detection and rejection, not by construction. The exact difference between
`hook_stopped` and `stop_hook_prevented` is **unverified** — both are in the union and neither is
defined in the docs or in the `.d.ts` JSDoc.

### 4. What Claude Code reports

**Usage.** The result's `usage` object carries `input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens`, but the TypeScript reference scopes it to
"main agent loop only. Excludes subagent and auxiliary model calls… Prefer `modelUsage` for
token/cost accounting". `modelUsage` is a per-model map with `inputTokens`, `outputTokens`,
`thinkingTokens`, `cacheReadInputTokens`, `cacheCreationInputTokens`, `webSearchRequests`, `costUSD`,
`contextWindow`, `maxOutputTokens`, `canonicalModel`, `provider`, and covers the main loop, subagents
and internal calls such as compaction. `total_cost_usd` is present and explicitly "an estimate, not
a billing statement". Timing: `duration_ms`, `duration_api_ms`, `num_turns`, plus `ttft_ms` and
friends on the success arm. After a session crash the final result is `error_during_execution` and
its cost fields may be zeroed.

**Tool calls.** Fully surfaced. `--output-format stream-json` emits an assistant message per
completed content block, and its `message` is a `BetaMessage` whose `content` carries the `tool_use`
blocks with their inputs verbatim; `tool_result` blocks arrive as user-role messages.
`--include-partial-messages` additionally streams raw `content_block_delta` frames for tool input
JSON. Subagent tool calls appear with `parent_tool_use_id` set. `--include-hook-events` adds
`hook_started`/`hook_progress`/`hook_response`. The result also carries
`permission_denials: {tool_name, tool_use_id, tool_input}[]`, which the `.d.ts` marks as the
authoritative record — while noting it does not cover PreToolUse hook denies or file-tool calls
refused by a path-scoped deny rule. A `system/init` event opens the stream with the model, tool list,
`mcp_servers[]` and `mcp_server_errors[]` — a usable gate for "the MCP server providing the
activation tool actually loaded".

**Termination reason.** `terminal_reason` (19 values, above), then `subtype` (5 values), `is_error`,
and the model's own API `stop_reason` (`end_turn`, `max_tokens`, `refusal`, `tool_use`, plus
Claude-Code-specific `tool_deferred` / `tool_deferred_unavailable`).

**Structured final answer.** `--json-schema` with `--output-format json` validates the final answer
into `structured_output`. This constrains the shape of the answer, not the loop; it is not a
substitute for a completion tool, but it does make the worker's hand-back machine-readable.
## What this means for the three guarantees

The three requirements do not fail together. They fail, hold and half-hold in different ways, and the
differences are what #68 has to decide against.

**Guarantee 1 — the forced first tool call — is unavailable on both workers, and the gap is not of a
kind a host can close.** Neither product exposes a `tool_choice` equivalent, and this was established
negatively from three independent primary surfaces per worker (CLI `--help`, the vendor config or
settings reference, and a machine-readable interface description the vendor itself publishes — Codex's
generated protocol schema, Claude Code's SDK `.d.ts`). What both offer instead is **execution gating**:
deny every tool but the designated one, so nothing else *runs* first. That is a real, enforced
property, but it is a different property. Forcing the first call constrains what the model emits;
gating constrains what happens afterwards. A model can still open with a text-only turn and no tool
call at all, and on Claude Code it can additionally call `EndConversation`, which the documentation
states cannot be blocked by deny rules, `--disallowedTools`, a `--tools` list, or a `PreToolUse` hook.
On both workers, the only lever against a text-only opening is the `Stop` hook — that is, requirement 1
collapses into requirement 3.

**Guarantee 2 — the unwritable evidence directory — holds on both, and is the strongest result here.**
On both workers it is OS-level enforcement (Seatbelt on macOS; bubblewrap on Linux/WSL2 for both; the
native Windows sandbox on Codex, no Windows support on Claude Code), and on Codex this was confirmed
empirically down to an `EPERM` from a separately spawned interpreter. Two important differences:

- **Claude Code needs two layers, not one.** Its sandbox covers Bash/PowerShell/Monitor subprocesses
  only; "Read, Edit, and Write use the permission system directly rather than running through the
  sandbox". So the host must set both `sandbox.filesystem.denyWrite` *and* an `Edit(//…/evidence/**)`
  deny rule. Codex's permission profile covers both paths with one rule.
- **Both have a fail-open default that the host must close.** Claude Code "runs commands without
  sandboxing" if the sandbox cannot start unless `sandbox.failIfUnavailable` is set, and ships an
  escape hatch (`dangerouslyDisableSandbox`) that is on until `allowUnsandboxedCommands: false`. Codex
  ships `--add-dir`, `--sandbox danger-full-access` and
  `--dangerously-bypass-approvals-and-sandbox`. In both cases the hatches are host-controlled at
  launch, so this is a configuration obligation, not a hole.

A separate and easily-missed finding on both: the *obvious* rule is the one that silently does
nothing. On Claude Code a `Write(path)` deny "is accepted but never consulted" (an `Edit` rule is
required); on Codex the legacy `sandbox_workspace_write` block has no deny primitive at all and cannot
express the requirement.

**Guarantee 3 — declared completion as the only non-exceptional exit — is where the two workers
diverge, and where the sharpest negative result sits.** The negative result is common to both:
**neither worker reports the distinction ADR 0014 is built on.** Codex emits `turn.completed` carrying
`usage` and nothing else; Claude Code emits `terminal_reason: "completed"` in both cases. A model that
declared completion and a model that fell silent are the same observable event in the worker's own
output — exactly the conflation the pilot suffered, reappearing one level up. On both workers the host
must keep its own receipt of the tool call (from the tool-call stream, or from a `PostToolUse` hook, or
written by the tool itself) and join it to the termination event. The distinction is reconstructible;
it is not reported.

Around that shared negative the two differ materially:

- **Claude Code classifies the *other* exits far better.** Its 19-value `terminal_reason` union
  separates limits (`max_turns`, `budget_exhausted`, `prompt_too_long`, `blocking_limit`,
  `rapid_refill_breaker`), errors (`api_error`, `model_error`, `turn_setup_failed`,
  `malformed_tool_use_exhausted`), interrupts (`aborted_streaming`, `aborted_tools`) and hook-driven
  stops. Codex reports only `turn.failed` versus `turn.completed`, and has **no turn or budget ceiling
  at all** — no `--max-turns`, zero schema matches — so a host embedding Codex must supply its own
  wall-clock bound, and cannot distinguish "ran out of room" from "stopped".
- **Codex's refusal to stop is uncapped; Claude Code's is capped at 8.** Both expose a `Stop` hook
  whose `{"decision": "block", "reason": "…"}` pushes the model back into the loop with host-authored
  text — structurally the same move as ADR 0014's deterministic continuations, and on Codex documented
  as literally becoming "a new user prompt, using your `reason` as that prompt text". Codex leaves the
  cap to the host (`stop_hook_active` is informational). Claude Code states "Claude Code overrides the
  hook and ends the turn after 8 consecutive blocks", and its `Stop` hook does not fire on user
  interrupt (API errors raise `StopFailure`, which has no decision control) and treats a hook timeout
  as an allow. ADR 0014's budget of two continuations per work node fits inside 8, so the cap is not
  binding at today's budget — but it is a ceiling the host does not own.
- **On Codex the continuation is invisible in the stream.** Forced continuations happen inside the same
  `turn_id`; the exec JSONL shows one `turn.started` and one `turn.completed` regardless. A host that
  derives work-node structure from the stream will not see its own continuations.

In both cases the shape of the mechanism is the same, and it is not the shape the contract currently
has. The runtime-owned loop enforces by **construction**: the model cannot reach a state the contract
forbids. A native worker enforces by **gating and rejection**: the model may emit anything, the host
blocks the effects it does not want, refuses the stops it does not accept, and classifies the run
afterwards from evidence it kept itself. Whether that is a second compliance class or a reason to
declare native workers unsupported is [#68](https://github.com/lbacik/coding-agent/issues/68)'s
decision, not this document's.

## What this research does not establish

- It does not establish that the gating construction is *sufficient* for any Implementation Outcome.
  It establishes only which primitives exist and what each one enforces.
- It does not measure model behaviour. Nothing here says how often a model instructed to call
  `activate_implement` first actually does so on either worker.
- It was verified on **macOS arm64 only**. The Linux and Windows sandbox paths are documented but were
  not exercised.
- Version-pinned: Codex CLI 0.155.1, Claude Code 2.1.278, Agent SDK 0.3.278. Several of the load-bearing
  mechanisms (Claude Code's `terminal_reason`, `tee` redirect checking, hook timeout semantics) carry
  explicit "added in 2.1.2xx" notes, so this surface is moving.

## Flagged as unverified

- **Whether MCP server processes run inside or outside the Codex sandbox.** Not stated in the primary
  MCP documentation. This is load-bearing for the evidence-directory design, since the host-supplied
  tool that *writes* Criterion Evidence must be outside the deny. Test empirically before relying on
  either answer.
- **The exact Linux sandbox primitive on Codex.** The sandboxing page names bubblewrap (`bwrap`); it
  does not name Landlock or seccomp in the retrieved text. Only Seatbelt (macOS) and the native Windows
  sandbox are explicitly named.
- **Codex `exec` exit-code table.** Exit 0 was observed on `turn.completed` (including a blocked,
  unproductive run) and 1 on `turn.failed`; no document enumerates the full set.
- **Claude Code: the difference between `terminal_reason: "hook_stopped"` and `"stop_hook_prevented"`.**
  Both are in the published union; neither is defined in the documentation or the `.d.ts` JSDoc.
- **Claude Code `--sandbox`**: the flag string is present in the 2.1.278 binary but absent from `--help`
  and from the CLI reference. The documented route is `--settings '{"sandbox": …}'`.
- **Claude Code `--restricted`**: semantics beyond its `--help` text; no dedicated documentation page
  was found in the page index.
- **Claude Code `--max-turns`**: documented in the CLI reference and present as a flag string in the
  binary, but absent from `--help` in 2.1.278. Whether that is deliberate hiding or a regression is
  unknown.
- **Claude Code's internal `tool_choice`**: the binary constructs one for its own single-shot helper
  calls (observable only via a log string about demotion under extended thinking). Which internal
  callers use it is unknown; it is reachable by no flag or SDK option either way.

## Primary sources

**Codex CLI.** Commands run: `codex --version`, `codex --help`, `codex exec --help`,
`codex exec resume --help`, `codex mcp --help`, `codex app-server --help`, `codex exec-server --help`,
`codex sandbox --help`, `codex app-server generate-json-schema --out <DIR>`; four live
`codex exec --json` runs and five `codex sandbox -P` enforcement tests. Documentation:
[hooks.md](https://learn.chatgpt.com/docs/hooks.md),
[permissions.md](https://learn.chatgpt.com/docs/permissions.md),
[sandboxing.md](https://learn.chatgpt.com/docs/sandboxing.md),
[config-reference](https://learn.chatgpt.com/docs/config-file/config-reference),
[config-advanced.md](https://learn.chatgpt.com/docs/config-file/config-advanced.md),
[non-interactive-mode.md](https://learn.chatgpt.com/docs/non-interactive-mode.md),
[cli/reference.md](https://learn.chatgpt.com/docs/cli/reference.md),
[extend/mcp](https://learn.chatgpt.com/docs/extend/mcp?surface=cli),
[codex-sdk.md](https://learn.chatgpt.com/docs/codex-sdk.md).

**Claude Code.** Commands run: `claude --version`, `claude --help`, `claude mcp --help`,
`claude agents --help`; `strings -a` over the 2.1.278 binary; `npm view @anthropic-ai/claude-agent-sdk`
and `package/sdk.d.ts` read from its tarball (0.3.278). Documentation, all fetched as raw markdown from
the canonical `code.claude.com/docs/en/` host: [hooks](https://code.claude.com/docs/en/hooks.md),
[permissions](https://code.claude.com/docs/en/permissions.md),
[permission-modes](https://code.claude.com/docs/en/permission-modes.md),
[sandboxing](https://code.claude.com/docs/en/sandboxing.md),
[headless](https://code.claude.com/docs/en/headless.md),
[cli-reference](https://code.claude.com/docs/en/cli-reference.md),
[settings-reference](https://code.claude.com/docs/en/settings-reference.md),
[tools-reference](https://code.claude.com/docs/en/tools-reference.md),
[goal](https://code.claude.com/docs/en/goal.md),
[agent-sdk/typescript](https://code.claude.com/docs/en/agent-sdk/typescript.md),
[agent-sdk/python](https://code.claude.com/docs/en/agent-sdk/python.md),
[agent-sdk/permissions](https://code.claude.com/docs/en/agent-sdk/permissions.md),
[agent-sdk/hooks](https://code.claude.com/docs/en/agent-sdk/hooks.md),
[agent-sdk/agent-loop](https://code.claude.com/docs/en/agent-sdk/agent-loop.md),
[agent-sdk/streaming-output](https://code.claude.com/docs/en/agent-sdk/streaming-output.md),
[agent-sdk/cost-tracking](https://code.claude.com/docs/en/agent-sdk/cost-tracking.md).
