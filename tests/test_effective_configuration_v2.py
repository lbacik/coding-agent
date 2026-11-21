"""Effective Configuration v2: identity core, canonical bytes and digest.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.10 (the
Attempt pins its Effective Configuration in full: the canonical compiled
Workflow, the file-level settings, every operator prompt file's bytes, every
available Skill Bundle member's provenance, inventory and selected review,
the exact activation deliveries with their framing version, and the Pinned
Model — one SHA-256 digest covers all of it; environment-variable values are
never part of it).

Matrix row ids: CFG-41 (shape), SKV3-26 (sensitivity: the digest moves with
every identity input), SKV3-27 (insensitivity: the digest ignores everything
outside the identity core).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from coding_agent.ledger.store import digest_effective_configuration
from coding_agent.provider.config import policy_for_pin
from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.skillbundle.delivery import DELIVERY_FRAMING_VERSION, delivery_for_review
from coding_agent.skillbundle.deployment import (
    VerifiedRunInputs,
    _overlay_records,
    prepare_skill_bundle,
    verify_run_inputs,
)
from coding_agent.skillbundle.installer import (
    FAKE_REPORT_SCHEMA_VERSION,
    FakeInstallerAdapter,
    ProducerIdentity,
    write_fake_store,
)
from coding_agent.skillbundle.policy import PolicyFile, PolicyReference, PolicyReview
from coding_agent.workflow.effective import (
    BundleMember,
    InventoryFile,
    MemberOverlay,
    build_effective_configuration,
    dumps_effective_canonical,
    is_full_effective_configuration,
    sha256_effective_core,
)
from coding_agent.workflow.schema import (
    Agent,
    AttemptCeiling,
    InputDecl,
    Instruction,
    ModelDecl,
    ModelPrices,
    RouteTarget,
    WorkflowStep,
    TargetRepository,
    WorkerSettings,
    Workflow,
)
from coding_agent.workflow.schema import WorkflowConfiguration
from coding_agent.workflow.configuration import (
    LoadedWorkflowConfiguration,
    load_workflow_configuration,
)
from support.effective_config import TEST_PIN, TEST_PRICES
from support.run_inputs import make_run_inputs
from support.skill_bundle import (
    COMMIT_A,
    COMMIT_B,
    COMMIT_C,
    SOURCE_A,
    SOURCE_B,
    DeclaredMember,
    build_policy_document,
    fake_store_spec_for,
    member_files,
    minimal_config_text,
    skills_entries_for,
    write_policy_file,
)

ALPHA_BODY = "# alpha\n\nSynthetic alpha body.\n"
BETA_BODY = "# beta\n\nSynthetic beta body.\n"
GUIDE_TEXT = "Guide companion text.\n"
EXTRA_TEXT = "Extra companion text.\n"
NOT_DELIVERED_TEXT = "Tool script bytes.\n"
PROMPT_TEXT = "Greet prompt bytes.\n"


def _sha256_hex(text: str) -> str:
    """The SHA-256 hex digest of ``text``'s UTF-8 bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _alpha_review(
    *,
    companions: tuple[tuple[str, str], ...] = (
        ("docs/guide.md", GUIDE_TEXT),
        ("docs/extra.md", EXTRA_TEXT),
    ),
    notice: str = "Use beta for cross checks.",
    not_delivered_sha: str | None = None,
    reason: str = "synthetic review",
) -> PolicyReview:
    """The selected review for ``alpha``: two inject companions in policy
    order, one operator notice and one not-delivered file."""
    files = [
        PolicyFile(
            path=path,
            sha256=_sha256_hex(text),
            executable=False,
            disposition="inject",
            reason="synthetic inject",
        )
        for path, text in companions
    ]
    files.append(
        PolicyFile(
            path="bin/tool",
            sha256=not_delivered_sha or _sha256_hex(NOT_DELIVERED_TEXT),
            executable=False,
            disposition="not-delivered",
            reason="synthetic not-delivered",
        )
    )
    return PolicyReview(
        source_identity=SOURCE_A,
        name="alpha",
        skill_sha256=_sha256_hex(ALPHA_BODY),
        requires=(),
        files=tuple(files),
        references=(
            PolicyReference(
                origin="SKILL.md",
                text="See /beta for details.",
                classification="unavailable-nonrequired-reference",
                target="beta",
                reason="synthetic ruling",
                notice=notice,
            ),
        ),
        reason=reason,
    )


def _beta_review() -> PolicyReview:
    """The selected review for ``beta`` (on-demand-only): nothing delivered
    beyond ``SKILL.md``, no notices."""
    return PolicyReview(
        source_identity=SOURCE_B,
        name="beta",
        skill_sha256=_sha256_hex(BETA_BODY),
        requires=(),
        files=(),
        references=(),
        reason="synthetic review",
    )


def _alpha_member(
    *,
    commit: str = COMMIT_A,
    body: str = ALPHA_BODY,
    review: PolicyReview | None = None,
) -> BundleMember:
    """The ``alpha`` member: provenance, full inventory and selected review."""
    decided = review if review is not None else _alpha_review()
    return BundleMember(
        name="alpha",
        source_identity=SOURCE_A,
        resolved_commit=commit,
        declared_ref=None,
        relative_source_path="skills/alpha",
        store_relative_path="alpha",
        files=(
            InventoryFile(path="SKILL.md", sha256=_sha256_hex(body), executable=False),
            InventoryFile(path="bin/tool", sha256=_sha256_hex(NOT_DELIVERED_TEXT), executable=False),
            InventoryFile(path="docs/extra.md", sha256=_sha256_hex(EXTRA_TEXT), executable=False),
            InventoryFile(path="docs/guide.md", sha256=_sha256_hex(GUIDE_TEXT), executable=False),
        ),
        overlays=(),
        review=decided,
    )


def _beta_member(*, commit: str = COMMIT_B, body: str = BETA_BODY) -> BundleMember:
    """The ``beta`` member: on-demand-only, no static instruction names it."""
    return BundleMember(
        name="beta",
        source_identity=SOURCE_B,
        resolved_commit=commit,
        declared_ref=None,
        relative_source_path="skills/beta",
        store_relative_path="beta",
        files=(InventoryFile(path="SKILL.md", sha256=_sha256_hex(body), executable=False),),
        overlays=(),
        review=_beta_review(),
    )


def _alpha_delivery(review: PolicyReview | None = None) -> str:
    """The exact activation delivery for ``alpha`` under the versioned framing."""
    return delivery_for_review(
        name="alpha",
        skill_md_text=ALPHA_BODY,
        contents={
            "SKILL.md": ALPHA_BODY.encode("utf-8"),
            "docs/guide.md": GUIDE_TEXT.encode("utf-8"),
            "docs/extra.md": EXTRA_TEXT.encode("utf-8"),
            "bin/tool": NOT_DELIVERED_TEXT.encode("utf-8"),
        },
        review=review if review is not None else _alpha_review(),
    )


def _fixed_workflow() -> Workflow:
    """The fixed fixture Workflow every record in this file pins."""
    return Workflow(
        id="implement",
        model="sonnet",
        input=InputDecl(type="issue", label="agent:implement"),
        ceiling=AttemptCeiling(cost_usd=6, effective_tokens=1500000, tool_calls=300, wall_clock_minutes=30),
        outcomes={"completed": ("done",)},
        start="work",
        defaults_routes={
            "error": RouteTarget(end="failed"),
            "limit-exceeded": RouteTarget(end="unfinished"),
        },
        steps={
            "work": WorkflowStep(
                id="work",
                agent=Agent(
                    instructions=(Instruction(skill="alpha", prompt="prompts/greet.md"),),
                    tools=("workspace-read",),
                    results={"done": {}},
                ),
                max_executions=1,
                routes={"done": RouteTarget(end="completed")},
            ),
        },
    )


def _fixed_model_decl(*, api_key_env: str = "ANTHROPIC_API_KEY") -> ModelDecl:
    """The ``models`` entry the fixed fixture Workflow resolves to."""
    return ModelDecl(
        id="sonnet",
        provider="anthropic",
        model="claude-sonnet-5",
        endpoint="messages",
        effort="medium",
        api_key_env=api_key_env,
        prices=ModelPrices(
            input=TEST_PRICES.input,
            output=TEST_PRICES.output,
            cache_read=TEST_PRICES.cache_read,
            cache_write=TEST_PRICES.cache_write,
        ),
    )


def _fixed_config(workflow: Workflow, *, source: Path = Path("workflows.yaml")) -> WorkflowConfiguration:
    """The fixed fixture configuration carrying ``workflow``."""
    return WorkflowConfiguration(
        version=3,
        target=TargetRepository(
            kind="github",
            repository="example-owner/example-repo",
            base_branch="main",
            token_env="GITHUB_TOKEN",
        ),
        models={workflow.model: _fixed_model_decl()},
        worker=WorkerSettings(),
        workflows={workflow.id: workflow},
        skills=(),
        source=source,
    )


def _fixed_run_inputs(
    members: Sequence[BundleMember],
    bodies: Mapping[str, str],
    *,
    prompt_text: str = PROMPT_TEXT,
    workflow: Workflow | None = None,
    config: WorkflowConfiguration | None = None,
) -> VerifiedRunInputs:
    """Verified run inputs for the fixed fixture, varying members and bodies."""
    decided_workflow = workflow if workflow is not None else _fixed_workflow()
    decided_config = config if config is not None else _fixed_config(decided_workflow)
    loaded = LoadedWorkflowConfiguration(
        path=decided_config.source, raw=b"", config=decided_config
    )
    locators: list[str] = []
    for step in decided_workflow.steps.values():
        if step.agent is None:
            continue
        for instruction in step.agent.instructions:
            if instruction.prompt is not None and instruction.prompt not in locators:
                locators.append(instruction.prompt)
    return make_run_inputs(
        loaded,
        prompt_raws={locator: prompt_text.encode("utf-8") for locator in locators},
        members=members,
        bodies=bodies,
    )


def _effective_record(
    *,
    members: tuple[BundleMember, ...] | None = None,
    activations: dict[str, str] | None = None,
    prompt_text: str = PROMPT_TEXT,
    source: Path = Path("workflows.yaml"),
) -> dict[str, object]:
    """A full v2 Effective Configuration record for the fixed fixture."""
    workflow = _fixed_workflow()
    decided_members = members if members is not None else (_alpha_member(), _beta_member())
    decided_activations = (
        activations
        if activations is not None
        else {"alpha": _alpha_delivery(), "beta": BETA_BODY}
    )
    config = _fixed_config(workflow, source=source)
    run_inputs = _fixed_run_inputs(
        decided_members, decided_activations, prompt_text=prompt_text, workflow=workflow, config=config
    )
    return build_effective_configuration(
        run_inputs, workflow=workflow, pin=TEST_PIN, prices=TEST_PRICES
    )


def _record_digest(record: dict[str, object]) -> str:
    """The one SHA-256 digest the Run Ledger pins beside the record."""
    return digest_effective_configuration(record)


def _declared_alpha_beta() -> list[DeclaredMember]:
    """Two declared members on distinct sources, pinned to full commits."""
    return [
        DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A, ref=COMMIT_A),
        DeclaredMember(name="beta", source=SOURCE_B, commit=COMMIT_B, ref=COMMIT_B),
    ]


def _prepare_install(
    root: Path, members: list[DeclaredMember], installer: FakeInstallerAdapter
) -> tuple[Path, Path, Path, Path]:
    """Prepare through the installer-adapter port; returns config/policy/bundle/home."""
    root.mkdir(parents=True, exist_ok=True)
    config = root / "workflow.yml"
    config.write_text(minimal_config_text(skills_entries_for(members)), encoding="utf-8")
    policy = root / "skill-policy.json"
    write_policy_file(policy, build_policy_document(members))
    bundle_dir = root / "bundle"
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    for member in members:
        installer.seed_member(member.source, member.name, member_files(member))
    loaded, faults = load_workflow_configuration(config)
    assert loaded is not None, [str(fault) for fault in faults]
    prepared = prepare_skill_bundle(
        config=loaded,
        policy=policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="install",
        installer=installer,
    )
    assert prepared.ok, [str(fault) for fault in prepared.faults]
    return config, policy, bundle_dir, home


def _prepare_installed(
    root: Path,
    members: list[DeclaredMember],
    *,
    policy_doc: dict[str, object] | None = None,
) -> tuple[Path, Path, Path, Path]:
    """Prepare by adopting the managed store offline; returns config/policy/bundle/home."""
    root.mkdir(parents=True, exist_ok=True)
    config = root / "workflow.yml"
    config.write_text(minimal_config_text(skills_entries_for(members)), encoding="utf-8")
    policy = root / "skill-policy.json"
    write_policy_file(policy, policy_doc if policy_doc is not None else build_policy_document(members))
    bundle_dir = root / "bundle"
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    write_fake_store(home, fake_store_spec_for(members))
    loaded, faults = load_workflow_configuration(config)
    assert loaded is not None, [str(fault) for fault in faults]
    prepared = prepare_skill_bundle(
        config=loaded, policy=policy, bundle_dir=bundle_dir, home=home, mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert prepared.ok, [str(fault) for fault in prepared.faults]
    return config, policy, bundle_dir, home


def _verified_identities(
    config: Path, policy: Path, bundle_dir: Path, home: Path
) -> tuple[tuple[BundleMember, ...], dict[str, str]]:
    """The verified members and activation bodies for one deployment."""
    loaded, faults = load_workflow_configuration(config)
    assert loaded is not None, [str(fault) for fault in faults]
    result = verify_run_inputs(
        config=loaded, policy=policy, bundle_dir=bundle_dir, home=home
    )
    assert isinstance(result, VerifiedRunInputs), result
    return tuple(result.members), dict(result.registry.bodies)


def _record_from_identities(
    members: Sequence[BundleMember], bodies: Mapping[str, str]
) -> dict[str, object]:
    """A record pinning verified deployment identities under the fixed fixture."""
    run_inputs = _fixed_run_inputs(members, bodies)
    return build_effective_configuration(
        run_inputs, workflow=_fixed_workflow(), pin=TEST_PIN, prices=TEST_PRICES
    )


@dataclass
class _VersionedFakeInstaller(FakeInstallerAdapter):
    """A fake installer reporting a chosen producer version (SKV3-27)."""

    fake_version: str = "fake-installer-test"

    def producer_for(self, home: Path) -> ProducerIdentity:
        """The chosen producer version, independent of the store."""
        _ = home
        return ProducerIdentity(
            installer_version=self.fake_version,
            report_schema_version=FAKE_REPORT_SCHEMA_VERSION,
        )


# --- Serializer properties ----------------------------------------------------


def test_canonical_bytes_sort_keys_and_use_compact_separators() -> None:
    """Sorted keys and compact separators: no ``": "`` and no ``", "``."""
    raw = dumps_effective_canonical({"b": 1, "a": [1, 2], "c": {"y": 1, "x": 2}})
    assert raw == b'{"a":[1,2],"b":1,"c":{"x":2,"y":1}}'
    assert b'": "' not in raw
    assert b'", "' not in raw


def test_canonical_bytes_escape_non_ascii() -> None:
    """``ensure_ascii=True``: the bytes are pure ASCII with ``\\u`` escapes."""
    raw = dumps_effective_canonical({"café": "é"})
    assert raw == b'{"caf\\u00e9":"\\u00e9"}'
    assert raw.isascii()


def test_canonical_bytes_refuse_non_finite_floats() -> None:
    """``allow_nan=False``: NaN and infinities refuse instead of emitting."""
    with pytest.raises(ValueError):
        dumps_effective_canonical({"x": float("nan")})
    with pytest.raises(ValueError):
        dumps_effective_canonical({"x": float("inf")})


def test_canonical_bytes_have_no_bom_and_no_trailing_newline() -> None:
    """No BOM and no trailing newline."""
    raw = dumps_effective_canonical(_effective_record())
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert not raw.endswith(b"\n")
    assert not raw.endswith(b"\r")


def test_canonical_bytes_keep_model_numbers_as_numbers() -> None:
    """Model numbers stay numbers: prices serialize unquoted as JSON numbers."""
    raw = dumps_effective_canonical(_effective_record())
    assert b'"input":2.0' in raw
    assert b'"output":10.0' in raw
    assert b'"input":"2.0"' not in raw
    parsed = json.loads(raw.decode("utf-8"))
    assert isinstance(parsed, dict)
    pinned = parsed["pinned_model"]
    assert isinstance(pinned, dict)
    prices = pinned["prices"]
    assert isinstance(prices, dict)
    assert prices["input"] == 2.0 and isinstance(prices["input"], float)


def test_golden_vectors_freeze_numeric_unicode_and_framing_bytes() -> None:
    """Golden vectors: numeric, Unicode and framing bytes are byte-exact."""
    assert dumps_effective_canonical({"a": 2.5, "b": 1}) == b'{"a":2.5,"b":1}'
    assert dumps_effective_canonical({"é": 1}) == b'{"\\u00e9":1}'
    body = "hello"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert digest == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    record = _effective_record(activations={"alpha": body}, members=(_alpha_member(),))
    parsed = json.loads(dumps_effective_canonical(record).decode("utf-8"))
    assert isinstance(parsed, dict)
    activations = parsed["activations"]
    assert isinstance(activations, dict)
    assert activations["alpha"] == {
        "framing_version": DELIVERY_FRAMING_VERSION,
        "sha256": digest,
        "size_bytes": 5,
    }


def test_digest_is_sha256_of_the_canonical_core() -> None:
    """The digest is SHA-256 of the canonical bytes, and the ledger agrees."""
    record = _effective_record()
    raw = dumps_effective_canonical(record)
    assert _record_digest(record) == hashlib.sha256(raw).hexdigest()
    assert _record_digest(record) == sha256_effective_core(record)


def test_core_carries_exactly_the_version_2_sections() -> None:
    """The record carries exactly the v2 sections, and the file section its
    four settings groups: scope creep in either changes this test."""
    record = _effective_record()
    assert record["version"] == 2
    assert set(record.keys()) == {
        "version",
        "workflow",
        "file",
        "prompts",
        "skill_bundle",
        "activations",
        "pinned_model",
    }
    file_section = record["file"]
    assert isinstance(file_section, dict)
    assert set(file_section.keys()) == {"target", "model", "worker", "skills"}


def test_full_shape_validates_and_version_1_refuses() -> None:
    """``is_full`` accepts the v2 record and refuses the v1 shape."""
    assert is_full_effective_configuration(_effective_record()) is True
    legacy = dict(_effective_record())
    legacy["version"] = 1
    assert is_full_effective_configuration(legacy) is False
    assert is_full_effective_configuration({}) is False
    missing = dict(_effective_record())
    del missing["activations"]
    assert is_full_effective_configuration(missing) is False


def test_activations_must_cover_every_available_member() -> None:
    """The builder refuses a member without a delivery and vice versa."""
    with pytest.raises(ValueError, match="missing activations"):
        _effective_record(members=(_alpha_member(), _beta_member()), activations={"alpha": "x"})
    with pytest.raises(ValueError, match="without members"):
        _effective_record(
            members=(_alpha_member(),),
            activations={"alpha": "x", "beta": "y"},
        )


def test_builder_refuses_a_policy_without_a_cap_or_a_window() -> None:
    """Fail fast where the pin resolves no cap or window: such a record could
    never pass the full-shape check, so the builder refuses like the
    supervisor does before any Attempt opens."""
    members = (_alpha_member(), _beta_member())
    bodies = {"alpha": _alpha_delivery(), "beta": BETA_BODY}
    good = policy_for_pin(TEST_PIN)
    assert good.output_cap is not None and good.context_window is not None
    unknown = PinnedModel(
        provider="anthropic", model="no-such-model", endpoint="messages", effort="medium"
    )
    run_inputs = _fixed_run_inputs(members, bodies)
    with pytest.raises(ValueError, match="no output cap or context-window"):
        build_effective_configuration(
            run_inputs, workflow=_fixed_workflow(), pin=unknown, prices=TEST_PRICES
        )


def test_overlay_tuple_paths_survive_identity() -> None:
    """Overlay ``paths`` keep their order whether the seam hands a list or a
    tuple; anything else coerces to no paths rather than guessing."""
    records = _overlay_records(
        [{"id": "invocation-policy", "version": "1", "paths": ("SKILL.md",)}]
    )
    assert records == (
        MemberOverlay(id="invocation-policy", version="1", paths=("SKILL.md",)),
    )
    listed = _overlay_records([{"id": "i", "version": "v", "paths": ["a", "b"]}])
    assert listed[0].paths == ("a", "b")
    junk = _overlay_records([{"id": "i", "version": "v", "paths": "SKILL.md"}])
    assert junk[0].paths == ()


# --- SKV3-26: sensitivity ------------------------------------------------------


def test_changing_the_on_demand_member_moves_the_digest() -> None:
    """A byte change in the on-demand-only member moves the digest."""
    before = _record_digest(_effective_record())
    changed = _beta_member(body="# beta\n\nChanged body.\n")
    after = _record_digest(_effective_record(members=(_alpha_member(), changed)))
    assert before != after


def test_removing_the_on_demand_member_moves_the_digest() -> None:
    """Dropping the on-demand-only member moves the digest."""
    before = _record_digest(_effective_record())
    after = _record_digest(_effective_record(members=(_alpha_member(),), activations={"alpha": _alpha_delivery()}))
    assert before != after


def test_changing_the_selected_review_moves_the_digest() -> None:
    """A change to the member's selected review moves the digest."""
    before = _record_digest(_effective_record())
    review = _alpha_review(reason="changed operator reason")
    after = _record_digest(_effective_record(members=(_alpha_member(review=review), _beta_member())))
    assert before != after


def test_changing_companion_order_moves_the_digest() -> None:
    """Companion order is identity: swapping inject order moves the digest."""
    before = _record_digest(_effective_record())
    swapped = _alpha_review(
        companions=(("docs/extra.md", EXTRA_TEXT), ("docs/guide.md", GUIDE_TEXT))
    )
    deliveries = {"alpha": _alpha_delivery(swapped), "beta": BETA_BODY}
    after = _record_digest(
        _effective_record(members=(_alpha_member(review=swapped), _beta_member()), activations=deliveries)
    )
    assert before != after


def test_changing_a_notice_moves_the_digest() -> None:
    """An operator-authored notice is identity: editing it moves the digest."""
    before = _record_digest(_effective_record())
    review = _alpha_review(notice="Different operator guidance.")
    deliveries = {"alpha": _alpha_delivery(review), "beta": BETA_BODY}
    after = _record_digest(
        _effective_record(members=(_alpha_member(review=review), _beta_member()), activations=deliveries)
    )
    assert before != after


def test_changing_a_prompt_moves_the_digest() -> None:
    """A prompt byte change moves the digest."""
    before = _record_digest(_effective_record())
    after = _record_digest(_effective_record(prompt_text="Different prompt bytes.\n"))
    assert before != after


def test_changing_a_not_delivered_digest_moves_the_digest() -> None:
    """A ``not-delivered`` inventory digest is pinned: changing it moves."""
    before = _record_digest(_effective_record())
    review = _alpha_review(not_delivered_sha="d" * 64)
    after = _record_digest(_effective_record(members=(_alpha_member(review=review), _beta_member())))
    assert before != after


def test_changing_the_commit_with_identical_bytes_moves_the_digest() -> None:
    """The resolved commit is identity even where the bytes are identical."""
    before = _record_digest(_effective_record())
    after = _record_digest(_effective_record(members=(_alpha_member(commit=COMMIT_C), _beta_member())))
    assert before != after


# --- SKV3-27: insensitivity -----------------------------------------------------


def test_changing_workflow_semantics_moves_the_digest() -> None:
    """Selected Workflow semantics are identity: a step execution-limit change
    moves the digest."""
    members = (_alpha_member(), _beta_member())
    bodies = {"alpha": _alpha_delivery(), "beta": BETA_BODY}
    before = _record_digest(_effective_record(members=members, activations=bodies))
    workflow = _fixed_workflow()
    changed_step = dataclasses.replace(workflow.steps["work"], max_executions=2)
    changed_workflow = dataclasses.replace(workflow, steps={"work": changed_step})
    changed_inputs = _fixed_run_inputs(
        members, bodies, workflow=changed_workflow, config=_fixed_config(changed_workflow)
    )
    after = _record_digest(
        build_effective_configuration(
            changed_inputs, workflow=changed_workflow, pin=TEST_PIN, prices=TEST_PRICES
        )
    )
    assert before != after


def test_changing_file_level_settings_moves_the_digest() -> None:
    """File-level settings are identity: a worker poll-interval change moves
    the digest."""
    members = (_alpha_member(), _beta_member())
    bodies = {"alpha": _alpha_delivery(), "beta": BETA_BODY}
    before = _record_digest(_effective_record(members=members, activations=bodies))
    workflow = _fixed_workflow()
    config = _fixed_config(workflow)
    changed = dataclasses.replace(
        config,
        worker=dataclasses.replace(
            config.worker, poll_interval_seconds=config.worker.poll_interval_seconds + 5
        ),
    )
    changed_inputs = _fixed_run_inputs(members, bodies, workflow=workflow, config=changed)
    after = _record_digest(
        build_effective_configuration(
            changed_inputs, workflow=workflow, pin=TEST_PIN, prices=TEST_PRICES
        )
    )
    assert before != after


def _overlaid_member(paths: tuple[str, ...]) -> BundleMember:
    """A single member carrying one overlay with ``paths`` in the given order."""
    return BundleMember(
        name="alpha",
        source_identity=SOURCE_A,
        resolved_commit=COMMIT_A,
        declared_ref=None,
        relative_source_path="skills/alpha",
        store_relative_path="alpha",
        files=(InventoryFile(path="SKILL.md", sha256=_sha256_hex(ALPHA_BODY), executable=False),),
        overlays=(MemberOverlay(id="invocation-policy", version="1", paths=paths),),
        review=_alpha_review(companions=(), notice="n/a"),
    )


def test_changing_overlay_path_order_moves_the_digest() -> None:
    """Overlay path order is identity: reordering overlay paths moves the digest."""
    before = _record_digest(
        _effective_record(members=(_overlaid_member(("SKILL.md", "docs/guide.md")),), activations={"alpha": ALPHA_BODY})
    )
    after = _record_digest(
        _effective_record(members=(_overlaid_member(("docs/guide.md", "SKILL.md")),), activations={"alpha": ALPHA_BODY})
    )
    assert before != after


def test_root_relocation_leaves_the_digest_unchanged(tmp_path: Path) -> None:
    """Absolute roots are not identity: preparing and verifying the same
    fixture under two different checkout roots pins the same digest."""
    members = _declared_alpha_beta()
    config_a, policy_a, bundle_a, home_a = _prepare_installed(tmp_path / "checkout-a", members)
    config_b, policy_b, bundle_b, home_b = _prepare_installed(tmp_path / "checkout-b", members)
    members_a, bodies_a = _verified_identities(config_a, policy_a, bundle_a, home_a)
    members_b, bodies_b = _verified_identities(config_b, policy_b, bundle_b, home_b)
    assert _record_digest(_record_from_identities(members_a, bodies_a)) == _record_digest(
        _record_from_identities(members_b, bodies_b)
    )


def test_secret_values_and_excluded_markers_stay_out_of_the_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Values, image tags, producer versions, verification modes and the
    validation digest are not identity: none appears in the canonical bytes."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret-value")
    raw = dumps_effective_canonical(_effective_record())
    assert "sk-ant-test-secret-value".encode("utf-8") not in raw
    for marker in (b"image", b"verification", b"producer", b"validation"):
        assert marker not in raw


def test_producer_version_only_change_leaves_the_digest_unchanged(tmp_path: Path) -> None:
    """Two install-mode generations with different producer versions pin the
    same digest: the manifest's producer identity varies, the members do not.
    The producer block is the retag-correlated build identity (a retagged
    image ships a new producer version while the members stay byte-equal),
    and no image tag reaches the builder at all, so a retag cannot move the
    digest either."""
    members = _declared_alpha_beta()
    first = _VersionedFakeInstaller(fake_version="fake-installer-1.0")
    config_a, policy_a, bundle_a, home_a = _prepare_install(tmp_path / "first", members, first)
    second = _VersionedFakeInstaller(fake_version="fake-installer-9.9")
    config_b, policy_b, bundle_b, home_b = _prepare_install(tmp_path / "second", members, second)
    members_a, bodies_a = _verified_identities(config_a, policy_a, bundle_a, home_a)
    members_b, bodies_b = _verified_identities(config_b, policy_b, bundle_b, home_b)
    record_a = _record_from_identities(members_a, bodies_a)
    record_b = _record_from_identities(members_b, bodies_b)
    assert _record_digest(record_a) == _record_digest(record_b)
    manifest_a = json.loads((bundle_a / "manifest.json").read_bytes())
    manifest_b = json.loads((bundle_b / "manifest.json").read_bytes())
    assert isinstance(manifest_a, dict) and isinstance(manifest_b, dict)
    producer_a = manifest_a["producer"]
    producer_b = manifest_b["producer"]
    assert isinstance(producer_a, dict) and isinstance(producer_b, dict)
    assert producer_a["installerVersion"] == "fake-installer-1.0"
    assert producer_b["installerVersion"] == "fake-installer-9.9"
    raw = dumps_effective_canonical(record_a)
    assert b"fake-installer-1.0" not in raw
    assert b"fake-installer-9.9" not in raw
    assert b'"producer"' not in raw


def test_verification_mode_change_leaves_the_digest_unchanged(tmp_path: Path) -> None:
    """How the store proved the member (``source`` vs ``recorded-installation``)
    is not identity: installing and adopting the same fixture pin the same
    digest while the manifest generations record different modes."""
    members = _declared_alpha_beta()
    config_a, policy_a, bundle_a, home_a = _prepare_install(
        tmp_path / "by-installer", members, FakeInstallerAdapter()
    )
    config_b, policy_b, bundle_b, home_b = _prepare_installed(tmp_path / "adopted", members)
    members_a, bodies_a = _verified_identities(config_a, policy_a, bundle_a, home_a)
    members_b, bodies_b = _verified_identities(config_b, policy_b, bundle_b, home_b)
    record_a = _record_from_identities(members_a, bodies_a)
    record_b = _record_from_identities(members_b, bodies_b)
    assert _record_digest(record_a) == _record_digest(record_b)
    manifest_a = json.loads((bundle_a / "manifest.json").read_bytes())
    manifest_b = json.loads((bundle_b / "manifest.json").read_bytes())
    assert isinstance(manifest_a, dict) and isinstance(manifest_b, dict)
    artifacts_a = manifest_a["artifacts"]
    artifacts_b = manifest_b["artifacts"]
    assert isinstance(artifacts_a, list) and isinstance(artifacts_b, list)
    assert {str(item["verification"]) for item in artifacts_a} == {"source"}
    assert {str(item["verification"]) for item in artifacts_b} == {"recorded-installation"}
    raw = dumps_effective_canonical(record_a)
    assert b'"verification"' not in raw
    assert b"recorded-installation" not in raw


def test_unused_catalog_edits_leave_the_digest_unchanged(tmp_path: Path) -> None:
    """Reviews for undeclared members are not identity: approving an unused
    ``gamma`` review through a real prepare and verify pins the same digest."""
    members = _declared_alpha_beta()
    config_a, policy_a, bundle_a, home_a = _prepare_installed(tmp_path / "baseline", members)
    edited_doc = build_policy_document(members)
    reviews = edited_doc["reviews"]
    assert isinstance(reviews, list)
    reviews.append(
        {
            "files": [],
            "name": "gamma",
            "reason": "unused catalog edit",
            "references": [],
            "requires": [],
            "skillSha256": "e" * 64,
            "sourceIdentity": SOURCE_A,
        }
    )
    config_b, policy_b, bundle_b, home_b = _prepare_installed(
        tmp_path / "edited", members, policy_doc=edited_doc
    )
    members_a, bodies_a = _verified_identities(config_a, policy_a, bundle_a, home_a)
    members_b, bodies_b = _verified_identities(config_b, policy_b, bundle_b, home_b)
    record_a = _record_from_identities(members_a, bodies_a)
    record_b = _record_from_identities(members_b, bodies_b)
    assert _record_digest(record_a) == _record_digest(record_b)
    assert b"gamma" not in dumps_effective_canonical(record_b)


def test_unrelated_artifacts_leave_the_digest_unchanged(tmp_path: Path) -> None:
    """Files outside the member inventories are not identity: a stray file
    under the home root verifies cleanly and pins the same digest."""
    members = _declared_alpha_beta()
    config, policy, bundle_dir, home = _prepare_installed(tmp_path / "case", members)
    members_before, bodies_before = _verified_identities(config, policy, bundle_dir, home)
    stray = home / "field-notes.txt"
    stray.write_text("unrelated operator bytes", encoding="utf-8")
    members_after, bodies_after = _verified_identities(config, policy, bundle_dir, home)
    assert _record_digest(_record_from_identities(members_after, bodies_after)) == _record_digest(
        _record_from_identities(members_before, bodies_before)
    )
    assert b"field-notes" not in dumps_effective_canonical(
        _record_from_identities(members_after, bodies_after)
    )


def test_validation_digest_change_leaves_the_digest_unchanged(tmp_path: Path) -> None:
    """The generated ``manifest.json``/``validation.json`` digests bind the
    deployment, not the Attempt's identity: their real SHAs never enter the
    canonical bytes, and re-verifying the same deployment pins the same digest."""
    members = _declared_alpha_beta()
    config, policy, bundle_dir, home = _prepare_installed(tmp_path / "case", members)
    verified_members, bodies = _verified_identities(config, policy, bundle_dir, home)
    record = _record_from_identities(verified_members, bodies)
    raw = dumps_effective_canonical(record)
    for generated in ("manifest.json", "requirements.json", "validation.json"):
        generated_sha = hashlib.sha256((bundle_dir / generated).read_bytes()).hexdigest()
        assert generated_sha.encode("utf-8") not in raw
    verified_again, bodies_again = _verified_identities(config, policy, bundle_dir, home)
    assert _record_digest(_record_from_identities(verified_again, bodies_again)) == _record_digest(
        record
    )


def test_overlay_and_member_records() -> None:
    """Overlays and inventory entries round-trip through the core."""
    member = BundleMember(
        name="alpha",
        source_identity=SOURCE_A,
        resolved_commit=COMMIT_A,
        declared_ref="main",
        relative_source_path="skills/alpha",
        store_relative_path="alpha",
        files=(InventoryFile(path="SKILL.md", sha256=_sha256_hex(ALPHA_BODY), executable=False),),
        overlays=(
            MemberOverlay(id="invocation-policy", version="1", paths=("SKILL.md",)),
        ),
        review=_alpha_review(companions=(), notice="n/a"),
    )
    record = _effective_record(members=(member,), activations={"alpha": ALPHA_BODY})
    assert is_full_effective_configuration(record) is True
    parsed = json.loads(dumps_effective_canonical(record).decode("utf-8"))
    assert isinstance(parsed, dict)
    bundle = parsed["skill_bundle"]
    assert isinstance(bundle, dict)
    bundle_members = bundle["members"]
    assert isinstance(bundle_members, dict)
    assert bundle_members["alpha"]["declaredRef"] == "main"
    assert bundle_members["alpha"]["overlays"] == [
        {"id": "invocation-policy", "version": "1", "paths": ["SKILL.md"]}
    ]
