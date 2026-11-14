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

## Addendum (2026-11-13): PHP is pinned at the minor level

An upstream that does not keep superseded builds cannot carry an exact package pin. Sury builds each architecture separately, publishes a different version string per arch, and keeps only the latest build, so an exact `php8.4-*=<version>` pin broke the arm64 build while amd64 still matched. The image therefore pins PHP to exactly one **minor** version (`PHP_MINOR`, via the `php<minor>-*` package names) and accepts whatever patch build sury currently offers. The exact patch baked into an image is an observed build output, not an input: the build records it in the Supported Toolchain Matrix (`toolchains.php.version`, queried from the built `php` binary). "One version per language" in this ADR means one minor per image from now on.

## Addendum (2026-11-14): the final image's toolchain selection belongs to deployment assembly

Build-time provisioning (never during an Attempt) remains required: matching the image to the repository stays a deployment-time decision, not a runtime one. What changes is *which* image carries *which* selection. The root Dockerfile's default output is now a reusable base image carrying only coding-agent, agent-installer and their own dependencies (Python/uv, Node). The base image's Python+uv (and Node) are the agent's own tools, not Target Project toolchains, and the base carries no Supported Toolchain Matrix at all. A derived deployment image provisions its Target Project's toolchains and generates its Matrix from an explicit runtime selection (`scripts/generate-toolchain-matrix.py --runtime ...`), so the Matrix describes the deployment — e.g. a PHP-only deployment's Matrix has no `python` entry even though the base Python is present. The generator must never infer supported runtimes from whatever is on PATH in a deployment build; with no `--runtime` flags it keeps the legacy auto-detect behaviour for local host use only.
