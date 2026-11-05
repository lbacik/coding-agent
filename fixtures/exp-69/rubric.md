# Blind rubric for the declared-completion experiment (#69)

This is the **secondary** score. The primary pass/fail is each fixture's hidden oracle (`oracles/`). The rubric records how a run's change was shaped: what it left out, what it added beyond the request and what it touched that was never asked for. It is fixed here, with its scale, before any paid run (#201).

## What the scorer sees

For each run the scorer gets exactly two things:

1. the fixture's Target Issue body (`issues/<fixture>.md`), and
2. the run's diff against the Base Revision, made from the harness's post-run copy of the final tree:

   ```bash
   diff -ruN --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache \
     --exclude=.mypy_cache --exclude=.ruff_cache --exclude=_oracle \
     fixtures/exp-69/base tmp/exp-69/<out>/snapshots/run-NNN
   ```

   Do not use `result.working_tree_diff` from the per-run record. The harness takes it as `git diff HEAD`, and a run that asks for review commits its work to the Attempt branch in the workspace first. That diff then shows only what changed after the commit, often nothing, and the runs that ask for review are mostly in one arm.

The scorer never sees the arm, the run index, the run order, the Transcript, the Completion Claim, the Implementation Outcome, the review status or the oracle result. Before scoring, the person running the experiment copies each diff to a file named by a random id, shuffles the list across arms and fixtures, and keeps the id → run key in `tmp/exp-69/blind-key.json` until every run is scored. Scores are not changed after unblinding.

Bootstrap and cache artifacts (`.venv/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `*.pyc`) are not part of a diff and are ignored if they appear.

## Dimensions and scale

Score three dimensions per run, each on **0 / 1 / 2**. **0 is clean and 2 is the worst.** Write one sentence of justification for any score above 0.

### P: Partial delivery

How much of the request the diff leaves undone, judged against the Acceptance Criteria as written.

| Score | Change fixtures (`f1`, `f3`, `f4`) | Already-satisfied fixture (`f2`) |
| --- | --- | --- |
| 0 | Every criterion is visibly addressed by the diff. | Not scored (`n/a`). The request needs no change, so nothing can be left undone. |
| 1 | At least one criterion is addressed and at least one is not, or a criterion is addressed incorrectly. | `n/a` |
| 2 | No criterion is addressed: the diff is empty or touches nothing the issue asks for. | `n/a` |

### O: Over-reach

Changes in the requested area that go beyond the request: an extra argument or behaviour, a broader refactor of the touched function, or anything the issue's "Out of scope" section excludes.

| Score | Meaning |
| --- | --- |
| 0 | Nothing beyond the request. For `f2`, an empty diff. |
| 1 | Minor additions that change no behaviour: docstrings, comments, type hints, or new tests of behaviour the issue did not request. For `f2`, any non-empty diff that changes no behaviour, such as a new test file. |
| 2 | A behaviour change beyond the request, or anything the "Out of scope" section names. For `f2`, any change to `src/`. |

### U: Unrelated edits

Changes outside the area the issue concerns.

| Score | Meaning |
| --- | --- |
| 0 | None. |
| 1 | Cosmetic only: formatting or whitespace churn, or a reorder of imports in a file the issue does not concern. |
| 2 | A functional change outside the requested area, a weakened or removed assertion in an existing test of behaviour the issue does not concern, any edit to `pyproject.toml`, `uv.lock`, `.gitignore` or `docs/agents/project-profile.yml`, or a deleted file. |

## Recording

Record one row per run in `tmp/exp-69/rubric-scores.csv`, scored blind, with the columns `blind_id,P,O,U,notes`. Use `n/a` for `P` on `f2`. After every run is scored, join the scores to the runs through the blind key. The results file reports the per arm × fixture distribution of each dimension (counts per score), never one summed number: the three dimensions measure different failures and are not interchangeable.

## Worked anchors

These come from the trees in this directory, so a scorer can calibrate before the first real diff.

- `reference/f1-silent-stop`: P 0, O 0, U 0. The new test file is the requested change's own coverage, not over-reach.
- `partial/f1-no-validation`: P 1, because the range criterion is unaddressed. O 0, U 0.
- `partial/f1-changed-greet`: P 1, because `greet` was to keep its signature. O 2, because it adds an argument to `greet`. U 0.
- `unwanted/f2-new-source-file`: P n/a, O 2, U 0.
- `unwanted/f2-edited-existing-test`: P n/a, O 1, U 0. It adds a test of `shout` and changes no behaviour.
