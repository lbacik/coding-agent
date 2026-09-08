---
status: accepted
---

# One pinned toolchain version per language, provisioned at build time only

The agent image carries exactly one Python, one PHP and one Node version — its **Supported Toolchain Matrix** — and never provisions a toolchain during an Attempt. A Project Profile requiring a version outside that matrix is an **Unsupported Environment**: the Attempt fails with that classification rather than asking a human for information it already supplied correctly. Decided in [Define container and Python PHP TypeScript project profiles](https://github.com/lbacik/coding-agent/issues/6).

## Considered options

**A version manager in the image (mise/asdf), resolving the profile's declared versions at Attempt start.** Rejected: it turns every Attempt into a toolchain install of unbounded duration, spent against the 60-minute per-Attempt ceiling, and it makes the image non-reproducible — two runs of the same image can then execute against different interpreters.

**Letting uv fetch Python interpreters while PHP and Node stay fixed.** Rejected despite being nearly free, because uv already provisions interpreters and would do this by default. An asymmetric rule — "the agent installs Python versions but not PHP versions" — is a rule nobody remembers and nobody can predict from the code. `UV_PYTHON_DOWNLOADS=never` is therefore set explicitly, so the symmetry is enforced rather than assumed.

## Consequences

A target repository requiring an unavailable version needs a different image, not a different issue comment. This is affordable only because one deployment serves one Target Repository: matching the image to the repository is a deployment-time decision, not a runtime one. If that scoping assumption is ever dropped, revisit this ADR first.

Lifting the restriction for Python is a one-flag change, which makes the decision cheap to reverse for that language and expensive for the other two — the reason the symmetry is written down here rather than left to whoever edits the Dockerfile next.
