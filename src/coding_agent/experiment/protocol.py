"""The experiment protocol: a declarative document the harness validates.

Driven by a protocol file (YAML or JSON) that declares arms, fixtures (each
a Target Issue plus a Base Revision and its expected issue-body digest),
runs per cell, the run order interleaved across arms, the per-Attempt
``cost_usd`` limit, an overall dollar cap, an oracle command per fixture and
the Pinned Model the Attempts run under. The harness
(``coding_agent.experiment.harness``) validates a protocol before any paid
call and then runs the Attempts sequentially in the declared order.

This protocol is not Workflow Configuration (contract §2): no Workflow
Configuration field names an exit policy, and this module is never consulted
by the ``agent implement`` entry path.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

PROTOCOL_SCHEMA_VERSION = 1
"""The only protocol schema version this harness reads."""

KNOWN_EXIT_POLICIES = ("contract", "silence_ends_stage")
"""Exit policies a protocol arm may name (ADR 0019)."""


@dataclass(frozen=True)
class ProtocolArm:
    """One experiment arm: an id and the step exit policy it runs under."""

    id: str
    exit_policy: str = "contract"


@dataclass(frozen=True)
class ProtocolPinnedModel:
    """The Pinned Model every Attempt in the experiment runs under."""

    provider: str
    model: str
    effort: str


@dataclass(frozen=True)
class ProtocolFixture:
    """One fixture: a Target Issue, its Base Revision and its oracle."""

    id: str
    issue_number: int
    issue_body_digest: str
    base_revision: str
    oracle: tuple[str, ...]
    oracle_files: dict[str, str]


@dataclass(frozen=True)
class ProtocolRun:
    """One ordered run: an arm, a fixture and the 1-based run index."""

    arm: str
    fixture: str
    run: int


@dataclass(frozen=True)
class ExperimentProtocol:
    """A validated experiment protocol, ready for the harness to execute."""

    arms: tuple[ProtocolArm, ...]
    fixtures: tuple[ProtocolFixture, ...]
    runs_per_cell: int
    order: tuple[ProtocolRun, ...]
    per_attempt_cost_usd: float
    overall_cap_usd: float
    pinned_model: ProtocolPinnedModel

    def arm_ids(self) -> tuple[str, ...]:
        """The arm ids in declaration order."""
        return tuple(arm.id for arm in self.arms)

    def fixture_ids(self) -> tuple[str, ...]:
        """The fixture ids in declaration order."""
        return tuple(fixture.id for fixture in self.fixtures)

    def fixture_by_id(self, fixture_id: str) -> ProtocolFixture:
        """The fixture with ``fixture_id`` (validated present)."""
        for fixture in self.fixtures:
            if fixture.id == fixture_id:
                return fixture
        raise KeyError(f"unknown fixture {fixture_id!r}")

    def arm_by_id(self, arm_id: str) -> ProtocolArm:
        """The arm with ``arm_id`` (validated present)."""
        for arm in self.arms:
            if arm.id == arm_id:
                return arm
        raise KeyError(f"unknown arm {arm_id!r}")


class ProtocolError(ValueError):
    """A protocol file that fails validation, naming the offending path."""


def issue_body_digest(body: str) -> str:
    """The expected-digest function for a Target Issue body: hex SHA-256."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _require_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{path}: expected a mapping, got {type(value).__name__}")
    return value


def _require_list(value: object, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProtocolError(f"{path}: expected a list, got {type(value).__name__}")
    return value


def _require_str(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{path}: expected a non-empty string")
    return value


def _default_exit_policy(arm_id: str) -> str:
    if arm_id == "control":
        return "silence_ends_stage"
    return "contract"


def _parse_arm(value: object, index: int) -> ProtocolArm:
    path = f"arms[{index}]"
    if isinstance(value, str):
        arm_id = _require_str(value, path)
        return ProtocolArm(id=arm_id, exit_policy=_default_exit_policy(arm_id))
    mapping = _require_mapping(value, path)
    arm_id = _require_str(mapping.get("id"), f"{path}.id")
    raw_policy = mapping.get("exit_policy", _default_exit_policy(arm_id))
    if not isinstance(raw_policy, str) or raw_policy not in KNOWN_EXIT_POLICIES:
        raise ProtocolError(
            f"{path}.exit_policy: expected one of {list(KNOWN_EXIT_POLICIES)}, "
            f"got {raw_policy!r}"
        )
    return ProtocolArm(id=arm_id, exit_policy=raw_policy)


def _parse_fixture(value: object, index: int) -> ProtocolFixture:
    path = f"fixtures[{index}]"
    mapping = _require_mapping(value, path)
    fixture_id = _require_str(mapping.get("id"), f"{path}.id")
    raw_issue = mapping.get("issue_number")
    if isinstance(raw_issue, bool) or not isinstance(raw_issue, int) or raw_issue <= 0:
        raise ProtocolError(f"{path}.issue_number: expected a positive integer")
    digest = _require_str(mapping.get("issue_body_digest"), f"{path}.issue_body_digest")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
        raise ProtocolError(f"{path}.issue_body_digest: expected 64 hex characters")
    revision = _require_str(mapping.get("base_revision"), f"{path}.base_revision")
    raw_oracle = _require_list(mapping.get("oracle"), f"{path}.oracle")
    if not raw_oracle:
        raise ProtocolError(f"{path}.oracle: expected a non-empty command")
    oracle: list[str] = []
    for position, entry in enumerate(raw_oracle):
        oracle.append(_require_str(entry, f"{path}.oracle[{position}]"))
    raw_files = mapping.get("oracle_files", {})
    if not isinstance(raw_files, Mapping):
        raise ProtocolError(f"{path}.oracle_files: expected a mapping")
    oracle_files: dict[str, str] = {}
    for name, content in raw_files.items():
        if not isinstance(name, str) or not name or name.startswith("/") or ".." in name:
            raise ProtocolError(f"{path}.oracle_files: bad file name {name!r}")
        if not isinstance(content, str):
            raise ProtocolError(f"{path}.oracle_files[{name}]: expected text content")
        oracle_files[name] = content
    return ProtocolFixture(
        id=fixture_id,
        issue_number=raw_issue,
        issue_body_digest=digest.lower(),
        base_revision=revision,
        oracle=tuple(oracle),
        oracle_files=oracle_files,
    )


def _parse_run(value: object, index: int) -> ProtocolRun:
    path = f"order[{index}]"
    mapping = _require_mapping(value, path)
    arm = _require_str(mapping.get("arm"), f"{path}.arm")
    fixture = _require_str(mapping.get("fixture"), f"{path}.fixture")
    raw_run = mapping.get("run")
    if isinstance(raw_run, bool) or not isinstance(raw_run, int) or raw_run < 1:
        raise ProtocolError(f"{path}.run: expected a positive integer")
    return ProtocolRun(arm=arm, fixture=fixture, run=raw_run)


def parse_protocol(document: Mapping[str, Any]) -> ExperimentProtocol:
    """Validate a decoded protocol document, before any paid call.

    Every refusal names the offending path. Unknown top-level keys are
    ignored so a write-up-only annotation never blocks an execution.
    """
    raw_version = document.get("schema_version", PROTOCOL_SCHEMA_VERSION)
    if raw_version != PROTOCOL_SCHEMA_VERSION:
        raise ProtocolError(
            f"schema_version: expected {PROTOCOL_SCHEMA_VERSION}, got {raw_version!r}"
        )
    raw_arms = _require_list(document.get("arms"), "arms")
    if not raw_arms:
        raise ProtocolError("arms: expected a non-empty list")
    arms = tuple(_parse_arm(entry, index) for index, entry in enumerate(raw_arms))
    arm_ids = [arm.id for arm in arms]
    if len(set(arm_ids)) != len(arm_ids):
        raise ProtocolError(f"arms: duplicate arm ids in {arm_ids!r}")
    raw_fixtures = _require_list(document.get("fixtures"), "fixtures")
    if not raw_fixtures:
        raise ProtocolError("fixtures: expected a non-empty list")
    fixtures = tuple(
        _parse_fixture(entry, index) for index, entry in enumerate(raw_fixtures)
    )
    fixture_ids = [fixture.id for fixture in fixtures]
    if len(set(fixture_ids)) != len(fixture_ids):
        raise ProtocolError(f"fixtures: duplicate fixture ids in {fixture_ids!r}")
    raw_runs = document.get("runs_per_cell")
    if isinstance(raw_runs, bool) or not isinstance(raw_runs, int) or raw_runs < 1:
        raise ProtocolError("runs_per_cell: expected a positive integer")
    raw_order = _require_list(document.get("order"), "order")
    if not raw_order:
        raise ProtocolError("order: expected a non-empty run order")
    order = tuple(_parse_run(entry, index) for index, entry in enumerate(raw_order))
    for entry in order:
        if entry.arm not in arm_ids:
            raise ProtocolError(f"order: unknown arm {entry.arm!r}")
        if entry.fixture not in fixture_ids:
            raise ProtocolError(f"order: unknown fixture {entry.fixture!r}")
        if entry.run > raw_runs:
            raise ProtocolError(
                f"order: run {entry.run} exceeds runs_per_cell {raw_runs}"
            )
    expected_cells = len(arms) * len(fixtures) * raw_runs
    if len(order) != expected_cells:
        raise ProtocolError(
            f"order: expected {expected_cells} runs "
            f"({len(arms)} arms x {len(fixtures)} fixtures x {raw_runs}), "
            f"got {len(order)}"
        )
    seen: dict[tuple[str, str, int], int] = {}
    for entry in order:
        key = (entry.arm, entry.fixture, entry.run)
        seen[key] = seen.get(key, 0) + 1
    duplicates = sorted(key for key, count in seen.items() if count > 1)
    if duplicates:
        raise ProtocolError(f"order: duplicate runs {duplicates!r}")
    raw_limit = document.get("per_attempt_cost_usd")
    if (
        isinstance(raw_limit, bool)
        or not isinstance(raw_limit, (int, float))
        or not float(raw_limit) > 0
    ):
        raise ProtocolError("per_attempt_cost_usd: expected a positive number")
    raw_cap = document.get("overall_cap_usd")
    if (
        isinstance(raw_cap, bool)
        or not isinstance(raw_cap, (int, float))
        or not float(raw_cap) > 0
    ):
        raise ProtocolError("overall_cap_usd: expected a positive number")
    raw_model = _require_mapping(document.get("pinned_model"), "pinned_model")
    provider = _require_str(raw_model.get("provider"), "pinned_model.provider")
    model = _require_str(raw_model.get("model"), "pinned_model.model")
    effort = _require_str(raw_model.get("effort"), "pinned_model.effort")
    return ExperimentProtocol(
        arms=arms,
        fixtures=fixtures,
        runs_per_cell=raw_runs,
        order=order,
        per_attempt_cost_usd=float(raw_limit),
        overall_cap_usd=float(raw_cap),
        pinned_model=ProtocolPinnedModel(
            provider=provider, model=model, effort=effort
        ),
    )


def load_protocol(path: Path) -> ExperimentProtocol:
    """Load and validate the protocol file at ``path`` (YAML or JSON)."""
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        document = json.loads(text)
    elif suffix in (".yaml", ".yml"):
        document = yaml.safe_load(text)
    else:
        raise ProtocolError(
            f"{path}: expected a .yaml, .yml or .json protocol file"
        )
    if not isinstance(document, Mapping):
        raise ProtocolError(f"{path}: expected a mapping at the top level")
    return parse_protocol(document)
