# Producer conformance (issue #260)

The coding-agent producer conformance suite for the operator contract,
at the pinned contract tag, runs against the built coding-agent image
in CI so drift from the contract fails the build.

**Pin.** `contract-pin.txt` holds the pinned `agent-operator-contract`
revision (`06ee46c`, contract major 1). The upstream `v1.0.0` tag that
adoption-plan step 1 was meant to publish — and with it the canonical
harness API and the published JSON Schemas — does not exist yet, so
this directory tracks the pinned revision instead:

- `schemas/*.schema.json` are that revision's `examples/agentctl/`
  fixture shapes written as JSON Schema (draft 2020-12). Every reply
  shape the in-process `agentctl` tests produce validates against
  them (`tests/test_conformance_schemas.py`). When `v1.0.0` lands,
  these files are replaced by the published documents; the file names
  and the loader (`src/coding_agent/conformance/schemas.py`) stay.
- `src/coding_agent/conformance/harness.py` implements the five
  harness API operations the issue names over the existing fake
  provider (`tests/support/scripted_model.py`) and fake GitHub
  (`tests/support/fake_tracker.py`): make an issue eligible, hold an
  Attempt at a phase, finish an Attempt with a given outcome, crash
  the process, stop the container.

## What runs where

| Check | In-repo (`pytest`) | CI against the built image |
| --- | --- | --- |
| `agentctl` reply shapes validate against the schemas | `tests/test_conformance_schemas.py` | same suite, plus `producer-suite.sh schema` |
| harness implements all five operations | `tests/test_conformance_harness.py` | same suite |
| real process death (`SIGKILL`) and `SIGTERM` stop | subprocess kill/stop over a real OS process | `producer-suite.sh kill` runs the same against a container (`docker kill -s KILL`, `docker stop`) |
| `docker exec --user agent` / `docker run` invocation | exact argv constructors asserted without a daemon | `producer-suite.sh exec` runs them for real |
| read-only root filesystem, `/tmp` writable, volume layout | `Dockerfile`/`compose.yaml` structural assertions | `producer-suite.sh rootfs` runs a real bootstrap under `--read-only` |
| discovery labels on the `worker` service only, never the image | `tests/test_conformance_harness.py` + `test_operator_listener.py` | `producer-suite.sh labels` via `docker inspect` |
| socket directory `0700`, socket `0600`, no network listener | live listener test + `assert_socket_permissions` | `producer-suite.sh socket` via `docker exec stat` |
| a deliberate deviation fails | `test_conformance_schemas_negative` (corrupted fixture rejected) + `check-drift.sh` | `producer-suite.sh drift` runs `check-drift.sh` |

This container has no Docker, so `producer-suite.sh` skips its
Docker sections gracefully when no daemon is reachable; CI (with a
daemon) runs them all. The in-repo tests never need Docker.

## Running it

```sh
uv run --locked pytest -q tests/test_conformance_schemas.py tests/test_conformance_harness.py
./conformance/producer-suite.sh --image coding-agent:verify
./conformance/check-drift.sh
```
