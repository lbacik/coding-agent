# Coding Agent

A containerised agent that takes an issue from one configured GitHub repository, implements it with the upstream `/implement` skill, and hands a pull request back to a human. This glossary fixes the vocabulary of that journey; it holds no implementation detail.

## Language

### The worker and its work

**Worker**:
The single sequential process that selects and works one Target Issue at a time for one Target Repository.
_Avoid_: runner, bot, job, daemon

**Target Repository**:
The one GitHub repository, supplied as configuration, whose issues the Worker may select and write to.
_Avoid_: source repo, project repo, upstream

**Target Project**:
The codebase inside the Target Repository that the Worker changes, characterised by its language and toolchain (Python, PHP or TypeScript).
_Avoid_: repo, workspace

**Selection Label**:
The configurable GitHub label whose presence makes an issue a candidate for the Worker. Defaults to `ready-for-agent`, and only a human ever applies it.
_Avoid_: trigger label, agent label, ready label

**Eligible Issue**:
An open issue in the Target Repository that passes every selection guard and so may be claimed.
_Avoid_: candidate, queued issue

**Target Issue**:
The Eligible Issue the Worker has claimed and is currently working.
_Avoid_: task, job, work item

### Attempts and their durable record

**Attempt**:
One cycle from Claim to a Terminal Outcome for a single Target Issue, identified as `#<issue>/<n>`.
_Avoid_: run, iteration, pass, try

**Claim**:
The Worker's act of taking exclusive local responsibility for a Target Issue, opening a new Attempt.
_Avoid_: lock, assignment, acquire

**Lease**:
The heartbeat-backed record that an Attempt is live, distinguishing a running Worker from one that died.
_Avoid_: lock, mutex, token

**Run Ledger**:
The Worker's durable local record of Attempts, Fingerprints, Clarification Rounds and budgets. It is authoritative for lifecycle history; GitHub labels are not.
_Avoid_: database, state store, journal, log

**Fingerprint**:
The digest of the Target Issue's title, body and referenced specification as read at Claim time, identifying the version an Attempt is working against.
_Avoid_: hash, snapshot, version, revision

**Gate**:
A defined point between graph nodes, and before every external write, at which the Worker re-reads GitHub state, re-evaluates its limits, and may stop cooperatively.
_Avoid_: checkpoint, barrier, savepoint, poll

Named **Gate** rather than *checkpoint* because the execution framework owns that word for its own persisted state snapshots; one word for two concepts in one codebase is a defect.

**Drift**:
A change in Fingerprint detected during an Attempt, meaning the Target Issue no longer matches the version being implemented.
_Avoid_: staleness, conflict, divergence

### Clarification

**Clarification Round**:
One cycle of the Worker publishing a Question Set and a human answering it. Rounds are counted per Target Issue, not per Attempt.
_Avoid_: question, exchange, ping-pong

**Question Set**:
The numbered questions published in a single clarification comment, identified by a digest carried in that comment's machine-readable marker.
_Avoid_: questions, query, request

**Qualifying Answer**:
Content that the Worker may act on: a comment written after its Question Set by a user with write access to the Target Repository, or an edit to the Target Issue itself.
_Avoid_: reply, response, feedback

**Stale Answer**:
Content that predates the Question Set it appears to address, and which the Worker therefore ignores.
_Avoid_: old comment, outdated reply

**Readiness Facts**:
The minimum set of facts an Attempt needs before implementation may begin: runtime and package manager, bootstrap command, test command, type or static check commands (or an explicit statement that the Target Project has none), and the task's acceptance criteria.
_Avoid_: prerequisites, requirements, config, documentation

**Carried Decision**:
A human-approved resolution — an answer to a Question Set, or an accepted testing seam — recorded in the Run Ledger against the Fingerprint it was given under, and usable by a later Attempt only while that Fingerprint still holds. A human gate ends an Attempt; a Carried Decision is how its outcome reaches the next one.
_Avoid_: approval, consent, cached answer, memory

### Ending an attempt

**Terminal Outcome**:
The state in which an Attempt ends and the Worker stops touching the Target Issue until a human acts.
_Avoid_: result, exit, final state

**Delivery**:
The handoff of finished work as an open pull request plus a comment on the Target Issue. The Worker never merges.
_Avoid_: submit, ship, merge, complete

**Automatic Retry Budget**:
The number of Attempts the Worker may open for one Target Issue without fresh human authorisation.
_Avoid_: retries, max attempts, backoff
