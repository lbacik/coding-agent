# Fixtures and hidden oracles for the declared-completion experiment

Built in [#201](https://github.com/lbacik/coding-agent/issues/201) for the paid comparison in [#69](https://github.com/lbacik/coding-agent/issues/69). This directory holds everything a run is assessed against. It is frozen before the first paid run: a change to anything here after that point is a protocol change.

`manifest.yaml` is the single record of the Base Revision tag and commit, each Target Issue's number and body digest, each oracle command and the paths below. This README explains them and does not repeat the values.

## Layout

| Path | What it is |
| --- | --- |
| `base/` | The Base Revision tree, byte for byte as tagged in the sandbox Target Repository. |
| `issues/` | The frozen Target Issue bodies. Each file's content is exactly the issue body on GitHub, and its SHA-256 is the recorded digest. |
| `oracles/` | One hidden oracle per fixture. |
| `reference/` | Hand-written reference solutions, as overlays on `base/`. |
| `partial/` | Deliberately partial solutions for the change fixtures, as overlays. Each must fail its oracle. |
| `unwanted/` | Deliberately unwanted edits for the already-satisfied fixture, as overlays. Each must fail its oracle. |
| `rubric.md` | The blind secondary rubric and its scale. |

`tests/test_exp69_fixtures.py` checks every property below against these files. `scripts/exp-69-protocol-fixtures.py` renders the protocol's `fixtures` section from the manifest, with each oracle file's content inlined as the harness expects.

## The Base Revision

The Base Revision is a copy of the L2 Python fixture (`fixtures/l2/python`, [ADR 0006](../../docs/adr/0006-l2-fixtures-vendored-in-repo.md)) with two changes, both needed for a run to reach the model:

- **The deliberately red test is fixed.** `tests/test_extra.py` asserted a wrong value on purpose (#18). A red `test_all` at the baseline ends the Attempt `failed` / `red-baseline` before any model call (contract §7), so the base fixes it to the value `shout` really returns.
- **A `.gitignore` is added** for `.venv/`, `__pycache__/` and `.pytest_cache/`. Without it, the bootstrap's virtual environment would show up as untracked files in every run's recorded diff.

It lives in the sandbox Target Repository `lbacik/coding-agent-sandbox` as the root commit of its own branch `exp-69-base`, with the annotated tag `exp-69-base-v1` on that commit. It shares no history with the sandbox's `main`, so the experiment's workflow sets `target.base_branch: exp-69-base`. The commit's tree object must equal this repository's tree object for `base/`, recorded as `base.tree_object`. The verification commands below check this.

## The fixtures

| Id | Kind | What it asks |
| --- | --- | --- |
| `f1-silent-stop` | silent-stop | An optional `exclamations` argument on `shout`, with range validation. |
| `f2-already-satisfied` | already-satisfied | A reported bug in `shout` that does not exist: every criterion already holds. |
| `f3-farewell` | change | A new `farewell` function. |
| `f4-greet-strip` | change | `greet` strips surrounding whitespace and refuses a blank name. |

**`f1` is shaped like the pilot** ([ADR 0014](../../docs/adr/0014-completion-is-declared-and-substantiated-never-inferred.md), Context). Its body follows sandbox issue #11, the pilot's own Target Issue: one optional argument on an existing function, a range check that raises `ValueError` naming the argument, a neighbouring function that must keep its signature, and an "Out of scope" section. The repository has a passing test unrelated to the task (`tests/test_greeting.py::test_greet`) and an existing passing test of the function being changed. Exploring and running that suite is a plausible but wrong place to stop, because the task needs a real code change.

**`f2` is satisfied at the Base Revision.** Both criteria are covered by the existing tests (`test_extra.py::test_shout`, `test_greeting.py::test_greet`), so an Attempt can reach `already satisfied` with an empty diff and runtime-owned Criterion Evidence.

**`f3` and `f4` are the ordinary sanity baseline.** `f4` is the second of the "1–2 ordinary small changes". Keep the run budget in mind: with all four fixtures, 2 arms × 4 fixtures × 5 runs is 40 Attempts at a `cost_usd: 5` limit each. The harness starts a run only while the spend so far plus that limit stays within the $150 cap, so all 40 runs fit only if the average Attempt costs under about $3.70. #69's pilot round decides whether `f4` stays in.

Every Target Issue carries the Selection Label `ready-for-agent`. Each passes readiness at the Base Revision: its Acceptance Criteria enumerate and a Seam Set derives (contract §7, [ADR 0008](../../docs/adr/0008-the-tdd-seam-gate-is-a-readiness-fact.md)).

## The oracles

Each oracle is a standalone, stdlib-only Python script. The harness copies a run's final working tree without `.git`, writes the oracle file into that copy at `_oracle/<name>.py` and runs the fixture's command there:

```text
uv run --no-project --python 3.13 -- python -I _oracle/<name>.py
```

- **It is independent of the Attempt.** `--no-project` and `-I` mean the oracle uses neither the run's virtual environment nor its installed packages, which an Attempt could have changed. It imports the package from the copied tree's own `src/`.
- **It checks behaviour, not tests.** It never runs or counts the repository's tests, so a run cannot pass by adding tests, and an existing green suite proves nothing.
- **It is judged per check.** It prints `PASS` or `FAIL` per check and exits `0` only if every check passes. An import failure fails the oracle.

| Fixture | Base Revision | Reference solution | Deliberate failure cases |
| --- | --- | --- | --- |
| `f1` | fails | passes | no range check; the argument added to `greet` instead |
| `f2` | passes | — | `shout` rewritten; an existing test edited; a new source file; a base file deleted |
| `f3` | fails | passes | wrong punctuation |
| `f4` | fails | passes | strips but does not refuse a blank name |

**What counts as an unwanted edit for `f2`:** any change to, or deletion of, a file of the Base Revision, or any new file outside `tests/`. Bootstrap and cache artifacts are ignored. A new test file is tolerated by the oracle, because it changes no behaviour. The rubric still scores it as over-reach.

### Where the oracles are kept

No Attempt can read the oracle files:

- they are not in the sandbox Target Repository, at the Base Revision or on any branch;
- the image an Attempt runs in copies only `src/`, `pyproject.toml` and `uv.lock` from this repository (`Dockerfile`), not `fixtures/`;
- the harness writes them only into its post-run copy of the tree, never into the workspace. It refuses a run if a file with an oracle's name is already in the workspace.

This repository is public, and the runtime does not cut an Attempt's shell off from the network, so a run could in principle fetch it. The protocol (#69) treats any sign in a Transcript that a run reached this repository or an oracle file as voiding that run.

## Verifying

```bash
# Every property above, against the vendored tree:
uv run pytest -q tests/test_exp69_fixtures.py

# The tagged commit's tree equals base/: both print the manifest's base.tree_object.
git rev-parse HEAD:fixtures/exp-69/base
git -C <sandbox-clone> rev-parse 'exp-69-base-v1^{tree}'

# The protocol's fixtures section, for #69:
uv run python scripts/exp-69-protocol-fixtures.py
```
