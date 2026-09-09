---
status: accepted
---

# The Agent Identity is the repository owner in v1

The Worker authenticates as the **Target Repository owner's own GitHub account**, with a fine-grained personal access token owned by that account, scoped to the single Target Repository, and with the Workflows permission withheld. No dedicated machine account exists in v1. Decided in [Establish a workable Agent Identity credential for the Target Repository](https://github.com/lbacik/coding-agent/issues/10).

[Define agent GitHub identity and write authorization](https://github.com/lbacik/coding-agent/issues/9) §1 had chosen the opposite: a dedicated machine account invited to the Target Repository as a collaborator, authenticating with a fine-grained token. GitHub documents that combination as an unsupported scenario — *"Using fine-grained personal access token to contribute to repositories where the user is an outside or repository collaborator"* — a gap still present verbatim, unclosed since fine-grained tokens went GA in March 2025. The Target Repository is a personal repository, and a public one, which hits a second documented gap for good measure.

Two arrangements do work: the token owner writing to a repository the token owner **owns**, and an organization repository where the token owner is an org **member**. The account is the owner, and the repository is theirs, so the first one costs nothing to adopt. The purpose of v1 is a prototype that runs; a credential question is not a thing worth building an organization to answer before the first Attempt has ever been made.

## Considered options

**Move the Target Repository into a new organization, machine account as a member.** The only arrangement that keeps both a documented support guarantee and a token scoped to one repository, and the organization owner can lift the default per-token approval requirement. Rejected for v1: it restructures where the project lives in order to reach a property the prototype does not yet need, and it is a change that can be made later without discarding anything. It is the leading candidate for [#11](https://github.com/lbacik/coding-agent/issues/11).

**A classic personal access token with `repo` scope, machine account as collaborator.** GitHub's own documented fallback for exactly this case, and still fully supported with no announced sunset. Rejected: `repo` reaches every repository the account can reach, so scoping stops being a property of the token and becomes an administrative discipline — any repository the account is ever granted enters the blast radius of the *existing* token, with nobody deciding anything.

**A GitHub App installed by the repository owner.** The strongest permission model of the three, and both grounds on which [#9](https://github.com/lbacik/coding-agent/issues/9) rejected it turn out to be wrong: the bot's numeric id resolves through `GET /users/{app-slug}[bot]`, `author_association` is present on App-authored comments, and the published GraphQL schema admits `Bot` as an assignee. Installation tokens expire in an hour but may be re-minted at will. Rejected for v1 only on cost: it is the largest of the three changes and buys a separation the prototype has no user for yet. Its re-examination belongs in [#11](https://github.com/lbacik/coding-agent/issues/11), where the corrected facts are recorded.

**Prove the credential on a repository the machine account owns.** Cheapest of all, and passes S0 trivially. Rejected: it demonstrates an arrangement that does not occur in the deployment it is meant to derisk, so every slice from S3 onward would still rest on an untested assumption.

## Consequences

**The account no longer distinguishes the Worker's writing from a human's, so the Attempt Marker must.** This is the whole substance of the decision. `author.id == agent_identity_id` was the *first* test of a Qualifying Answer, and under a shared account it rejects the only human able to answer — every Clarification Round would exhaust itself against the Worker's own comment. Step 1 now rejects on the presence of an Attempt Marker instead, which forces markers to answer "is this me" — precisely what the contract previously forbade them to answer, on the grounds that a marker is forgeable. It is forgeable, and in v1 the only account able to forge one belongs to the only human authorised to answer, so the forgery has no victim. That reasoning expires the moment a second writer exists.

**Two abstentions now carry load and must not be relaxed casually.** The Worker never edits the Target Issue's body, and never applies the Selection Label. They were conveniences before; they are now the reason an issue-body edit is known to be a human's answer and the Selection Label is known to be a human's authorisation. Either one relaxed silently breaks the clarification loop rather than merely widening it.

**Assignment leaves the contract.** Assigning the Agent Identity is indistinguishable from the owner assigning themselves, and unassigning at a Terminal Outcome would strip the owner's own assignment. The §3 `Assignment` column is gone, `Assignment is retained` during clarification is gone, and §2 now excludes an issue with **any** assignee. Nothing is lost: labels authorised and the Run Ledger recorded, both before and after.

**Every commit the Worker makes is attributed to the owner in git history, permanently.** Labels can be corrected and comments deleted; commit authorship in a pushed branch cannot. The `Attempt:` trailer is the only durable evidence that a machine produced the commit, which raises it from a join key to the sole provenance record.

**The Worker consumes the owner's API rate limit**, shared with that human's own tooling, so startup checks for headroom rather than assuming the budget is its own.

**The Workflows exclusion becomes a startup check rather than an intention.** Because the owner's general-purpose tokens do carry `workflow`, the Worker refuses to start on a credential whose prefix is not `github_pat_` or whose `GET /user` response carries an `X-OAuth-Scopes` header. Both rest on observed API behaviour rather than on documentation, and S0 confirms them; the platform's refusal of a `.github/workflows/` push is undocumented in every credential arrangement, so it too is proved there rather than assumed.

Reversing this decision is the expected path, not a failure of it: [#11](https://github.com/lbacik/coding-agent/issues/11) settles the production arrangement once the prototype has run. A replacement must restore an identity test in Qualifying Answer step 1, and say what happens to Attempt numbering reconstructed across the change of identity — the disclosure the contract already owes for a rebuilt Run Ledger.
