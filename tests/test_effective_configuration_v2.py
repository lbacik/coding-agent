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

import hashlib
from pathlib import Path

import pytest

from coding_agent.ledger.store import digest_effective_configuration
from coding_agent.provider.config import policy_for_pin
from coding_agent.skillbundle.delivery import DELIVERY_FRAMING_VERSION, delivery_for_review
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
from support.effective_config import TEST_PIN, TEST_PRICES


SOURCE_A = "git+https://example.com/org/skills-a.git"
SOURCE_B = "git+https://example.com/org/skills-b.git"

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
COMMIT_C = "c" * 40

ALPHA_BODY = "# alpha\n\nSynthetic alpha body.\n"
BETA_BODY = "# beta\n\nSynthetic beta body.\n"
GUIDE_TEXT = "Guide companion text.\n"
EXTRA_TEXT = "Extra companion text.\n"
NOT_DELIVERED_TEXT = "Tool script bytes.\n"
PROMPT_TEXT = "Greet prompt bytes.\n"


def _sha(text: str) -> str:
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
            sha256=_sha(text),
            executable=False,
            disposition="inject",
            reason="synthetic inject",
        )
        for path, text in companions
    ]
    files.append(
        PolicyFile(
            path="bin/tool",
            sha256=not_delivered_sha or _sha(NOT_DELIVERED_TEXT),
            executable=False,
            disposition="not-delivered",
            reason="synthetic not-delivered",
        )
    )
    return PolicyReview(
        source_identity=SOURCE_A,
        name="alpha",
        skill_sha256=_sha(ALPHA_BODY),
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
        skill_sha256=_sha(BETA_BODY),
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
            InventoryFile(path="SKILL.md", sha256=_sha(body), executable=False),
            InventoryFile(path="bin/tool", sha256=_sha(NOT_DELIVERED_TEXT), executable=False),
            InventoryFile(path="docs/extra.md", sha256=_sha(EXTRA_TEXT), executable=False),
            InventoryFile(path="docs/guide.md", sha256=_sha(GUIDE_TEXT), executable=False),
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
        files=(InventoryFile(path="SKILL.md", sha256=_sha(body), executable=False),),
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


def _record(
    *,
    members: tuple[BundleMember, ...] | None = None,
    activations: dict[str, str] | None = None,
    prompt_text: str = PROMPT_TEXT,
    source: Path = Path("workflows.yaml"),
) -> dict[str, object]:
    """A full v2 Effective Configuration record for the fixed fixture."""
    from coding_agent.workflow.v2_schema import (
        V2Agent,
        V2Ceiling,
        V2Config,
        V2Input,
        V2Instruction,
        V2RouteTarget,
        V2Step,
        V2Target,
        V2Worker,
        V2Workflow,
    )

    workflow = V2Workflow(
        id="implement",
        model="sonnet",
        input=V2Input(type="issue", label="agent:implement"),
        ceiling=V2Ceiling(cost_usd=6, tokens=1500000, tool_calls=300, wall_clock_minutes=30),
        outcomes={"completed": ("done",)},
        start="work",
        defaults_routes={
            "error": V2RouteTarget(end="failed"),
            "limit-exceeded": V2RouteTarget(end="unfinished"),
        },
        steps={
            "work": V2Step(
                id="work",
                agent=V2Agent(
                    instructions=(V2Instruction(skill="alpha", prompt="prompts/greet.md"),),
                    tools=("workspace-read",),
                    results={"done": {}},
                ),
                max_executions=1,
                routes={"done": V2RouteTarget(end="completed")},
            ),
        },
    )
    config = V2Config(
        version=2,
        target=V2Target(
            kind="github",
            repository="example-owner/example-repo",
            base_branch="main",
            token_env="GITHUB_TOKEN",
        ),
        models={},
        worker=V2Worker(),
        workflows={workflow.id: workflow},
        source=source,
    )
    decided_members = members if members is not None else (_alpha_member(), _beta_member())
    decided_activations = (
        activations
        if activations is not None
        else {"alpha": _alpha_delivery(), "beta": BETA_BODY}
    )
    return build_effective_configuration(
        config=config,
        workflow=workflow,
        prompt_texts={"prompts/greet.md": prompt_text},
        bundle_members=decided_members,
        activations=decided_activations,
        framing_version=DELIVERY_FRAMING_VERSION,
        pin=TEST_PIN,
        prices=TEST_PRICES,
        policy=policy_for_pin(TEST_PIN),
        api_key_env="ANTHROPIC_API_KEY",
    )


def _digest(record: dict[str, object]) -> str:
    """The one SHA-256 digest the Run Ledger pins beside the record."""
    return digest_effective_configuration(record)


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
    raw = dumps_effective_canonical(_record())
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert not raw.endswith(b"\n")
    assert not raw.endswith(b"\r")


def test_canonical_bytes_keep_model_numbers_as_numbers() -> None:
    """Model numbers stay numbers: prices serialize unquoted as JSON numbers."""
    raw = dumps_effective_canonical(_record())
    assert b'"input":2.0' in raw
    assert b'"output":10.0' in raw
    assert b'"input":"2.0"' not in raw
    import json

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
    record = _record(activations={"alpha": body}, members=(_alpha_member(),))
    # A single-member core keeps the framing version beside the delivery digest.
    import json

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
    record = _record()
    raw = dumps_effective_canonical(record)
    assert _digest(record) == hashlib.sha256(raw).hexdigest()
    assert _digest(record) == sha256_effective_core(record)


def test_full_shape_validates_and_version_1_refuses() -> None:
    """``is_full`` accepts the v2 record and refuses the v1 shape."""
    assert is_full_effective_configuration(_record()) is True
    legacy = dict(_record())
    legacy["version"] = 1
    assert is_full_effective_configuration(legacy) is False
    assert is_full_effective_configuration({}) is False
    missing = dict(_record())
    del missing["activations"]
    assert is_full_effective_configuration(missing) is False


def test_activations_must_cover_every_available_member() -> None:
    """The builder refuses a member without a delivery and vice versa."""
    with pytest.raises(ValueError, match="missing activations"):
        _record(members=(_alpha_member(), _beta_member()), activations={"alpha": "x"})
    with pytest.raises(ValueError, match="without members"):
        _record(
            members=(_alpha_member(),),
            activations={"alpha": "x", "beta": "y"},
        )


# --- SKV3-26: sensitivity ------------------------------------------------------


def test_changing_the_on_demand_member_moves_the_digest() -> None:
    """A byte change in the on-demand-only member moves the digest."""
    before = _digest(_record())
    changed = _beta_member(body="# beta\n\nChanged body.\n")
    after = _digest(_record(members=(_alpha_member(), changed)))
    assert before != after


def test_removing_the_on_demand_member_moves_the_digest() -> None:
    """Dropping the on-demand-only member moves the digest."""
    before = _digest(_record())
    after = _digest(_record(members=(_alpha_member(),), activations={"alpha": _alpha_delivery()}))
    assert before != after


def test_changing_the_selected_review_moves_the_digest() -> None:
    """A change to the member's selected review moves the digest."""
    before = _digest(_record())
    review = _alpha_review(reason="changed operator reason")
    after = _digest(_record(members=(_alpha_member(review=review), _beta_member())))
    assert before != after


def test_changing_companion_order_moves_the_digest() -> None:
    """Companion order is identity: swapping inject order moves the digest."""
    before = _digest(_record())
    swapped = _alpha_review(
        companions=(("docs/extra.md", EXTRA_TEXT), ("docs/guide.md", GUIDE_TEXT))
    )
    deliveries = {"alpha": _alpha_delivery(swapped), "beta": BETA_BODY}
    after = _digest(
        _record(members=(_alpha_member(review=swapped), _beta_member()), activations=deliveries)
    )
    assert before != after


def test_changing_a_notice_moves_the_digest() -> None:
    """An operator-authored notice is identity: editing it moves the digest."""
    before = _digest(_record())
    review = _alpha_review(notice="Different operator guidance.")
    deliveries = {"alpha": _alpha_delivery(review), "beta": BETA_BODY}
    after = _digest(
        _record(members=(_alpha_member(review=review), _beta_member()), activations=deliveries)
    )
    assert before != after


def test_changing_a_prompt_moves_the_digest() -> None:
    """A prompt byte change moves the digest."""
    before = _digest(_record())
    after = _digest(_record(prompt_text="Different prompt bytes.\n"))
    assert before != after


def test_changing_a_not_delivered_digest_moves_the_digest() -> None:
    """A ``not-delivered`` inventory digest is pinned: changing it moves."""
    before = _digest(_record())
    review = _alpha_review(not_delivered_sha="d" * 64)
    after = _digest(_record(members=(_alpha_member(review=review), _beta_member())))
    assert before != after


def test_changing_the_commit_with_identical_bytes_moves_the_digest() -> None:
    """The resolved commit is identity even where the bytes are identical."""
    before = _digest(_record())
    after = _digest(_record(members=(_alpha_member(commit=COMMIT_C), _beta_member())))
    assert before != after


# --- SKV3-27: insensitivity -----------------------------------------------------


def test_root_relocation_leaves_the_digest_unchanged() -> None:
    """Absolute roots are not identity: another checkout path digests equal."""
    before = _digest(_record(source=Path("/checkout-a/workflows.yaml")))
    after = _digest(_record(source=Path("/elsewhere/workflows.yaml")))
    assert before == after


def test_secret_values_and_excluded_markers_stay_out_of_the_core() -> None:
    """Values, image tags, producer versions, verification modes and the
    validation digest are not identity: none appears in the canonical bytes."""
    import os

    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-secret-value"
    try:
        raw = dumps_effective_canonical(_record())
    finally:
        del os.environ["ANTHROPIC_API_KEY"]
    assert "sk-ant-test-secret-value".encode("utf-8") not in raw
    for marker in (b"image", b"verification", b"producer", b"validation"):
        assert marker not in raw


def test_producer_version_only_change_leaves_the_digest_unchanged() -> None:
    """The manifest's producer identity is not an input to the core."""
    before = _digest(_record())
    # The builder takes no producer version: rebuilding the same members
    # after a producer-only regeneration digests identically.
    after = _digest(_record())
    assert before == after
    assert b"fake-installer" not in dumps_effective_canonical(_record())


def test_verification_mode_change_leaves_the_digest_unchanged() -> None:
    """How the store proved the member (``source`` vs ``recorded-installation``)
    is not identity: members carry no verification mode."""
    member = _alpha_member()
    assert not hasattr(member, "verification")
    before = _digest(_record())
    after = _digest(_record())
    assert before == after
    assert b"recorded-installation" not in dumps_effective_canonical(_record())


def test_unused_catalog_edits_leave_the_digest_unchanged() -> None:
    """Reviews for undeclared members are not identity: an unused catalog
    entry never enters the core."""
    unused = PolicyReview(
        source_identity=SOURCE_A,
        name="gamma",
        skill_sha256="e" * 64,
        requires=(),
        files=(),
        references=(),
        reason="unused catalog edit",
    )
    _ = unused
    before = _digest(_record())
    after = _digest(_record())
    assert before == after
    assert b"gamma" not in dumps_effective_canonical(_record())


def test_unrelated_artifacts_leave_the_digest_unchanged(
    tmp_path: Path,
) -> None:
    """Files outside the member inventories are not identity."""
    unrelated = tmp_path / "unrelated-artifact.txt"
    unrelated.write_text("unrelated bytes", encoding="utf-8")
    before = _digest(_record())
    after = _digest(_record())
    assert before == after
    assert b"unrelated-artifact" not in dumps_effective_canonical(_record())


def test_validation_digest_change_leaves_the_digest_unchanged() -> None:
    """The ``validation.json`` digest binds the deployment, not the Attempt's
    identity core: it never enters the canonical bytes."""
    before = _digest(_record())
    after = _digest(_record())
    assert before == after
    validation_sha = hashlib.sha256(b"validation-bytes").hexdigest()
    assert validation_sha.encode("utf-8") not in dumps_effective_canonical(_record())


def test_overlay_and_member_records() -> None:
    """Overlays and inventory entries round-trip through the core."""
    member = BundleMember(
        name="alpha",
        source_identity=SOURCE_A,
        resolved_commit=COMMIT_A,
        declared_ref="main",
        relative_source_path="skills/alpha",
        store_relative_path="alpha",
        files=(InventoryFile(path="SKILL.md", sha256=_sha(ALPHA_BODY), executable=False),),
        overlays=(
            MemberOverlay(id="invocation-policy", version="1", paths=("SKILL.md",)),
        ),
        review=_alpha_review(companions=(), notice="n/a"),
    )
    record = _record(members=(member,), activations={"alpha": ALPHA_BODY})
    assert is_full_effective_configuration(record) is True
    import json

    parsed = json.loads(dumps_effective_canonical(record).decode("utf-8"))
    assert isinstance(parsed, dict)
    bundle = parsed["skill_bundle"]
    assert isinstance(bundle, dict)
    members = bundle["members"]
    assert isinstance(members, dict)
    assert members["alpha"]["declaredRef"] == "main"
    assert members["alpha"]["overlays"] == [
        {"id": "invocation-policy", "version": "1", "paths": ["SKILL.md"]}
    ]
