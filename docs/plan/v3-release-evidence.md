# Workflow Configuration v3 release evidence

**Release:** `coding-agent 0.3.0` (first v3 release).
**Status: current.** This is the release record for issue #404: the exact
versions, formats, source commits and reviewed policies the v3 release
ships, where each of the 36 acceptance rows SKV3-01…SKV3-36 gets its
executable evidence, and which gates ran where. The normative row
definitions live in the
[transition resolution](https://github.com/lbacik/coding-agent/issues/378#issuecomment-6877971121);
the in-repo row-to-test mapping lives in
[the step-workflow acceptance matrix](./step-workflow-acceptance-matrix.md#skv3-v3-skill-bundle-acceptance-transition-resolution).
That mapping is the published coverage claim; this document records the
release it covers and how to reproduce it.

## 1. Exact release identity

| Component | Pinned value | Where it is pinned |
| --- | --- | --- |
| coding-agent | `0.3.0` (`pyproject.toml`) | `pyproject.toml`, tags `v0.3.0` |
| Base Python image | `python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e` | `Dockerfile` (`ARG PYTHON_IMAGE`) |
| Base Node | `22.23.2-1nodesource1` (`NODE_MAJOR=22`) | `Dockerfile` |
| Base uv | `0.5.29` | `Dockerfile` (`ARG UV_VERSION`) |
| agent-installer | `0.8.0` — first release satisfying `agent-installer#74` (complete Git artifact provenance, freshly verified installed copies, offline subset verification) | `docker/agent-installer/package.json`, `src/coding_agent/skillbundle/installer.py` (`REAL_INSTALLER_VERSION`) |
| Operator contract | `v1.0.4` (`commit 5e17e8c34670cc26f9174d7a7deecc93037f179c`) | `contract-pin.txt`, `conformance/contract-pin.txt` |
| Workflow Configuration | `version: 3` only (`version: 2` refused, exit 1) | `src/coding_agent/workflow/*`, `schemas/workflow-v3.schema.json` |
| Generated records | `requirements.json` / `manifest.json` / `validation.json`, each `schemaVersion: 1`, canonical JSON | `src/coding_agent/skillbundle/records.py` (`SCHEMA_VERSION`) |
| Skill Bundle Policy | `kind: coding-agent.skill-bundle-policy`, `schemaVersion: 1` | `src/coding_agent/skillbundle/policy.py`, `docker/deployment/skill-policy.json` |
| Effective Configuration record | `version: 2` (separate from YAML v3) | `src/coding_agent/workflow/effective.py` |
| Activation delivery framing | `skill-delivery v1` | `src/coding_agent/skillbundle/delivery.py` (`DELIVERY_FRAMING_VERSION = 1`) |
| Breaking-change notice | [v3 breaking-change notice](../ops/v3-breaking-change-notice.md) (`coding-agent 0.3.0`) | `docs/ops/v3-breaking-change-notice.md` |

`tests/test_v3_release_evidence.py::test_release_version_pins` asserts the
code-owned half of this table; bumping any of those constants without
updating this record fails the suite on purpose.

## 2. Supported installer formats

| Input / report | Accepted | Rejected |
| --- | --- | --- |
| Git source | HTTPS only, `https://` or `git+https://` spelling, normalised to one Source Identity; no userinfo, query or fragment | SSH, local directories, vendored content, credential-bearing locators (refused before any subprocess or log line, SKV3-03) |
| Install report | `agent-installer` report `schemaVersion: 3` only | Any other version, duplicate keys, malformed digests, cross-reference mismatches (SKV3-11) |
| Verify report | `schemaVersion: 3` only | Same refusals as above |
| Content hash | `agent-installer-tree-v2` only | Legacy unmarked shapes (SKV3-12 inventory binding) |
| Managed-store state | `installer_state_version 4` (released installer); anything else is the fake seam for unit tests | Host-installed variants never auto-approved (SKV3-36) |

## 3. Source commits and reviewed policies

The reference deployment bakes the established four-skill selection:

- Source: `git+https://github.com/mattpocock/skills.git`
- Commit: `3cca18b368ae95cdbdebbff572ccafa662551015`
- Members: `implement`, `tdd`, `code-review`, `codebase-design`
- Declaration: [`docker/deployment/workflow.yml`](../../docker/deployment/workflow.yml)
- Reviewed policy: [`docker/deployment/skill-policy.json`](../../docker/deployment/skill-policy.json)
  (`sha256 bc2e162d792b0903167ca0aed94722d6ab41d1f97472f103d178371fb594b6b6`)

| Skill | `SKILL.md` (`skillSha256`) | `requires` | Companion dispositions |
| --- | --- | --- | --- |
| `implement` | `6d3fd9e83b8f36e5213854779db49b256a457a7ebb4a503e53fa7dcff696adc3` | `code-review`, `tdd` | `agents/openai.yaml` → `not-delivered` |
| `tdd` | `cb01f66bebfaa25fa1f88e6b7e769cd9fd9f35b1120b8563749820738814c927` | `codebase-design`, `code-review` | `agents/openai.yaml` → `not-delivered`; `mocking.md`, `tests.md` → `inject` |
| `code-review` | `47f4e52c21694def9c7c11cbfbf891ca35eac7a93e395797515be3c8a409ae50` | — | `agents/openai.yaml` → `not-delivered` |
| `codebase-design` | `2c20617f87ec8af6a434859f381b2f061a69b530444e74eb39e78bb016a6d1e2` | — | `agents/openai.yaml` → `not-delivered`; `DEEPENING.md`, `DESIGN-IT-TWICE.md` → `inject` |

Historical per-file decisions carry forward only for matching reviewed
content (same source identity, `SKILL.md` digest and complete inventory);
they never auto-approve host HEAD, a changed `SKILL.md`, added metadata or
assets, or changed executable bits. Administrative files such as
`agents/openai.yaml` always need explicit dispositions.

The second-source, nested-`agents/openai.yaml`, hidden-file and
host-conflict cases (SKV3-36) are proved by the real-installer suite
(`tests/test_skill_bundle_real_installer.py`) against recorded immutable
Git fixtures through `insteadOf` mappings — no network is touched, and no
host-installed variant is auto-approved.

## 4. Row-to-test mapping

The published mapping is the SKV3 section of
[the step-workflow acceptance matrix](./step-workflow-acceptance-matrix.md#skv3-v3-skill-bundle-acceptance-transition-resolution):
one row per SKV3-01…SKV3-36 with the required result and the executable
evidence. Seams (per #385 Testing Decisions):

- **Phase-API seam** (`cli.main` for `config validate`,
  `prepare-skill-bundle`, `verify-skill-bundle` and `run` startup against
  real deployment roots under `tmp_path`; fake in-process installer
  adapter): SKV3-01…07, SKV3-09…27, SKV3-29…32, SKV3-35.
- **Worker-loop seam** (`run_worker_loop` with `ScriptedModel`,
  `FakeStepTracker`, real `RunLedger`): SKV3-17…19, SKV3-25, SKV3-28…30.
- **Release gates outside the unit suite** (released installer, real store,
  real image): SKV3-08, SKV3-10 (shared-store half),
  SKV3-28 (crash/restart half), SKV3-33, SKV3-34, SKV3-36.

Rows marked **release gate** in the matrix are proved outside the unit
suite and the unit column names the in-repo tests that pin the same
behaviour deterministically.

## 5. Local CLI evidence without Docker

`tests/test_v3_release_evidence.py::test_local_cli_phases_against_a_real_store`
runs the whole local sequence in one place against a real store:

1. `agent config validate --config <tmp>/workflow.yml` → exit 0, no
   secrets set (SKV3-20).
2. `agent prepare-skill-bundle --mode installed --config … --policy …
   --bundle-dir … --home …` → exit 0, publishes `requirements.json`,
   `manifest.json`, `validation.json` as one completed generation, each
   `schemaVersion: 1`.
3. `agent verify-skill-bundle` with the same roots → exit 0, offline and
   read-only.
4. `run_worker` startup with a real `SkillDeploymentCheck`, real toolchain
   matrix and an empty tracker → exit 0, full offline re-verification
   before the (stubbed) live checks, no Attempt opened.

Reproduce it without Docker:

```sh
env -u GITHUB_TOKEN -u ANTHROPIC_API_KEY -u OPENAI_API_KEY \
  uv run --locked pytest -q tests/test_v3_release_evidence.py
```

The baked reference inputs' offline half is pinned separately by
`tests/test_deployment_baked.py` (committed `docker/deployment/` files
validate secret-free and declare exactly the pinned four-member bundle).

## 6. Real derived-image proof

`scripts/verify-image.sh` proves the image half. It builds the reusable
base (`coding-agent:base`) and the reference derived deployment
(`--target deployment`), then checks, against the finished images rather
than builder logs:

- Baked package: base carries no toolchain/Matrix/bundle; the deployment
  publishes the pinned Matrix, the baked `workflow.yml`/prompts/policy and
  the prepared bundle records; `verify-skill-bundle` passes offline
  against the final roots (`--network none`); installed skills are exactly
  the four bundled ones (SKV3-20, SKV3-23, SKV3-33 baked half, SKV3-34).
- External package: an operator-prepared package at non-baked absolute
  roots (copied workflow/prompts/policy plus a fresh `--mode installed`
  preparation against the image's own store) verifies identically;
  final-root verification runs after the last copy, and the rendered `run`
  invocation uses explicit `--config/--policy/--bundle-dir` paths rather
  than baked defaults (SKV3-23, SKV3-33 external half).
- Copy mismatch and healing: a relocated identical tree verifies; a
  byte-edited copy refuses until re-prepared (SKV3-22, SKV3-23).
- Missing member: an induced build declaring `ghost-skill` fails at
  preparation naming the member (SKV3-09/16 build-failure half).
- Toolchains: Python/PHP/TypeScript fixtures bootstrap under a read-only
  rootfs as the non-root runtime user; `uv` refuses out-of-matrix
  interpreters instead of downloading (SKV3-34; L2 fixtures via
  `scripts/verify-l2-fixtures.sh`).

Run it (needs Docker; this container has none):

```sh
./scripts/verify-image.sh
./scripts/verify-l2-fixtures.sh
```

A builder-stage log is never the acceptance proof: every deployment check
above re-runs the command against the finished image, including the
induced failure.

## 7. Retained gates

| Gate | Where it runs | Status for this release |
| --- | --- | --- |
| `uv run --locked pytest -q` | CI `check`, local | Must pass (this change keeps it green; no coverage deleted) |
| `uv run --locked mypy src tests` | CI `check`, local | Must pass (strict; new code is fully annotated) |
| Generated-schema equality | unit suite | Covered; `schemas/workflow-v3.schema.json` stays generated-and-committed |
| Secret-free example validation | CI (`config validate` step, no secrets in env) | Covered; `examples/workflows.yaml` validates with no secrets |
| `scripts/verify-image.sh` | Docker host / release gate | Extended here with the external-package proof; a human runs it (no Docker in this container) |
| `scripts/verify-l2-fixtures.sh` | Docker host / release gate | Retained; a human runs it |
| `conformance/check-drift.sh` | CI `conformance` | Retained |
| Producer suite vs base image | CI `conformance` (`--image coding-agent:base`) | Retained |
| L1 gateway rows (`-m l1_gateway`) | Manual before each release tag | Explicitly not run in CI; not claimed by this change |

No existing graph, limit, effect, integrity, credential-stripping,
footprint or ref-guard coverage was deleted to get a green run (SKV3-35).
