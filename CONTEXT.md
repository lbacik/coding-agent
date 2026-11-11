# Coding Agent

A containerised agent that works selected issues or pull requests from one configured GitHub repository through operator-defined Workflows. This glossary fixes the vocabulary of that work; it holds no implementation detail.

## Operator contract terms

The following terms are defined by the [operator contract glossary](https://github.com/lbacik/agent-operator-contract/blob/06ee46c0204f0fc8a2465827cc397bc235dedc26/CONTEXT.md) at pinned revision `06ee46c0204f0fc8a2465827cc397bc235dedc26` and are used here without redefinition: [Operator Capability](https://github.com/lbacik/agent-operator-contract/blob/06ee46c0204f0fc8a2465827cc397bc235dedc26/CONTEXT.md#control), [Intake](https://github.com/lbacik/agent-operator-contract/blob/06ee46c0204f0fc8a2465827cc397bc235dedc26/CONTEXT.md#control), [Operator Command](https://github.com/lbacik/agent-operator-contract/blob/06ee46c0204f0fc8a2465827cc397bc235dedc26/CONTEXT.md#control), [Sealed](https://github.com/lbacik/agent-operator-contract/blob/06ee46c0204f0fc8a2465827cc397bc235dedc26/CONTEXT.md#attempts-and-archives), [Operator Handoff](https://github.com/lbacik/agent-operator-contract/blob/06ee46c0204f0fc8a2465827cc397bc235dedc26/CONTEXT.md#control), [Managed Instance](https://github.com/lbacik/agent-operator-contract/blob/06ee46c0204f0fc8a2465827cc397bc235dedc26/CONTEXT.md#instances) and [Fleet](https://github.com/lbacik/agent-operator-contract/blob/06ee46c0204f0fc8a2465827cc397bc235dedc26/CONTEXT.md#instances).

## Language

### The worker and its work

**Worker**:
The single sequential process that selects and works one issue or pull request at a time for one Target Repository, running the one Workflow it was started with. It owns the tool loop of every model conversation it holds, so a third-party coding agent that runs its own loop, such as Codex CLI or Claude Code, is neither a Worker nor a Pinned Model.
_Avoid_: runner, bot, job, daemon, native worker

**Target Repository**:
The one GitHub repository, named by the Workflow Configuration, whose issues or pull requests the Worker may select and work on.
_Avoid_: source repo, project repo, upstream

**Target Project**:
The codebase inside the Target Repository that a Workflow examines or changes, characterised by its language and toolchain (Python, PHP or TypeScript).
_Avoid_: repo, workspace

**Selection Label**:
The configurable GitHub label whose presence makes an issue or pull request a candidate for the Worker. Only a human applies it: its presence authorises work, and removing it withdraws that authorisation; other configured label filters narrow the candidates but authorise nothing.
_Avoid_: trigger label, agent label, ready label

**Eligible Issue**:
An open issue in the Target Repository that passes every selection guard and so may be claimed.
_Avoid_: candidate, queued issue

**Target Issue**:
The Eligible Issue the Worker has claimed and is currently working.
_Avoid_: task, job, work item

**Target Pull Request**:
The eligible pull request the Worker has claimed and is currently working. Its type identifies the input, not the purpose of the Workflow.
_Avoid_: review task, review job

### Workflows

**Workflow Configuration**:
The operator's declaration of the Workflows a Worker may run and of the deployment they run in: the Target Repository, input types, model policy, label names and limits. It names secrets but never holds them, and it never comes from the Target Repository, so project or input content cannot redefine the graph or grant runtime authority.
_Avoid_: config, settings, workflow file, Project Profile

**Workflow**:
A named, configurable graph of Workflow Steps executed within an Attempt, entered at exactly one start step and left only through a Workflow End. Its configuration and activated skills describe the process and its requirements for progress, completion and optional publication.
_Avoid_: pipeline, recipe, flow

**Workflow Step**:
One unit of execution in a Workflow graph, with a declared result contract. It is carried out either by an agent, in a fresh conversation of the Worker's own model/tool loop with only the tools it declares, or by a Runtime Operation with no model at all. Its purpose comes from the Workflow and its instructions rather than from a built-in implementation or review role. Several instructions in one step are a bundle within one conversation; only separate steps guarantee that one piece of work happened before another.
_Avoid_: Workflow Block, node, stage, phase, task, step function

**Step Execution**:
One entry into a Workflow Step, identified by the Attempt, the step and its sequence number among that step's Step Executions. Its Step Result, Step Artifacts, execution facts and usage belong to it alone and never change once it ends, so revisiting a step adds a new Step Execution rather than overwriting an earlier one.
_Avoid_: visit, run, invocation, iteration

**Route**:
The assignment of one Step Result name of one Workflow Step to the Workflow Step that runs next or to a Workflow End. A Step Result selects exactly one Route by its name alone; the payload never chooses the successor.
_Avoid_: edge, transition, branch, condition

**Workflow End**:
A Route target that ends the Workflow under an ending category its author chooses from a closed set. It states which declared path ended the Workflow, not that the work succeeded, and is distinct from the Terminal Outcome the runtime records for the Attempt.
_Avoid_: terminal step, exit, finish, final state

**Execution Ceiling**:
The greatest number of Step Executions one Workflow Step may have within an Attempt. A Route into a step that has reached it is refused without a new Step Execution and continues along that step's limit-exceeded Route.
_Avoid_: max visits, loop limit, retry count

**Step Result**:
The explicit result of one Step Execution: either one of the named results its Workflow Step declares, with the payload declared for that name, reported by the agent as its only exit, or a result reserved to the runtime — a limit exceeded or an error — which no agent can declare or report. An agent that stops without a valid report has produced no result of its own; the runtime records one for it. A Runtime Operation's results are always the runtime's. A Step Result is a claim, not evidence, and is distinct from runtime-recorded execution facts and from the Terminal Outcome of the Attempt.
_Avoid_: Block Result, Completion Claim, Terminal Outcome, output

**Runtime Operation**:
A deterministic, registered operation of the Worker that a Workflow Step may perform, such as running validation, pushing the Attempt's work or publishing to the selected issue or pull request. It is the only route to an external write, and it enforces its own factual preconditions — authorisation, and that what it publishes is exactly the revision and evidence it names — wherever the Workflow places it. Whether its result is enough to proceed is the Workflow's policy, not the operation's.
_Avoid_: action, tool, effect, command, hook

**Step Artifact**:
A named, immutable, redacted document taken from a field of a Step Result that its Workflow Step declares as a document, belonging to that one Step Execution and kept outside the Target Repository's tree. It is how one step's writing reaches another step or a Runtime Operation; a Step Execution without a valid report leaves none.
_Avoid_: file, output, attachment, report

**Effective Configuration**:
The part of the Workflow Configuration an Attempt runs under, together with the exact operator instructions its Workflow Steps activate, fixed at Claim. It is what the Attempt is answerable to, so editing the Workflow Configuration affects the next Attempt and never one already open.
_Avoid_: snapshot, pinned config, current config

### The runtime environment

**Project Profile**:
The Target Repository's own declaration of how its Target Project is bootstrapped, tested and checked, how those commands report their individual results, and what runtime versions and services they require. It is where the Readiness Facts are declared; prose documentation is a fallback, not the canonical form. It describes one working area, so a repository holding two applications declares the one an Attempt may be judged against.
_Avoid_: config, manifest, project config, profile file

**Supported Toolchain Matrix**:
The language runtime versions and package managers a given agent image actually provides. A Project Profile requiring anything outside it cannot be worked by that image.
_Avoid_: toolchain, supported versions, image matrix

**Skill Bundle**:
The reviewed, pinned set of upstream skills a given agent image carries, together with their resolved commit provenance. Its version is the image's version; it is never changed at runtime.
_Avoid_: skills, skill set, allowlist, installed skills

**Companion File**:
A file sitting beside a `SKILL.md` in the Skill Bundle that its skill links to for depth. In this system a companion is either injected whole into the Pinned Prefix along with the skill that owns it, or it is unavailable to the model entirely; there is no route by which one is fetched during an Attempt. Which of the two a given companion gets is decided by reading it, and a build-time digest is what stops that reading from silently expiring when the bundle's pin advances.
_Avoid_: resource, skill resource, attachment, supporting file, linked doc

**Sub-agent**:
A separate conversation a Workflow Step's model starts by tool call, given its own instructions and a restricted set of tools, whose only return to the caller is its report. It shares nothing with its caller's conversation, and everything it spends counts against the Attempt that started it. It opens with framing the Worker authors, telling it that it does the work its instructions describe itself and ends only with its report. That framing carries no criteria, and without it a conversation handed instructions that describe a prompt can answer them by writing that prompt instead of doing the work.
_Avoid_: subtask, delegated conversation, child agent, helper, task

**Sub-agent Report**:
The single report a Sub-agent returns to its caller, made by a tool call that is its only exit. A Sub-agent that stops speaking, runs out of rounds or is stopped has reported nothing, and its caller is told so rather than handed whatever it last said.
_Avoid_: sub-agent output, sub-agent result, response, summary

**Review**:
A code-assessment activity described by a Workflow and/or an activated skill. Its scope, participants and completion requirements belong to that scenario.
_Avoid_: review run, review pass, review invocation, review round

**Review Report**:
The findings of a Review, or an explicit statement that it has none, in the form required by the Workflow or skill. It may be carried by a Step Result or a Sub-agent Report.
_Avoid_: review result, findings, reviewer output, report

**Attempt Header**:
The framing the Worker itself authors at the head of a work node's conversation, carrying the Attempt's own facts, the inputs its Workflow Step declares, framed as data rather than instruction, and the Step Result contract that is its only exit. Facts particular to a scenario, such as the Seam Set, reach it only as declared inputs. It is held out of compaction for the life of the node, so an instruction in an upstream skill that refers to it still has its referent late in a long tool loop. It answers the references an upstream skill makes to things an Attempt does not have, such as other skills named as slash commands, a user to ask, or sub-agents and the shell commands they would run, naming what stands for each, because nothing else in an Attempt resolves them. It answers those references and never edits the skill text that makes them.
_Avoid_: system prompt, preamble, context header, prelude

**Pinned Prefix**:
The part of a work node's conversation that compaction cannot reach: the Attempt Header and the Attempt's selected issue or pull request. It is a region rather than a rule — the eviction code is handed the history and never the prefix, so "never compacted" is a fact about which list a message is in rather than an instruction a future compactor has to keep obeying. Losing it is silent: a provider accepts a request without it and answers normally, so the guarantee cannot rest on noticing. A skill body reaches the model through its own activation tool call and is an ordinary Exchange Unit, never a Pinned Prefix member, in the node that called it or in any later one.
_Avoid_: system prompt, pinned context, protected messages, the preamble

**Exchange Unit**:
One assistant turn together with every tool result answering it. It is the grain compaction evicts by: the parts move together or not at all, because a tool result parted from its call is a malformed request on one pin and a broken reasoning chain on the other. Whether a message-counting truncation happens to split one is a coin flip on where its boundary falls, which is why the grain is named rather than left to arithmetic.
_Avoid_: turn, message pair, exchange, round trip

**Pinned Model**:
The provider, model, endpoint and effort the Worker calls for the whole of an Attempt, fixed by the Workflow Configuration rather than chosen per Target Issue or per Workflow Step: every Step Execution and every Sub-agent in the Attempt uses it. It is recorded in the Run Ledger beside the Fingerprint. The endpoint is part of it, not a detail beneath it: the same model id on another endpoint accepts a different set of requests. No second model ever answers for it — there is no fallback at any point, and the model that answered each response is recorded so the absence can be asserted rather than trusted.
_Avoid_: the model, provider, LLM, model config, model name

**Provider Capability Assertion**:
The small live check the Worker makes at startup to establish that its Pinned Model can do what a work node needs — call a tool and report non-zero usage under the pinned effort, then refuse the same request sent with an invalid effort, which proves the effort setting reaches the provider — plus the assertion that a price is configured for it. It establishes capability by attempting rather than by reading what the provider declares about itself, because the capability every node depends on hardest, tool calling, is declared by neither provider. It refuses by exiting nonzero with no Attempt opened; a misconfigured deployment is not a property of any Target Issue.
_Avoid_: preflight, capability check, model preflight, health check, smoke test

**Agent Identity**:
The single GitHub account the Worker acts as for every read, write and commit, recognised by its numeric account id rather than by its login. No other identity ever writes on the Worker's behalf. It does not, on its own, tell the Worker's own writing apart from a human's: the account may be shared with a human, and what carries that distinction is the Attempt Marker.
_Avoid_: bot, service account, machine user, token, credentials

**Base Revision**:
The commit the Attempt's work is based on, resolved from the configured base branch at Claim time and recorded alongside the Fingerprint. It fixes what "implemented against" means for the whole Attempt.
_Avoid_: base, base branch, main, HEAD, target branch

### Attempts and their durable record

**Attempt**:
One cycle from Claim to a recorded Terminal Outcome for a single selected issue or pull request.
_Avoid_: run, iteration, pass, try

**Claim**:
The Worker's act of taking exclusive responsibility for an authorised issue or pull request, opening a new Attempt. It is a lifecycle responsibility shared by all Workflow scenarios.
_Avoid_: lock, assignment, acquire

**Lease**:
The heartbeat-backed record that an Attempt is live, distinguishing a running Worker from one that died. It is not part of the MVP: a single Worker that holds the exclusive lock on its state volume already knows that every open Attempt it finds was left by a dead process.
_Avoid_: lock, mutex, token

**Run Ledger**:
The Worker's durable local record of Attempts, Fingerprints, Clarification Rounds and budgets, together with each Attempt's pinned Effective Configuration, its Terminal Outcome as decided before any of it was published, and which of its external writes have landed. It is authoritative for lifecycle history; GitHub labels are not. It is the only state a restarted Worker recovers from: the execution framework's own saved state is not part of it, so a restart finishes what the ledger records rather than resuming what the framework remembers.
_Avoid_: database, state store, journal, log

**Transcript**:
The per-Attempt record of what each conversation's model asked for and was answered: every request, response and tool call with its arguments, redacted. It records what was requested, never what executed code did as a side effect, and nothing in an Attempt reads it back.
_Avoid_: log, conversation log, history, trace

**Fingerprint**:
The digest of the Target Issue's title, body and referenced specification as read at Claim time, identifying the version an Attempt is working against.
_Avoid_: hash, snapshot, version, revision

**Context-Window Handoff**:
A bounded transition from one work node to a fresh work node when the current node's context approaches its Pinned Model threshold. It is an engine-internal transition and is distinct from an [Operator Handoff](https://github.com/lbacik/agent-operator-contract/blob/06ee46c0204f0fc8a2465827cc397bc235dedc26/CONTEXT.md#control), which belongs to the operator contract vocabulary above.
_Avoid_: compaction, eviction, retry

**Continuation Snapshot**:
A versioned, redacted record of the explicit repository facts a fresh work node needs to continue work: within an Attempt after a Context-Window Handoff, or across Attempts when a Continuation picks up pushed work. It is composed by the runtime, never written into the Target Project's tree.
_Avoid_: summary, transcript, checkpoint, handoff note

**Continuation**:
An Attempt that starts from the pushed work of the Target Issue's previous Attempt, rebased onto its own Base Revision, rather than from the Base Revision alone. It happens only because a human re-applied the Selection Label while that work still exists; deleting the previous branch is how a human asks for a fresh start instead. It is a new Attempt, not the old one resumed.
_Avoid_: resume, retry, round, auto-continuation

**Attempt Marker**:
The stamp the Worker puts on every artifact it writes, carried in whatever idiom the artifact allows: a machine-readable line in a comment or a pull request body, a trailer on a commit, a prefix on a branch name. It is what tells the Worker's own writing apart from a human's, and it reads in one direction only — an artifact without one was not written by the Worker. The converse is not guaranteed, because whoever holds the Agent Identity's credential can write one by hand.
_Avoid_: tag, stamp, signature, identity marker, attempt id

**Gate**:
A defined point between graph nodes, and before every external write, at which the Worker re-reads GitHub state, re-evaluates its limits, and may stop cooperatively.
_Avoid_: checkpoint, barrier, savepoint, poll

Named **Gate** rather than *checkpoint* because the execution framework owns that word for its own persisted state snapshots; one word for two concepts in one codebase is a defect.

**Publication Gate**:
The publication-policy decision described by a Workflow or skill, using the results that scenario requires. It is distinct from the mandatory runtime checks on authorization, limits and durable external effects.
_Avoid_: final gate, publish check, release gate

**Drift**:
A change in Fingerprint detected during an Attempt, meaning the Target Issue no longer matches the version being implemented.
_Avoid_: staleness, conflict, divergence

### Clarification

**Clarification Round**:
One cycle of the Worker publishing a Question Set and a human answering it. Rounds are counted per Target Issue, not per Attempt, and they survive re-authorisation: re-applying the Selection Label to resume a clarification does not return the count to zero. Only a human lifting a terminal stop resets it. It is not part of the MVP: a missing Readiness Fact ends the Attempt with a comment naming what is missing, and a human re-applies the Selection Label once the Target Issue is fixed.
_Avoid_: question, exchange, ping-pong

**Question Set**:
The numbered questions published in a single clarification comment, identified by a digest carried in that comment's machine-readable marker. It is not part of the MVP: the Worker asks no questions, and a Readiness refusal names what is missing in its terminal comment instead.
_Avoid_: questions, query, request

**Qualifying Answer**:
Content that the Worker may act on: a comment written after its Question Set by someone whose effective repository role lets them apply the Selection Label, or an edit to the Target Issue itself. Whoever may authorise may answer; the two audiences are deliberately the same one, so that a person who can hand the issue back cannot also be ignored when they explain it. Content bearing an Attempt Marker is the Worker's own writing and never qualifies. It is not part of the MVP: the Worker reads no comment as an answer, so a human answers by editing the Target Issue and re-applying the Selection Label.
_Avoid_: reply, response, feedback

**Stale Answer**:
Content that predates the Question Set it appears to address, and which the Worker therefore ignores. It is not part of the MVP: the Worker reads no human comment, so no content can be stale.
_Avoid_: old comment, outdated reply

**Readiness Facts**:
The minimum set of facts an Attempt needs before implementation may begin: runtime and package manager, bootstrap command, test command, how that test command reports its individual results, type or static check commands (or an explicit statement that the Target Project has none), the Acceptance Criteria, and the Seam Set. The last two are properties of the task rather than of the Target Project, so they are the two that never resolve from the Project Profile.
_Avoid_: prerequisites, requirements, config, documentation

**Acceptance Criterion**:
One item of what the Target Issue asks for, enumerated from its body by a text convention alone, with no model call — a Readiness Fact like the Seam Set, and missing in the same way when the issue carries none. The enumeration is the runtime's and never the model's: a list the model composed would let an Attempt narrow its own task and then report the narrowed version complete.
_Avoid_: requirement, spec item, AC, acceptance criteria (as an unsplit mass noun)

**Seam Set**:
The public boundaries an Attempt's tests may be written at, named individually, resolved before the implementer's conversation opens and never authored inside it. It resolves from the Target Issue and its referenced specification alone — a seam is a property of the task, so no project-level declaration can state it. Where the Target Issue names none, the Seam Set is a missing Readiness Fact like any other. It authorises rather than verifies: nothing afterwards checks that the tests landed at its boundaries, and the Delivery discloses it as a Carried Decision used.
_Avoid_: seams, test boundaries, interfaces under test, agreed seams

**Carried Decision**:
A human-approved resolution — an answer to a Question Set, or an accepted testing seam — recorded in the Run Ledger against the Fingerprint it was given under, and usable by a later Attempt only while that Fingerprint still holds. A human gate ends an Attempt; a Carried Decision is how its outcome reaches the next one. It is not part of the MVP: nothing a human decided at a gate is carried to a later Attempt except what the Target Issue's body says.
_Avoid_: approval, consent, cached answer, memory

### Ending an attempt

**Completion Claim**:
An implementation-scenario declaration that the requested implementation is complete, in the form required by its Workflow or skill. It is a claim rather than runtime-produced evidence, and is not the universal result contract of every Workflow Step.
_Avoid_: completion, done signal, finish call, final answer

**Criterion Evidence**:
The recorded result of the runtime running the Validation Contract's targeted test command, binding an Acceptance Criterion to the test identifiers that actually executed and their results. It is produced only by the tool that owns the evidence directory; a test run by any channel other than `test_targeted` — including `run_command` — leaves nothing that counts, for the same reason the model's account of a test run is never Validation Evidence — a file the model wrote by hand is that account in another format.
_Avoid_: test evidence, proof, coverage, targeted results

**Execution Footprint**:
The set of workspace paths that project code changed while one tool call executed it (`test_targeted` or `run_command`), each with its content digest before and after. It watches the git-visible tree only — tracked files plus untracked files that are not gitignored, never `.git/` and never ignored paths, the same set as the final-checks dirty-tree rule — so writes to gitignored paths are invisible to it. It discloses an indirect write; it never gates one, and it proves nothing against code that set out to hide its effects, because that code runs with the Worker's own privileges.
_Avoid_: write log, side-effect log, file changes, diff

**Ref Guard**:
The runtime's restoration of the Attempt's branch and HEAD after every tool call that executes project code. It compares three values before and after the call — the branch HEAD points at, the commit HEAD resolves to, and the commit the Attempt branch ref points to — and where any differs it restores all three with ref plumbing only, leaving the working tree and the index exactly as the call left them. The footprint never watches `.git/`, which is why the guard exists beside it: a commit moves no workspace path the footprint could see.
_Avoid_: branch restore, HEAD reset, checkout guard, ref check

**Implementation Outcome**:
The implementation-specific result described by an implementation Workflow or skill. It is distinct from a generic Step Result and from the Terminal Outcome of the Attempt.
_Avoid_: outcome, result, status, terminal outcome, completion status

**Terminal Outcome**:
The runtime-recorded state in which an Attempt ends, distinct from the business result of any individual Workflow Step and from the Workflow End that reached it, since cancellation or a runtime failure can end an Attempt without one.
_Avoid_: result, exit, final state

**Delivery Snapshot**:
The commit that fixes the candidate tree an Attempt offers for judgement. It is what the reviewers and the validation commands are pointed at, so a tree that changes afterwards is different evidence and needs a new Delivery Snapshot of its own.
_Avoid_: snapshot, checkpoint commit, candidate commit, WIP commit

**Validation Contract**:
The set of validation commands and their evidence declarations as they stand at the Base Revision, pinned at Claim time. It is what this Attempt is judged by, so a candidate that edits the Project Profile changes the contract for the next Attempt and never for its own.
_Avoid_: commands, checks, test config

**Validation Evidence**:
The recorded result of the Worker running the Validation Contract's commands against a Delivery Snapshot: the commands, their exit codes, how many tests actually executed, and the identifier of every individual failure. It is what makes "verified" a checkable claim; the model's account of a test run is never Validation Evidence.
_Avoid_: test results, CI, report, log, green

**Baseline Failure**:
An individual failure, identified by name, that already occurs at the Attempt's Base Revision. It is excluded from the Publication Gate, because it is not this Attempt's debt. The unit is the failure, never the command that reported it: a command whose failures at the Delivery Snapshot are not all present at the Base Revision has produced a regression, and that blocks. Where a command cannot name its individual failures, no Baseline Failure can be established for it and the Attempt cannot claim to have shown the absence of a regression. It is not part of the MVP: a red baseline is a Readiness refusal before any model call, so no failure is ever excused.
_Avoid_: pre-existing failure, known failure, flaky test, red baseline

**Delivery**:
The optional publication of a Workflow's result, such as a pull request, review or comment. It is distinct from the end of the Attempt itself.
_Avoid_: submit, ship, merge, complete

**Delivery Intent**:
The durable record the Publication Gate writes before the first delivery write, naming the Attempt, its branch and head commit, the gate's verdict and the writes that verdict calls for. It is what makes an interrupted publication resumable as itself rather than repeatable as a fresh Attempt, and writing it is the act that makes the Attempt's outcome irreversible.
_Avoid_: publish plan, delivery record, pending write, intent log

**Delivery Draft**:
A Delivery the Worker publishes as a draft pull request because it does not itself consider the Publication Gate satisfied — the Target Issue drifted, blocking review findings survived, or validation stayed red. It carries the reason on its face and waits for a human's judgement before ordinary review even begins. The Worker never converts one to a ready pull request. It is not part of the MVP: an Attempt whose Publication Gate is not satisfied ends without a pull request, with its branch pushed and `ready-for-human` added.
_Avoid_: WIP PR, failed delivery, partial delivery, draft

**Unsupported Environment**:
The condition in which a Project Profile is complete and correct but the running image cannot satisfy it — a runtime version outside the Supported Toolchain Matrix, or a declared service that is unreachable. It is a classification of a failed Attempt, not a request for information: no human answer can resolve it, only a different deployment.
_Avoid_: unsupported, environment failure, bad config, missing dependency

**Credential Failure**:
The condition in which the Agent Identity's credential has stopped working altogether — absent, expired or revoked — so the Worker cannot even publish an explanation of its own failure. Like Unsupported Environment it is a classification of a failed Attempt rather than a request for information, but unlike it, it halts the Worker instead of releasing it to the next Target Issue: a broken credential would otherwise consume the whole queue one issue at a time. A single write refused for want of a permission is not this; that Attempt fails alone and the Worker continues.
_Avoid_: auth error, 401, token error, permission denied

**Automatic Retry Budget**:
The number of Attempts the Worker may open for one Target Issue without fresh human authorisation. It is not part of the MVP: every new Attempt needs a human to re-apply the Selection Label.
_Avoid_: retries, max attempts, backoff
