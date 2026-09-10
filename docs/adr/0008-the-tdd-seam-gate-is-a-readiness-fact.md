---
status: accepted
---

# The TDD seam gate is a readiness fact, not a question to the model

`tdd/SKILL.md` carries its hardest rule as an instruction to whoever reads it: *"Before writing any test, write down the seams under test and confirm them with the user. No test is written at an unconfirmed seam."* The Worker has no user in the loop — `interrupt()` is not used, and a human gate is the end of a graph run rather than a pause. So the Seam Set is resolved as a **Readiness Fact**, before the implementer's conversation opens, and the implementer is handed it as a given. Decided in [Where does the TDD seam gate live, now that the model will not report an unconfirmed seam?](https://github.com/lbacik/coding-agent/issues/23).

## The observation that rules out the obvious design

The obvious design is to leave the instruction in place, let the model notice it has nobody to confirm with, and assert on what it says. [The implementer prototype](https://github.com/lbacik/coding-agent/issues/22) ran three arms against that instruction with no user available. **None asked. None noted the absence. None stopped.** Each declared the seam already settled and wrote tests — arm A in the past tense, *"the single agreed seam"*, agreed with nobody.

What makes this decisive rather than merely disappointing is the comparison with the `/code-review` substitution in the same run. Handed an unreachable `/code-review`, **every arm reviewed its own work and every arm disclosed that it had.** Handed an unconfirmable seam, **no arm disclosed anything** — the substitution left no trace in the output at all. So the two failure modes are not the same class, and an assertion over the model's report catches one and not the other:

> `L3-CLR-16` cannot be satisfied by the model noticing that it has no Carried Decision. It will not notice, and its prose will read as though it did.

One arm points at the design that works. Arm B wrote *"the ticket names `receipt_total` as the public interface under test — that's the only seam I'll test at."* That is not a fabricated agreement; it is a **derivation from the Target Issue**. The seam was available without asking anybody, and the run that had the least to invent was the one that invented nothing.

## The rule, in four parts

**1. The Seam Set is a Readiness Fact, resolved by `evaluate_readiness`.** It is derivable when the Target Issue or its referenced specification **names a public symbol, path or endpoint**; the Seam Set is the list of those names. Where it names none, the Seam Set is a missing Readiness Fact and T3 routes to clarification exactly as a missing test command does, in the same single Question Set. The derivation is a text test over the issue, decidable without a model — which is what makes `L3-CLR-16` assertable at L3 against a scripted model, and what keeps the failure mode above from simply moving one call upstream.

**2. `confirm_seam` asserts; it does not ask.** It performs no model call and takes no branch a correct run can reach. Its refusal is an internal invariant violation, and it ends the Attempt `failed`, non-transient, with an explanatory comment — not a process exit. [ADR 0007](0007-upstream-skill-text-is-answered-never-rewritten.md) chose the process exit for its own start-time failure on the stated grounds that *"no Attempt is open, so there is nothing to classify"*. Here one is: `claim_labels` has already applied `agent-running` and removed `needs-info`, so exiting would strand an issue wearing the agent's labels with no explanation of why.

**3. The Seam Set rides the Attempt Header, and the skill text is untouched.** [ADR 0007](0007-upstream-skill-text-is-answered-never-rewritten.md) forbids editing the sentence out, so the Worker **answers** it instead: the Attempt Header supplies the referent the instruction refers to, alongside the verbatim skill, the way a Reviewer Brief supplies the inputs a Role Block names. Because `L3-IMP-4` holds the Attempt Header out of compaction, the referent survives a long tool loop — the agreement cannot evaporate mid-node and leave the model reading *"confirm them with the user"* with nothing behind it.

**4. It authorises; it does not verify.** The Seam Set names boundaries and nothing afterwards checks that the tests landed at them. Validation Evidence cannot support the check — JUnit XML carries test identifiers, not the interfaces they exercised — and the only mechanism that could is the Standards reviewer, which would mean handing it a referent §3 never asks for. So the Seam Set is **disclosed** instead, in the pull request body, where §12 already requires the Carried Decisions used.

## Considered options

**A mandatory human gate on every issue.** No Attempt enters `implement` without a Carried Decision from a human. Rejected on cost, three times over: an obligatory round-trip per issue; one of the three Clarification Rounds spent before a line of code exists; and a direct contradiction with `L3-CLR-1` (*"Clear task, everything documented → Straight to implementation, no comment"*), which is one of the nine scenarios [#8](https://github.com/lbacik/coding-agent/issues/8) required and therefore the more expensive row to amend.

**No gate at all** — seams always derived, `L3-CLR-16` and `L3-CLR-17` deleted. Rejected: it removes the only brake on precisely the case that needs one, an issue written vaguely enough that no boundary can be read out of it.

**A model call proposes the Seam Set, the harness records it.** Rejected: it relocates the prototype's failure one call upstream. A model that did not notice it had no agreement will equally not notice that it named a boundary the issue never mentions, and its Seam Set reads identically in both cases. "Derivable" has to be decidable without a model or it is not a gate.

**A separate `confirm_seam` that both resolves and escalates**, as the contract first drew it. Rejected on the clarification budget: an issue missing both a test command and a derivable seam would spend **two** of three rounds on what one comment could ask, because §9 permits only one clarification comment per Attempt and two facts resolved in two different nodes cannot share it.

**A narrower Seam Fingerprint** over the acceptance criteria and referenced specification alone, so an editorial change to the issue body does not void a recorded seam decision. Rejected: it introduces a second quantity versioning one issue, and `Drift` is already defined as a change in Fingerprint. Two measures of the same thing in one codebase is the defect the glossary named `Gate` rather than *checkpoint* to avoid.

**A `seams` field in the Project Profile.** Rejected: a seam is a property of the task, not the project, so the field would be too coarse to authorise anything; and being in the profile would pin it into the Validation Contract at the Base Revision, leaving an Attempt whose task *is* moving a testing boundary unable to move it.

## Consequences

**The transition table gains nothing.** T2 already guards on *"All Readiness Facts resolved"* and T3 already fires on *"Missing Readiness Fact"*, so a missing seam routes itself. A design that needs no new transition is evidence it sits where the contract already had a place for it.

**`L3-CLR-16` is amended and needs a companion row.** It becomes *"no valid Carried Decision **and no seam derivable from the Target Issue**"*. Without the positive case beside it — `L3-CLR-20`, the Target Issue names the interface under test and the Attempt goes straight to implementation with no comment — the amended row has nothing bounding it against `L3-CLR-1`, and the collision this ADR resolves would survive in the matrix.

**`L3-CLR-17` stands as written.** The binding is the whole Fingerprint, and a stale Carried Decision earns a fresh gate rather than a rescue. The cost is smaller than it looks: a Carried Decision exists only for the issues whose seam could not be derived, and a human clarifying such an issue commonly edits its body — which is `L3-CLR-13`, a valid answer read at the next Claim and recorded under the Fingerprint that Claim reads, not under the one it invalidated.

**A defect in the readiness-to-`confirm_seam` wiring fails every Target Issue identically**, which is the premise `Credential Failure` uses to justify halting the Worker. It does not halt here, because this is a defect in our code rather than a condition of the deployment, and there is no start-time check that would surface it earlier — halting would buy a faster stop without a better diagnosis.

**The Question Set proposes no candidate seams.** It asks. Proposing candidates would need a model call in the path part 1 keeps model-free.

Reversing this decision means letting the seam gate back inside the model's conversation. Any replacement must say how an unconfirmed seam becomes **visible**, given that the prototype's arms wrote tests at one and none of them said so.
