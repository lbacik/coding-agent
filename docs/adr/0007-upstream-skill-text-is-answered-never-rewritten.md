---
status: accepted
---

# Upstream skill text is answered, never rewritten

Every piece of an upstream skill the harness hands a model travels **verbatim**. The adapter may cut it at structural boundaries and surround it with its own framing, but it never edits a byte inside. Where a Role Block refers to something it does not contain — "the list of standards-source files you found in step 3" — the adapter **supplies the referent alongside** rather than rewriting the reference. Decided in [How much may the S4 adapter rewrite an extracted role block?](https://github.com/lbacik/coding-agent/issues/21).

The question is not tidiness. The Skill Bundle is pinned per image and its version is the image's version, so advancing the pin can reword §4 of `code-review/SKILL.md`, renumber a step, or move the smell baseline. What happens then depends entirely on how much the adapter assumes about the text's internal shape. An adapter that only knows where a block *begins and ends* has one assumption to check; an adapter that knows which sentence carries which reference has as many assumptions as sentences, and every one of them rots silently.

This is the hazard class [ADR 0002](0002-commit-and-push-before-review.md) addresses for evidence: a check that cannot fail visibly is not a check.

## The rule, in four parts

**1. Verbatim slices.** The adapter's only operations on upstream text are cut and concatenate. No normalisation, no inline substitution, no removal of orchestrator-facing framing — the self-naming first line `**Standards sub-agent prompt** should include:` stays in, because dropping it is an edit rule whose correctness depends on the block's shape. Composing the Standards Role Block from two verbatim slices (§4's brief plus §3, which the brief says the reviewer "has no other access to") is concatenation, not editing, so both roles get the identical rule and differ only in how many anchors they declare.

**2. Structural anchors only.** An anchor is a heading line or a line opening a block with a bold label — never a prose fragment. Headings and bold labels are structure an upstream author treats as stable; a sentence opening is prose they reword without thinking. This makes the digest gate below fire on *content* changes rather than on the adapter having lost its footing.

**3. A bounded Seat Assignment.** The adapter authors the framing that tells a conversation which single axis it occupies and that its final message is its report. [The fan-out prototype](https://github.com/lbacik/coding-agent/issues/20) proved this is load-bearing: the arm with no seat assigned reviewed both axes in one context and reported a fan-out that never happened. It is therefore not removable — and equally, it carries **no review criteria**. It is a constant, identical for both roles but for the axis name, interpolating nothing else. §4 already carries its own word limit and report structure; a second set from the adapter would be two sources of truth.

**4. A build-time digest gate.** The pin fixes the bytes, but nothing today notices when those bytes move under an adapter that depends on their shape. So the Skill Bundle spec declares, per Role Block, its anchors and a digest of the resulting slice, and the build asserts each anchor occurs **exactly once** in the installed `SKILL.md` and each slice's raw digest matches. A pin bump that rewords §3 or §4 fails the build, naming the block, and a human reads the diff before it ships. The digest is over raw bytes: normalising first would be one more assumption about shape, which is the thing being avoided.

The gate binds only where the adapter depends on internal structure, so it covers S4's slices and not S3's whole-file injection of `implement`, `tdd` and `codebase-design`. Part 1 binds those too — they go in verbatim — but a whole-file digest would buy nothing: a pin bump changes every file's digest, so the human re-records four values without reading anything. The narrowness of a slice is what makes its change legible.

## Considered options

**Render the referents inline.** Rewrite "the list of standards-source files you found in step 3" into a sentence naming the files. Rejected: it requires the adapter to know which sentence carries which reference, and that knowledge is exactly what a pin bump invalidates without any signal. The prototype resolved the same references by juxtaposition — block verbatim, then a section supplying the concrete inputs — and the reviewer behaved.

**Normalise the orchestrator-facing framing** — drop the self-naming line, re-address second-person references. Rejected for the same reason at smaller scale: the prototype left the self-naming phrase in on purpose and the reviewer did not misbehave, so nothing forces it out, and a rule permitting the edit is a rule that has to stay correct across pins.

**Anchors as any unique literal string.** What the prototype did, cutting §3's baseline at `On top of whatever the repo documents,`. Rejected: an upstream reword of that sentence breaks extraction rather than tripping a content check, and it makes "which slice is this" unverifiable by eye.

**Anchors plus one documented prose anchor**, to keep `If the spec is missing, skip the Spec sub-agent` out of the Spec Role Block. Rejected: it buys one sentence for exactly the kind of anchor upstream can reword mindlessly. See the residual risk below.

**Extract at image build and bake the blocks as files.** Rejected: it creates a second source of truth, and a baked block can diverge from the installed skill. What runs is read from what is installed.

**Aggregate §5 in a third model conversation.** Rejected: it reintroduces the prototype's silent-collapse failure at the join, where it is harder to see — the two reports really were produced separately and only their aggregation lies. §5 is fully deterministic (verbatim, no merge, no rerank), so the model has no work to do.

## Consequences

**Extraction happens at process start, from the installed `SKILL.md`, and a failure is a refusal to start.** One declaration, two enforcement points: the build checks it, and so does the process before it selects any Target Issue. A failure is not an Attempt classification — no Attempt is open, so there is nothing to classify; the process exits nonzero. This follows the reasoning already recorded for **Credential Failure**: a defect that will break every Target Issue identically must not consume the queue one issue at a time.

**The Standards Role Block carries the whole of §3**, including the sentence about where standards sources live, which is the orchestrator's work. Accepted as the same class of thing as the self-naming line.

**The Spec Role Block carries `If the spec is missing, skip the Spec sub-agent and note this in the final report.`** The antecedent is false by construction — the adapter opens a Spec conversation only when §2 resolved a spec — but this is the one place a reviewer reads an instruction to skip itself, and the prototype did not have it. **S4's first real run must check that the Spec reviewer did not skip its axis.** Recorded as a residual risk, not as a reason to add a prose anchor.

**Blocking by kind reads upstream's own labels.** §4 already emits the taxonomy `L3-REV-6`/`L3-REV-7` need: Standards is told to "distinguish hard violations from judgement calls", Spec labels findings (a) missing/partial, (b) scope creep, (c) implemented but wrong. The adapter maps those onto blocking and asks the reviewer for nothing extra, which is what keeps part 3 intact. The mapping is itself an assumption about the block's content — and the digest gate covers those sentences, so a reworded taxonomy fails the build.

**A finding whose kind cannot be read blocks.** The project already resolves ambiguity against the Attempt: where a validation command cannot name its individual failures, no Baseline Failure can be established and the Attempt cannot claim to have shown the absence of a regression. The cost asymmetry agrees — a false block costs one remediation round and then `delivered-as-draft`, bounded by construction, while a false pass ships a ready pull request with a correctness bug.

**Advancing the Skill Bundle pin now requires reading a diff.** A whitespace-only change to §3 or §4 will fail the build. That is the price of the gate, and it is small: bumps are deliberate and rare, and a false alarm costs one reading.

Reversing this decision means permitting the adapter to know the inside of a block. Any replacement must say how a pin bump that changes that inside becomes visible before it ships.
