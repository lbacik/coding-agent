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

**Agent Identity**:
The single GitHub account the Worker acts as for every read, write and commit, recognised by its numeric account id rather than by its login. No other identity ever writes on the Worker's behalf. It does not, on its own, tell the Worker's own writing apart from a human's: the account may be shared with a human, and what carries that distinction is the Attempt Marker.
_Avoid_: bot, service account, machine user, token, credentials

**Base Revision**:
The commit the Attempt's work is based on, resolved from the configured base branch at Claim time and recorded alongside the Fingerprint. It fixes what "implemented against" means for the whole Attempt.
_Avoid_: base, base branch, main, HEAD, target branch

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

**Attempt Marker**:
The stamp the Worker puts on every artifact it writes, carried in whatever idiom the artifact allows: a machine-readable line in a comment or a pull request body, a trailer on a commit, a prefix on a branch name. It is what tells the Worker's own writing apart from a human's, and it reads in one direction only — an artifact without one was not written by the Worker. The converse is not guaranteed, because whoever holds the Agent Identity's credential can write one by hand.
_Avoid_: tag, stamp, signature, identity marker, attempt id

**Gate**:
A defined point between graph nodes, and before every external write, at which the Worker re-reads GitHub state, re-evaluates its limits, and may stop cooperatively.
_Avoid_: checkpoint, barrier, savepoint, poll

Named **Gate** rather than *checkpoint* because the execution framework owns that word for its own persisted state snapshots; one word for two concepts in one codebase is a defect.

**Publication Gate**:
The last Gate of an Attempt, immediately before the pull request is opened, at which the validation result, the review findings, Drift, movement of the base branch and any stop signal are judged together. It is the Attempt's point of no return: a stop reaching it diverts the Attempt, a stop arriving after it does not.
_Avoid_: final gate, publish check, release gate

**Drift**:
A change in Fingerprint detected during an Attempt, meaning the Target Issue no longer matches the version being implemented.
_Avoid_: staleness, conflict, divergence

### Clarification

**Clarification Round**:
One cycle of the Worker publishing a Question Set and a human answering it. Rounds are counted per Target Issue, not per Attempt, and they survive re-authorisation: re-applying the Selection Label to resume a clarification does not return the count to zero. Only a human lifting a terminal stop resets it.
_Avoid_: question, exchange, ping-pong

**Question Set**:
The numbered questions published in a single clarification comment, identified by a digest carried in that comment's machine-readable marker.
_Avoid_: questions, query, request

**Qualifying Answer**:
Content that the Worker may act on: a comment written after its Question Set by someone whose effective repository role lets them apply the Selection Label, or an edit to the Target Issue itself. Whoever may authorise may answer; the two audiences are deliberately the same one, so that a person who can hand the issue back cannot also be ignored when they explain it. Content bearing an Attempt Marker is the Worker's own writing and never qualifies.
_Avoid_: reply, response, feedback

**Stale Answer**:
Content that predates the Question Set it appears to address, and which the Worker therefore ignores.
_Avoid_: old comment, outdated reply

**Readiness Facts**:
The minimum set of facts an Attempt needs before implementation may begin: runtime and package manager, bootstrap command, test command, how that test command reports its individual results, type or static check commands (or an explicit statement that the Target Project has none), and the task's acceptance criteria.
_Avoid_: prerequisites, requirements, config, documentation

**Carried Decision**:
A human-approved resolution — an answer to a Question Set, or an accepted testing seam — recorded in the Run Ledger against the Fingerprint it was given under, and usable by a later Attempt only while that Fingerprint still holds. A human gate ends an Attempt; a Carried Decision is how its outcome reaches the next one.
_Avoid_: approval, consent, cached answer, memory

### Ending an attempt

**Terminal Outcome**:
The state in which an Attempt ends and the Worker stops touching the Target Issue until a human acts.
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
An individual failure, identified by name, that already occurs at the Attempt's Base Revision. It is excluded from the Publication Gate, because it is not this Attempt's debt. The unit is the failure, never the command that reported it: a command whose failures at the Delivery Snapshot are not all present at the Base Revision has produced a regression, and that blocks. Where a command cannot name its individual failures, no Baseline Failure can be established for it and the Attempt cannot claim to have shown the absence of a regression.
_Avoid_: pre-existing failure, known failure, flaky test, red baseline

**Delivery**:
The handoff of finished work as an open pull request plus a comment on the Target Issue. The Worker never merges.
_Avoid_: submit, ship, merge, complete

**Delivery Intent**:
The durable record the Publication Gate writes before the first delivery write, naming the Attempt, its branch and head commit, the gate's verdict and the writes that verdict calls for. It is what makes an interrupted publication resumable as itself rather than repeatable as a fresh Attempt, and writing it is the act that makes the Attempt's outcome irreversible.
_Avoid_: publish plan, delivery record, pending write, intent log

**Delivery Draft**:
A Delivery the Worker publishes as a draft pull request because it does not itself consider the Publication Gate satisfied — the Target Issue drifted, blocking review findings survived, or validation stayed red. It carries the reason on its face and waits for a human's judgement before ordinary review even begins. The Worker never converts one to a ready pull request.
_Avoid_: WIP PR, failed delivery, partial delivery, draft

**Unsupported Environment**:
The condition in which a Project Profile is complete and correct but the running image cannot satisfy it — a runtime version outside the Supported Toolchain Matrix, or a declared service that is unreachable. It is a classification of a failed Attempt, not a request for information: no human answer can resolve it, only a different deployment.
_Avoid_: unsupported, environment failure, bad config, missing dependency

**Credential Failure**:
The condition in which the Agent Identity's credential has stopped working altogether — absent, expired or revoked — so the Worker cannot even publish an explanation of its own failure. Like Unsupported Environment it is a classification of a failed Attempt rather than a request for information, but unlike it, it halts the Worker instead of releasing it to the next Target Issue: a broken credential would otherwise consume the whole queue one issue at a time. A single write refused for want of a permission is not this; that Attempt fails alone and the Worker continues.
_Avoid_: auth error, 401, token error, permission denied

**Automatic Retry Budget**:
The number of Attempts the Worker may open for one Target Issue without fresh human authorisation.
_Avoid_: retries, max attempts, backoff
