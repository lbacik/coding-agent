"""Draft helper for the Skill Bundle Policy (``agent draft-skill-policy``).

Contract: the helper drafts a proposed ``skill-policy.json`` for operator
review (issues #374 and #385, user story 24). It carries forward reviews
whose identity still matches exactly and drafts placeholder-only (JSON
``null``) reviews for new or changed members. An unedited draft always
fails policy validation, so it can never be used as an approval. The helper
is offline: it reads the Workflow Configuration, the installed store and
the optional current policy, and writes nothing except ``--out``.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from coding_agent.cli import main as cli_main
from coding_agent.skillbundle.canonical import dumps_canonical, loads_strict
from coding_agent.skillbundle.deployment import (
    REVIEW_AID_REF_RE,
    prepare_skill_bundle,
)
from coding_agent.workflow.configuration import (
    LoadedWorkflowConfiguration,
    load_workflow_configuration,
)


def _loaded_config(path: Path) -> LoadedWorkflowConfiguration:
    """The single-loader value for the config file at ``path``."""
    loaded, faults = load_workflow_configuration(path)
    assert loaded is not None, [str(fault) for fault in faults]
    return loaded

from coding_agent.skillbundle.draft import build_draft, draft_document_bytes, draft_to
from coding_agent.skillbundle.installer import (
    FakeInstallerAdapter,
    InstallerFault,
    query_installer_version,
    write_fake_store,
)
from coding_agent.skillbundle.policy import (
    parse_policy_bytes,
    validate_policy_object,
)
from support.skill_bundle import (
    COMMIT_A,
    COMMIT_B,
    SOURCE_A,
    SOURCE_B,
    DeclaredMember,
    build_policy_document,
    default_skill_md_text,
    fake_store_spec_for,
    minimal_config_text,
    skills_entries_for,
    write_policy_file,
)


def _real_available() -> bool:
    """Whether the released installer binary passes its minimum gate."""
    try:
        query_installer_version()
    except InstallerFault:
        return False
    return True


requires_real_installer = pytest.mark.skipif(
    not _real_available(),
    reason="needs agent-installer on PATH",
)


def _write_config(tmp_path: Path, members: list[DeclaredMember]) -> Path:
    config = tmp_path / "workflow.yml"
    config.write_text(minimal_config_text(skills_entries_for(members)), encoding="utf-8")
    return config


def _setup(
    tmp_path: Path,
    policy_members: list[DeclaredMember],
    store_members: list[DeclaredMember] | None = None,
) -> tuple[Path, Path, Path, Path]:
    """Write config, policy (from ``policy_members``) and fake store.

    Returns ``(config, policy, bundle_dir, home)``. The store holds
    ``store_members`` when given, else ``policy_members``.
    """
    config = _write_config(tmp_path, store_members or policy_members)
    policy = tmp_path / "skill-policy.json"
    write_policy_file(policy, build_policy_document(policy_members))
    bundle_dir = tmp_path / "bundle"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    write_fake_store(home, fake_store_spec_for(store_members or policy_members))
    return config, policy, bundle_dir, home


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    """Map every file under ``root`` (relative POSIX path) to its bytes."""
    snapshot: dict[str, bytes] = {}
    for child in sorted(root.rglob("*")):
        if child.is_file() and not child.is_symlink():
            snapshot[str(child.relative_to(root))] = child.read_bytes()
    return snapshot


def test_unchanged_pin_draft_is_identical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With a matching policy the draft is identical after normalisation."""
    members = [
        DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A),
        DeclaredMember(
            name="beta",
            source=SOURCE_B,
            commit=COMMIT_B,
            extra_files={"docs/notes.md": (b"# notes\n", False)},
        ),
    ]
    config, policy, _, home = _setup(tmp_path, members)
    out = tmp_path / "draft.json"
    assert cli_main(
        [
            "draft-skill-policy",
            "--config",
            str(config),
            "--home",
            str(home),
            "--policy",
            str(policy),
            "--out",
            str(out),
        ]
    ) == 0
    report = capsys.readouterr().out
    assert "0 unchanged" not in report
    assert "2 unchanged, 0 changed, 0 new, 0 stale" in report
    assert "0 placeholders need operator review" in report
    assert out.read_bytes() == policy.read_bytes()
    outcome = build_draft(config=config, home=home, policy=policy)
    assert outcome.ok
    assert outcome.placeholders == 0
    assert outcome.document is not None
    assert draft_document_bytes(outcome.document) == policy.read_bytes()


def test_changed_member_only_redrafts_that_member(tmp_path: Path) -> None:
    """One changed SKILL.md redrafts only that member; the old review is kept."""
    old_members = [
        DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A),
        DeclaredMember(name="beta", source=SOURCE_A, commit=COMMIT_A),
    ]
    config, policy, _, home = _setup(tmp_path, old_members)
    changed_beta = DeclaredMember(
        name="beta",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=default_skill_md_text("beta", "Changed body."),
    )
    write_fake_store(home, fake_store_spec_for([old_members[0], changed_beta]))
    config.write_text(
        minimal_config_text(skills_entries_for([old_members[0], changed_beta])),
        encoding="utf-8",
    )
    outcome = build_draft(config=config, home=home, policy=policy)
    assert outcome.ok, outcome.faults
    assert outcome.document is not None
    reviews = outcome.document["reviews"]
    assert isinstance(reviews, list)
    assert len(reviews) == 3
    old_doc = json.loads(policy.read_bytes().decode("utf-8"))
    assert reviews[0] == old_doc["reviews"][0]
    assert reviews[1] == old_doc["reviews"][1]
    assert reviews[0] == reviews[0] and reviews[2] != old_doc["reviews"][1]
    assert "member 'alpha': unchanged" in outcome.report
    assert "member 'beta': changed" in outcome.report
    assert "stale review:" in outcome.report and "'beta'" in outcome.report
    assert "member changed, old review retained" in outcome.report
    assert outcome.placeholders > 0


def test_draft_placeholders_are_rejected(tmp_path: Path) -> None:
    """A draft with nulls fails policy validation and preparation refuses it."""
    members = [
        DeclaredMember(
            name="plain",
            source=SOURCE_A,
            commit=COMMIT_A,
            skill_md=default_skill_md_text("plain", "No references here."),
        ),
    ]
    config, _, bundle_dir, home = _setup(tmp_path, members)
    outcome = build_draft(config=config, home=home, policy=None)
    assert outcome.ok, outcome.faults
    assert outcome.document is not None
    assert outcome.document["reviews"] != []
    draft_bytes = draft_document_bytes(outcome.document)
    parsed, faults = parse_policy_bytes(draft_bytes)
    assert parsed is None and faults
    value, problem = loads_strict(draft_bytes)
    assert problem is None
    assert value is not None
    reparsed, refaults = validate_policy_object(value)
    assert reparsed is None and refaults
    draft_policy = tmp_path / "draft.json"
    draft_policy.write_bytes(draft_bytes)
    prepared = prepare_skill_bundle(
        config=_loaded_config(config),
        policy=draft_policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert not prepared.ok
    assert any(fault.phase == "policy" for fault in prepared.faults)


def test_file_changes_appear_in_report_and_force_redraft(tmp_path: Path) -> None:
    """Added, removed and executable-bit changes each force a redraft."""
    policy_members = [
        DeclaredMember(name="added", source=SOURCE_A, commit=COMMIT_A),
        DeclaredMember(
            name="removed",
            source=SOURCE_A,
            commit=COMMIT_A,
            extra_files={"docs/notes.md": (b"# notes\n", False)},
        ),
        DeclaredMember(
            name="execbit",
            source=SOURCE_A,
            commit=COMMIT_A,
            extra_files={"tools/run.sh": (b"echo hi\n", False)},
        ),
    ]
    store_members = [
        DeclaredMember(
            name="added",
            source=SOURCE_A,
            commit=COMMIT_A,
            extra_files={"docs/notes.md": (b"# notes\n", False)},
        ),
        DeclaredMember(name="removed", source=SOURCE_A, commit=COMMIT_A),
        DeclaredMember(
            name="execbit",
            source=SOURCE_A,
            commit=COMMIT_A,
            extra_files={"tools/run.sh": (b"echo hi\n", True)},
        ),
    ]
    config, policy, _, home = _setup(tmp_path, policy_members, store_members)
    outcome = build_draft(config=config, home=home, policy=policy)
    assert outcome.ok, outcome.faults
    assert "member 'added': changed" in outcome.report
    assert "member 'removed': changed" in outcome.report
    assert "member 'execbit': changed" in outcome.report
    assert "file 'docs/notes.md': added" in outcome.report
    assert "file 'docs/notes.md': removed" in outcome.report
    assert "executable bit changed" in outcome.report
    assert outcome.document is not None
    reviews = outcome.document["reviews"]
    assert isinstance(reviews, list)
    drafted = reviews[3:]
    assert len(drafted) == 3
    for entry in drafted:
        assert isinstance(entry, dict)
        files = entry["files"]
        assert isinstance(files, list)
        for record in files:
            assert isinstance(record, dict)
            assert record["disposition"] is None
            assert record["reason"] is None


def test_unchanged_companion_gets_null_with_suggestion(tmp_path: Path) -> None:
    """An unchanged companion in a changed member is nulled, old shown once."""
    old = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        extra_files={"docs/notes.md": (b"# notes\n", False)},
    )
    config, policy, _, home = _setup(tmp_path, [old])
    changed = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=default_skill_md_text("alpha", "Changed body."),
        extra_files={"docs/notes.md": (b"# notes\n", False)},
    )
    write_fake_store(home, fake_store_spec_for([changed]))
    config.write_text(minimal_config_text(skills_entries_for([changed])), encoding="utf-8")
    outcome = build_draft(config=config, home=home, policy=policy)
    assert outcome.ok, outcome.faults
    assert outcome.document is not None
    reviews = outcome.document["reviews"]
    assert isinstance(reviews, list)
    assert len(reviews) == 2
    redrafted = reviews[1]
    assert isinstance(redrafted, dict)
    files = redrafted["files"]
    assert isinstance(files, list)
    assert len(files) == 1
    assert files[0]["disposition"] is None
    assert "suggestion (unconfirmed): file 'docs/notes.md' was previously ruled" in outcome.report
    assert "'inject'" in outcome.report


def test_suggestions_stay_in_report_only(tmp_path: Path) -> None:
    """Earlier rulings and requires hints never enter the draft JSON."""
    old = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=default_skill_md_text("alpha", "Use /beta for that."),
        requires=("beta",),
        references=(
            {
                "classification": "not-a-skill-reference",
                "origin": "SKILL.md",
                "reason": "old ruling",
                "text": "Use /beta for that.",
            },
        ),
    )
    config, policy, _, home = _setup(tmp_path, [old])
    changed = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=default_skill_md_text("alpha", "Use /beta for that.\n\nExtra line."),
    )
    beta = DeclaredMember(name="beta", source=SOURCE_A, commit=COMMIT_A)
    write_fake_store(home, fake_store_spec_for([changed, beta]))
    config.write_text(
        minimal_config_text(skills_entries_for([changed, beta])), encoding="utf-8"
    )
    outcome = build_draft(config=config, home=home, policy=policy)
    assert outcome.ok, outcome.faults
    assert outcome.document is not None
    draft_bytes = draft_document_bytes(outcome.document)
    value, problem = loads_strict(draft_bytes)
    assert problem is None and isinstance(value, dict)
    reviews = value["reviews"]
    assert isinstance(reviews, list)
    assert len(reviews) == 3
    # The stale old review is kept verbatim; the two drafted reviews carry
    # only null placeholders and empty requires.
    drafted = [entry for entry in reviews[1:] if isinstance(entry, dict)]
    assert len(drafted) == 2
    for entry in drafted:
        assert entry["requires"] == []
        assert entry["reason"] is None
        references = entry["references"]
        assert isinstance(references, list)
        for ruling in references:
            assert isinstance(ruling, dict)
            assert ruling["classification"] is None
            assert ruling["reason"] is None
            assert "target" not in ruling
            assert "notice" not in ruling
        assert set(entry.keys()) == {
            "files",
            "name",
            "reason",
            "references",
            "requires",
            "skillSha256",
            "sourceIdentity",
        }
    assert "previously ruled 'not-a-skill-reference'" in outcome.report
    assert "(consider requires)" in outcome.report


def test_out_same_as_policy_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--out resolving to --policy is refused and the policy is untouched."""
    members = [DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)]
    config, policy, _, home = _setup(tmp_path, members)
    monkeypatch.chdir(tmp_path)
    before = policy.read_bytes()
    assert (
        cli_main(
            [
                "draft-skill-policy",
                "--config",
                str(config),
                "--home",
                str(home),
                "--policy",
                str(policy),
                "--out",
                str(policy),
            ]
        )
        == 1
    )
    assert policy.read_bytes() == before
    assert "refuses to overwrite" in capsys.readouterr().err
    relative_out = os.path.relpath(policy, tmp_path)
    assert (
        cli_main(
            [
                "draft-skill-policy",
                "--config",
                str(config),
                "--home",
                str(home),
                "--policy",
                str(policy),
                "--out",
                relative_out,
            ]
        )
        == 1
    )
    assert policy.read_bytes() == before
    link = tmp_path / "policy-link.json"
    link.symlink_to(policy)
    assert (
        cli_main(
            [
                "draft-skill-policy",
                "--config",
                str(config),
                "--home",
                str(home),
                "--policy",
                str(policy),
                "--out",
                str(link),
            ]
        )
        == 1
    )
    assert policy.read_bytes() == before


def test_writes_nothing_except_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The helper touches no input and creates only --out."""
    members = [DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)]
    config, policy, bundle_dir, home = _setup(tmp_path, members)
    bundle_dir.mkdir(exist_ok=True)
    (bundle_dir / "manifest.json").write_bytes(b"{}")
    before = _snapshot_tree(tmp_path)
    out = tmp_path / "draft.json"
    assert (
        cli_main(
            [
                "draft-skill-policy",
                "--config",
                str(config),
                "--home",
                str(home),
                "--policy",
                str(policy),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    capsys.readouterr()
    after = _snapshot_tree(tmp_path)
    assert set(after) - set(before) == {"draft.json"}
    for name, raw in before.items():
        assert after[name] == raw
    assert "manifest.json" not in out.name
    assert (bundle_dir / "manifest.json").read_bytes() == b"{}"


def test_invalid_inputs_exit_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unreadable config, missing member and bad policy each refuse."""
    members = [DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)]
    config, policy, _, home = _setup(tmp_path, members)
    missing_config = tmp_path / "nope.yml"
    assert (
        cli_main(
            [
                "draft-skill-policy",
                "--config",
                str(missing_config),
                "--home",
                str(home),
                "--out",
                str(tmp_path / "d1.json"),
            ]
        )
        == 1
    )
    capsys.readouterr()
    ghost = DeclaredMember(name="ghost", source=SOURCE_A, commit=COMMIT_A)
    ghost_config = _write_config(tmp_path, [ghost])
    assert (
        cli_main(
            [
                "draft-skill-policy",
                "--config",
                str(ghost_config),
                "--home",
                str(home),
                "--out",
                str(tmp_path / "d2.json"),
            ]
        )
        == 1
    )
    capsys.readouterr()
    bad_policy = tmp_path / "bad.json"
    bad_policy.write_text("{nope", encoding="utf-8")
    assert (
        cli_main(
            [
                "draft-skill-policy",
                "--config",
                str(config),
                "--home",
                str(home),
                "--policy",
                str(bad_policy),
                "--out",
                str(tmp_path / "d3.json"),
            ]
        )
        == 1
    )
    capsys.readouterr()
    assert not (tmp_path / "d3.json").exists()


def test_draft_with_placeholders_still_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Placeholders are operator work, not a helper failure: exit 0."""
    members = [DeclaredMember(name="alpha", source=SOURCE_A, commit=COMMIT_A)]
    config, _, _, home = _setup(tmp_path, members)
    out = tmp_path / "draft.json"
    assert (
        cli_main(
            [
                "draft-skill-policy",
                "--config",
                str(config),
                "--home",
                str(home),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert out.is_file()
    assert "placeholders need operator review" in capsys.readouterr().out


def _single_candidate(text: str) -> str:
    """The only review-aid candidate in a drafted excerpt (test only)."""
    found = [match.group(0) for match in REVIEW_AID_REF_RE.finditer(text)]
    assert len(found) == 1, f"draft excerpt must isolate one candidate: {text!r}"
    return found[0]


def test_shared_line_drafts_one_excerpt_per_candidate(tmp_path: Path) -> None:
    """One line with three candidates drafts three distinct single-candidate excerpts."""
    line = "Use /alpha and /beta, then run `/gamma-setup`."
    member = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=default_skill_md_text("alpha", line),
    )
    config, _, bundle_dir, home = _setup(tmp_path, [member])
    outcome = build_draft(config=config, home=home, policy=None)
    assert outcome.ok, outcome.faults
    assert outcome.document is not None
    reviews = outcome.document["reviews"]
    assert isinstance(reviews, list)
    assert len(reviews) == 1
    entry = reviews[0]
    assert isinstance(entry, dict)
    rulings = entry["references"]
    assert isinstance(rulings, list)
    assert len(rulings) == 3
    texts: list[str] = []
    for ruling in rulings:
        assert isinstance(ruling, dict)
        assert ruling["classification"] is None
        assert ruling["reason"] is None
        assert ruling["origin"] == "SKILL.md"
        text = str(ruling["text"])
        assert text in line
        texts.append(text)
    assert len(set(texts)) == 3
    assert [_single_candidate(text) for text in texts] == [
        "/alpha",
        "/beta",
        "/gamma-setup",
    ]
    fills: dict[str, dict[str, object]] = {
        "/alpha": {
            "classification": "not-a-skill-reference",
            "reason": "Prose mention, not a skill.",
        },
        "/beta": {
            "classification": "not-a-skill-reference",
            "reason": "Prose mention, not a skill.",
        },
        "/gamma-setup": {
            "classification": "unavailable-nonrequired-reference",
            "target": "gamma-setup",
            "reason": "Operator-side setup helper.",
            "notice": "Run it in your own client; out of scope for the Worker.",
        },
    }
    filled_refs: list[dict[str, object]] = []
    for text in texts:
        filled_refs.append({"origin": "SKILL.md", "text": text, **fills[_single_candidate(text)]})
    approved = build_policy_document([member])
    approved_reviews = approved["reviews"]
    assert isinstance(approved_reviews, list)
    approved_entry = approved_reviews[0]
    assert isinstance(approved_entry, dict)
    approved_entry["references"] = filled_refs
    filled_bytes = dumps_canonical(approved)
    filled_value, problem = loads_strict(filled_bytes)
    assert problem is None
    filled_policy, fill_faults = validate_policy_object(filled_value)
    assert filled_policy is not None, [str(fault) for fault in fill_faults]
    filled_path = tmp_path / "filled-policy.json"
    filled_path.write_bytes(filled_bytes)
    prepared = prepare_skill_bundle(
        config=_loaded_config(config),
        policy=filled_path,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert prepared.ok, [str(fault) for fault in prepared.faults]
    from coding_agent.skillbundle.deployment import verify_skill_bundle_deployment

    verified = verify_skill_bundle_deployment(
        config=_loaded_config(config), policy=filled_path, bundle_dir=bundle_dir, home=home
    )
    assert verified.ok, [str(fault) for fault in verified.faults]


def test_single_candidate_line_uses_whole_line(tmp_path: Path) -> None:
    """A line holding one candidate still drafts the whole stripped line."""
    body = "Only /solo here."
    member = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=default_skill_md_text("alpha", body),
    )
    _, _, _, home = _setup(tmp_path, [member])
    config = tmp_path / "workflow.yml"
    outcome = build_draft(config=config, home=home, policy=None)
    assert outcome.ok, outcome.faults
    assert outcome.document is not None
    reviews = outcome.document["reviews"]
    assert isinstance(reviews, list)
    assert len(reviews) == 1
    entry = reviews[0]
    assert isinstance(entry, dict)
    rulings = entry["references"]
    assert isinstance(rulings, list)
    assert len(rulings) == 1
    ruling = rulings[0]
    assert isinstance(ruling, dict)
    assert str(ruling["text"]) == body


def test_shared_token_excerpts_are_reported(tmp_path: Path) -> None:
    """Two candidates in one token draft per-candidate excerpts plus a shared-excerpt report line."""
    line = "See /a/b for details."
    member = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=default_skill_md_text("alpha", line),
    )
    _, _, _, home = _setup(tmp_path, [member])
    config = tmp_path / "workflow.yml"
    outcome = build_draft(config=config, home=home, policy=None)
    assert outcome.ok, outcome.faults
    assert outcome.document is not None
    reviews = outcome.document["reviews"]
    assert isinstance(reviews, list)
    assert len(reviews) == 1
    entry = reviews[0]
    assert isinstance(entry, dict)
    rulings = entry["references"]
    assert isinstance(rulings, list)
    assert len(rulings) == 2
    texts = [str(ruling["text"]) for ruling in rulings if isinstance(ruling, dict)]
    assert len(texts) == 2
    assert [_single_candidate(text) for text in texts] == ["/a", "/b"]
    assert all(text in line for text in texts)
    assert "shared excerpt" in outcome.report
    assert "merge by hand" in outcome.report


def test_prior_suggestion_survives_narrower_excerpt(tmp_path: Path) -> None:
    """A prior ruling still suggests when the new excerpt differs but covers the same candidate."""
    old = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=default_skill_md_text("alpha", "Use /beta for that."),
        references=(
            {
                "classification": "not-a-skill-reference",
                "origin": "SKILL.md",
                "reason": "old ruling",
                "text": "Use /beta for that.",
            },
        ),
    )
    config, policy, _, home = _setup(tmp_path, [old])
    changed = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=default_skill_md_text(
            "alpha", "Use /beta for that, and also see /other."
        ),
    )
    write_fake_store(home, fake_store_spec_for([changed]))
    config.write_text(
        minimal_config_text(skills_entries_for([changed])), encoding="utf-8"
    )
    outcome = build_draft(config=config, home=home, policy=policy)
    assert outcome.ok, outcome.faults
    assert outcome.document is not None
    reviews = outcome.document["reviews"]
    assert isinstance(reviews, list)
    assert len(reviews) == 2
    redrafted = reviews[1]
    assert isinstance(redrafted, dict)
    rulings = redrafted["references"]
    assert isinstance(rulings, list)
    beta_texts = [
        str(ruling["text"])
        for ruling in rulings
        if isinstance(ruling, dict)
        and _single_candidate(str(ruling["text"])) == "/beta"
    ]
    assert len(beta_texts) == 1
    assert beta_texts[0] != "Use /beta for that."
    assert "previously ruled 'not-a-skill-reference'" in outcome.report


def test_prior_substring_without_candidate_match_suggests_nothing(
    tmp_path: Path,
) -> None:
    """A prior text holding the candidate only as a substring suggests nothing."""
    old = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=default_skill_md_text("alpha", "See /beta-x here."),
        references=(
            {
                "classification": "not-a-skill-reference",
                "origin": "SKILL.md",
                "reason": "old ruling",
                "text": "See /beta-x here.",
            },
        ),
    )
    config, policy, _, home = _setup(tmp_path, [old])
    changed = DeclaredMember(
        name="alpha",
        source=SOURCE_A,
        commit=COMMIT_A,
        skill_md=default_skill_md_text("alpha", "Use /beta now."),
    )
    write_fake_store(home, fake_store_spec_for([changed]))
    config.write_text(
        minimal_config_text(skills_entries_for([changed])), encoding="utf-8"
    )
    outcome = build_draft(config=config, home=home, policy=policy)
    assert outcome.ok, outcome.faults
    assert "suggestion (unconfirmed): reference" not in outcome.report


def _fill_from_reference(
    draft_doc: dict[str, Any], reference_doc: dict[str, Any], home: Path
) -> dict[str, Any]:
    """Resolve every draft placeholder from the reference policy (test only)."""
    from coding_agent.skillbundle.installer import base_store_for

    ref_by_name = {
        (str(r["sourceIdentity"]), str(r["name"])): r
        for r in reference_doc["reviews"]
        if isinstance(r, dict)
    }
    base = base_store_for(home)
    filled_reviews: list[dict[str, Any]] = []
    reviews = draft_doc["reviews"]
    assert isinstance(reviews, list)
    for entry in reviews:
        assert isinstance(entry, dict)
        key = (str(entry["sourceIdentity"]), str(entry["name"]))
        ref = ref_by_name[key]
        assert isinstance(ref, dict)
        ref_files = {str(f["path"]): f for f in ref["files"] if isinstance(f, dict)}
        filled_files: list[dict[str, Any]] = []
        files = entry["files"]
        assert isinstance(files, list)
        for record in files:
            assert isinstance(record, dict)
            match = ref_files[str(record["path"])]
            assert isinstance(match, dict)
            filled_files.append({**record, "disposition": match["disposition"], "reason": match["reason"]})
        ref_refs = ref["references"]
        assert isinstance(ref_refs, list)
        filled_refs: list[dict[str, Any]] = []
        references = entry["references"]
        assert isinstance(references, list)
        for ruling in references:
            assert isinstance(ruling, dict)
            origin = str(ruling["origin"])
            candidate = _single_candidate(str(ruling["text"]))
            origin_text = (base / str(entry["name"]) / origin).read_text(encoding="utf-8")
            assert str(ruling["text"]) in origin_text, (
                f"draft excerpt not in delivered text: {ruling!r}"
            )
            matches = [
                other
                for other in ref_refs
                if isinstance(other, dict)
                and str(other["origin"]) == origin
                and candidate
                in {
                    match.group(0)
                    for match in REVIEW_AID_REF_RE.finditer(str(other["text"]))
                }
            ]
            assert matches, f"no reference ruling covers {ruling!r}"
            first = matches[0]
            assert all(
                other["classification"] == first["classification"]
                and other.get("target") == first.get("target")
                and other["reason"] == first["reason"]
                and other.get("notice") == first.get("notice")
                for other in matches
            )
            filled: dict[str, Any] = {
                "origin": origin,
                "text": ruling["text"],
                "classification": first["classification"],
                "reason": first["reason"],
            }
            if first.get("target") is not None:
                filled["target"] = first["target"]
            if first.get("notice") is not None:
                filled["notice"] = first["notice"]
            filled_refs.append(filled)
        filled_reviews.append(
            {
                **entry,
                "files": filled_files,
                "references": filled_refs,
                "reason": ref["reason"],
                "requires": ref["requires"],
            }
        )
    return {**draft_doc, "reviews": filled_reviews}


def _coverage_map(doc: dict[str, Any], home: Path) -> dict[tuple[str, str, str], tuple[Any, ...]]:
    """Per-candidate ruling coverage: (member, origin, candidate) -> ruling."""
    from coding_agent.skillbundle.installer import base_store_for

    base = base_store_for(home)
    coverage: dict[tuple[str, str, str], tuple[Any, ...]] = {}
    reviews = doc["reviews"]
    assert isinstance(reviews, list)
    for entry in reviews:
        assert isinstance(entry, dict)
        name = str(entry["name"])
        references = entry["references"]
        assert isinstance(references, list)
        for ruling in references:
            assert isinstance(ruling, dict)
            origin = str(ruling["origin"])
            origin_text = (base / name / origin).read_text(encoding="utf-8")
            ruled = {
                match.group(0)
                for match in REVIEW_AID_REF_RE.finditer(str(ruling["text"]))
            }
            for match in REVIEW_AID_REF_RE.finditer(origin_text):
                candidate = match.group(0)
                if candidate in ruled:
                    coverage[(name, origin, candidate)] = (
                        ruling["classification"],
                        ruling.get("target"),
                        ruling["reason"],
                        ruling.get("notice"),
                    )
    return coverage


@requires_real_installer
def test_reference_deployment_draft_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Draft against the reference deployment, fill it, prepare and verify.

    Installs ``docker/deployment/workflow.yml`` at its pinned commit through
    the released installer, drafts against an empty policy, resolves every
    placeholder from ``docker/deployment/skill-policy.json``, then proves
    ``prepare-skill-bundle`` and ``verify-skill-bundle`` pass and the filled
    draft matches the reference policy (references compared by
    per-candidate coverage: the hand-maintained reference groups several
    candidates under one ruling where the draft proposes one per candidate).
    """
    from coding_agent.skillbundle.deployment import verify_skill_bundle_deployment
    from coding_agent.skillbundle.installer import SubprocessInstallerAdapter

    repo_root = Path(__file__).resolve().parent.parent
    deploy = tmp_path / "deploy"
    shutil.copytree(repo_root / "docker" / "deployment", deploy)
    config = deploy / "workflow.yml"
    reference_doc = json.loads((deploy / "skill-policy.json").read_bytes().decode("utf-8"))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    bundle_dir = tmp_path / "bundle"
    empty_policy = tmp_path / "empty-policy.json"
    write_policy_file(
        empty_policy,
        {"kind": "coding-agent.skill-bundle-policy", "reviews": [], "schemaVersion": 1},
    )
    install_policy = tmp_path / "install-policy.json"
    write_policy_file(install_policy, reference_doc)
    prepared_install = prepare_skill_bundle(
        config=_loaded_config(config),
        policy=install_policy,
        bundle_dir=bundle_dir,
        home=home,
        mode="install",
        installer=SubprocessInstallerAdapter(),
    )
    assert prepared_install.ok, [str(f) for f in prepared_install.faults]
    draft_path = tmp_path / "draft.json"
    outcome = draft_to(config=config, home=home, policy=empty_policy, out=draft_path)
    assert outcome.ok, outcome.faults
    assert outcome.document is not None
    assert outcome.placeholders > 0
    draft_doc = json.loads(draft_path.read_bytes().decode("utf-8"))
    filled = _fill_from_reference(draft_doc, reference_doc, home)
    filled_bytes = dumps_canonical(filled)
    filled_value, problem = loads_strict(filled_bytes)
    assert problem is None
    filled_policy, fill_faults = validate_policy_object(filled_value)
    assert filled_policy is not None, [str(f) for f in fill_faults]
    filled_path = tmp_path / "filled-policy.json"
    filled_path.write_bytes(filled_bytes)
    prepared = prepare_skill_bundle(
        config=_loaded_config(config),
        policy=filled_path,
        bundle_dir=bundle_dir,
        home=home,
        mode="installed",
        installer=FakeInstallerAdapter(),
    )
    assert prepared.ok, [str(f) for f in prepared.faults]
    verified = verify_skill_bundle_deployment(
        config=_loaded_config(config), policy=filled_path, bundle_dir=bundle_dir, home=home
    )
    assert verified.ok, [str(f) for f in verified.faults]
    ref_without_refs = {
        **reference_doc,
        "reviews": [{**r, "references": []} for r in reference_doc["reviews"]],
    }
    filled_without_refs = {
        **filled,
        "reviews": [{**r, "references": []} for r in filled["reviews"]],
    }
    assert dumps_canonical(filled_without_refs) == dumps_canonical(ref_without_refs)
    assert _coverage_map(filled, home) == _coverage_map(reference_doc, home)
    _ = monkeypatch


def test_draft_with_version_2_config_reports_migration_refusal(
    tmp_path: Path,
) -> None:
    """The draft goes through the single loader, so a `version: 2` file gets the migration diagnostic."""
    from support.skill_bundle import minimal_config_text

    from coding_agent.skillbundle.draft import build_draft

    config = tmp_path / "workflow.yml"
    config.write_text(
        minimal_config_text([]).replace("version: 3", "version: 2", 1),
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)

    outcome = build_draft(config=config, home=home, policy=None)

    assert not outcome.ok
    assert any("unsupported version 2" in fault for fault in outcome.faults), outcome.faults
