"""Trusted issue comments pinned at Claim (ADR 0035).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §7.1 (the
Fingerprint covers title and body only; comments are pinned separately) and
§7.4 (comments reach a step only as declared data).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from support.workflow_config import load_config

from coding_agent.inputs.comments import (
    MAX_PINNED_COMMENT_CHARS,
    MAX_PINNED_COMMENTS_CHARS,
    comments_archive_blob,
    comments_set_digest,
    new_trusted_comments,
    pin_trusted_comments,
    render_comments_block,
    select_trusted_comments,
)
from coding_agent.inputs.port import InputComment
from coding_agent.ledger.store import step_effect_marker

ISSUE_AUTHOR_ID = 11
OWNER_ID = 22
AGENT_ID = 33
THIRD_PARTY_ID = 44

CLAIM_MARKER = step_effect_marker("triage#7/1", "", 0, 1, kind="claim")
TERMINAL_MARKER = step_effect_marker("triage#7/1", "", 0, 0, kind="terminal")
PUBLICATION_MARKER = step_effect_marker(
    "triage#7/1", "brief", 2, 0, kind="post-comment"
)

PUBLICATION_BODY = (
    "## Agent Brief\n\nDo the thing.\n\n"
    "---\n"
    "Attempt: triage#7/1\n"
    "Workflow: triage\n"
    "Step: brief\n"
    "Configuration: abc123\n"
    f"{PUBLICATION_MARKER}"
)


def _comment(
    comment_id: int,
    body: str,
    *,
    author_id: int | None,
    author_login: str = "someone",
    created_at: str = "2026-11-01T00:00:00Z",
    updated_at: str | None = None,
) -> InputComment:
    return InputComment(
        comment_id=comment_id,
        body=body,
        author_login=author_login,
        author_id=author_id,
        marker_present="attempt=" in body,
        created_at=created_at,
        updated_at=updated_at,
    )


def _fixture_comments() -> list[InputComment]:
    reporter_note = _comment(
        1, "The traceback is attached.", author_id=ISSUE_AUTHOR_ID
    )
    owner_note = _comment(2, "Triage brief: do the thing.", author_id=OWNER_ID)
    agent_note = _comment(
        3, "A hand-written owner note under a shared identity.", author_id=AGENT_ID
    )
    third_party = _comment(4, "Me too!", author_id=THIRD_PARTY_ID)
    ghost = _comment(5, "I was deleted.", author_id=None, author_login="ghost")
    claim = _comment(
        6,
        f"coding-agent claims triage#7/1.\n\n{CLAIM_MARKER}\n",
        author_id=AGENT_ID,
        author_login="coding-agent",
    )
    terminal = _comment(
        7,
        f"coding-agent triage#7/1: completed\n\n{TERMINAL_MARKER}\n",
        author_id=AGENT_ID,
        author_login="coding-agent",
    )
    publication = _comment(
        8, PUBLICATION_BODY, author_id=AGENT_ID, author_login="coding-agent"
    )
    return [
        reporter_note,
        owner_note,
        agent_note,
        third_party,
        ghost,
        claim,
        terminal,
        publication,
    ]


def _select(
    comments: list[InputComment],
    *,
    agent_id: int | None = AGENT_ID,
) -> list:
    return select_trusted_comments(
        comments,
        issue_author_id=ISSUE_AUTHOR_ID,
        owner_id=OWNER_ID,
        agent_id=agent_id,
    )


def test_fixture_yields_exactly_the_trusted_four_oldest_first() -> None:
    selected = _select(_fixture_comments())
    assert [(item.comment.comment_id, item.role) for item in selected] == [
        (1, "issue-author"),
        (2, "repository-owner"),
        (3, "agent-identity"),
        (8, "worker-publication"),
    ]


def test_publication_is_reduced_to_its_document() -> None:
    (publication,) = [
        item for item in _select(_fixture_comments()) if item.role == "worker-publication"
    ]
    assert publication.body == "## Agent Brief\n\nDo the thing."
    assert publication.workflow_id == "triage"


def test_claim_and_terminal_comments_are_excluded() -> None:
    assert [item.comment.comment_id for item in _select(_fixture_comments())] == [1, 2, 3, 8]


def test_ghost_comment_is_never_trusted() -> None:
    (ghost,) = [
        comment for comment in _fixture_comments() if comment.comment_id == 5
    ]
    assert ghost.author_id is None
    assert 5 not in [item.comment.comment_id for item in _select([ghost])]


def test_trust_is_by_numeric_id_never_login() -> None:
    spoof = _comment(
        9,
        "I renamed my login to the reporter's old one.",
        author_id=THIRD_PARTY_ID,
        author_login="reporter-old-login",
    )
    assert _select([spoof]) == []


def test_unmarked_owner_comment_under_shared_identity_is_agent_identity() -> None:
    shared = OWNER_ID
    comment = _comment(10, "Hand-written.", author_id=shared)
    (selected,) = select_trusted_comments(
        [comment],
        issue_author_id=ISSUE_AUTHOR_ID,
        owner_id=OWNER_ID,
        agent_id=shared,
    )
    assert selected.role == "agent-identity"


def test_owner_who_wrote_the_issue_is_issue_author() -> None:
    comment = _comment(11, "My own issue.", author_id=OWNER_ID)
    (selected,) = select_trusted_comments(
        [comment],
        issue_author_id=OWNER_ID,
        owner_id=OWNER_ID,
        agent_id=AGENT_ID,
    )
    assert selected.role == "issue-author"


def test_edit_by_another_account_does_not_change_trust() -> None:
    comment = _comment(
        12,
        "Still the reporter's words.",
        author_id=ISSUE_AUTHOR_ID,
        updated_at="2026-11-02T00:00:00Z",
    )
    (selected,) = _select([comment])
    assert selected.role == "issue-author"


def test_forged_marker_by_another_account_is_not_a_publication() -> None:
    forged = _comment(
        13, PUBLICATION_BODY, author_id=THIRD_PARTY_ID, author_login="mallory"
    )
    assert _select([forged]) == []


def test_long_comment_is_truncated_with_archive_pointer() -> None:
    body = "x" * (MAX_PINNED_COMMENT_CHARS + 100)
    (selected,) = _select([_comment(20, body, author_id=ISSUE_AUTHOR_ID)])
    (pinned,) = pin_trusted_comments([selected])
    block = render_comments_block([pinned])
    assert body not in block
    assert body[:MAX_PINNED_COMMENT_CHARS] in block
    assert "Attempt Archive" in block
    assert str(len(body)) in block


def test_archive_blob_holds_the_full_content() -> None:
    body = "y" * (MAX_PINNED_COMMENT_CHARS + 100)
    (selected,) = _select([_comment(21, body, author_id=ISSUE_AUTHOR_ID)])
    (pinned,) = pin_trusted_comments([selected])
    blob = comments_archive_blob([pinned])
    assert len(blob) == 1
    raw = next(iter(blob.values()))
    assert body.encode("utf-8") in raw


def test_oversized_set_drops_oldest_whole_with_omission_line() -> None:
    comments = [
        _comment(30 + index, "z" * 8000, author_id=ISSUE_AUTHOR_ID) for index in range(10)
    ]
    pinned = pin_trusted_comments(_select(comments))
    assert len(pinned) == 10
    block = render_comments_block(pinned)
    assert "earlier Trusted Comments omitted" in block
    assert "full content is in the Attempt Archive" in block
    assert "## Comment 30 " not in block
    assert "## Comment 39 " in block
    total_capacity = MAX_PINNED_COMMENTS_CHARS
    assert total_capacity == 64000


def test_empty_set_renders_as_present() -> None:
    block = render_comments_block([])
    assert "No Trusted Comments were pinned at Claim." in block


def test_new_trusted_comments_reports_only_unpinned() -> None:
    comments = _fixture_comments()
    pinned = pin_trusted_comments(_select(comments))
    pinned_ids = {comment.comment_id for comment in pinned}
    late = _comment(50, "The answer you asked for.", author_id=ISSUE_AUTHOR_ID)
    fresh = new_trusted_comments(
        [*comments, late],
        pinned_ids,
        issue_author_id=ISSUE_AUTHOR_ID,
        owner_id=OWNER_ID,
        agent_id=AGENT_ID,
    )
    assert [item.comment.comment_id for item in fresh] == [50]


def test_set_digest_is_stable_and_covers_all_comments() -> None:
    pinned = pin_trusted_comments(_select(_fixture_comments()))
    first = comments_set_digest(pinned)
    assert first == comments_set_digest(list(pinned))
    assert first != comments_set_digest(pinned[:2])


# --- Static validation -------------------------------------------------------


_ISSUE_WORKFLOW = (
    "workflows:\n"
    "  implement:\n"
    "    model: sonnet\n"
    "    input: {type: issue, label: ready-for-agent}\n"
    "    limits: {cost-usd: 6, effective-tokens: 1500000,"
    " tool-calls: 300, wall-clock-minutes: 30}\n"
    "    start: work\n"
    "    defaults:\n"
    "      max-executions: 1\n"
    "      routes: {error: {end: failed}, limit-exceeded: {end: unfinished}}\n"
    "    steps:\n"
    "      work:\n"
    "        agent:\n"
    "          tools: [workspace-read]\n"
    "          inputs:\n"
    "            - input.comments\n"
    "          results:\n"
    "            done: {summary: document}\n"
    "        routes: {done: publish}\n"
    "      publish:\n"
    "        operation: post-comment\n"
    "        with: {body: work.done.summary}\n"
    "        routes: {posted: {end: completed}}\n"
)


def _issue_config(workflows: str) -> str:
    return (
        "version: 4\n"
        "target:\n"
        "  kind: github\n"
        "  repository: OWNER/REPO\n"
        "  base-branch: main\n"
        "  token-env: GITHUB_TOKEN\n"
        "models:\n"
        "  sonnet:\n"
        "    provider: anthropic\n"
        "    model: claude-sonnet-5\n"
        "    endpoint: messages\n"
        "    effort: medium\n"
        "    api-key-env: ANTHROPIC_API_KEY\n"
        "    prices-usd-per-mtok: {input: 2.00, output: 10.00,"
        " cache-read: 0.20, cache-write: 2.50}\n"
        "    context-window-tokens: 1000000\n"
        "    context-threshold-tokens: 80000\n"
        "    output-cap-tokens: 32000\n"
        f"{workflows}"
    )


def _write_config(tmp_path: Path, text: str) -> Path:
    config = tmp_path / "workflows.yaml"
    config.write_text(text, encoding="utf-8")
    return config


def _without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_PROXY_API_KEY", raising=False)


def test_validate_accepts_required_input_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    config, faults = load_config(
        _write_config(tmp_path, _issue_config(_ISSUE_WORKFLOW))
    )
    assert faults == []
    assert config is not None


def test_validate_accepts_optional_input_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    workflows = _ISSUE_WORKFLOW.replace(
        "            - input.comments\n", "            - {optional: input.comments}\n"
    )
    config, faults = load_config(_write_config(tmp_path, _issue_config(workflows)))
    assert faults == []
    assert config is not None


def test_validate_accepts_input_comments_on_later_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    workflows = _ISSUE_WORKFLOW.replace(
        "          inputs:\n            - input.comments\n", ""
    ).replace(
        "      publish:\n"
        "        operation: post-comment\n"
        "        with: {body: work.done.summary}\n"
        "        routes: {posted: {end: completed}}\n",
        "      second:\n"
        "        agent:\n"
        "          tools: [workspace-read]\n"
        "          inputs:\n"
        "            - input.comments\n"
        "          results:\n"
        "            done: {}\n"
        "        routes: {done: publish}\n"
        "      publish:\n"
        "        operation: post-comment\n"
        "        with: {body: work.done.summary}\n"
        "        routes: {posted: {end: completed}}\n",
    )
    workflows = workflows.replace("        routes: {done: publish}\n", "        routes: {done: second}\n", 1)
    config, faults = load_config(_write_config(tmp_path, _issue_config(workflows)))
    assert faults == []
    assert config is not None


def test_validate_refuses_input_comments_in_pull_request_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    workflows = _ISSUE_WORKFLOW.replace(
        "input: {type: issue, label: ready-for-agent}",
        "input: {type: pull-request, label: agent:review}",
    )
    _, faults = load_config(_write_config(tmp_path, _issue_config(workflows)))
    assert faults, "pull-request input.comments validated but must be refused"
    assert faults[0].path.startswith(
        "workflows.implement.steps.work.agent.inputs"
    )
    assert "pull-request" in faults[0].detail


def test_validate_refuses_input_comments_as_operation_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    workflows = _ISSUE_WORKFLOW.replace(
        "        with: {body: work.done.summary}\n",
        "        with: {body: input.comments}\n",
    )
    _, faults = load_config(_write_config(tmp_path, _issue_config(workflows)))
    assert faults, "input.comments operation body validated but must be refused"
    assert faults[0].path == "workflows.implement.steps.publish.with.body"


def test_validate_refuses_input_as_step_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_env(monkeypatch)
    workflows = _ISSUE_WORKFLOW.replace("      work:\n", "      input:\n").replace(
        "    start: work\n", "    start: input\n"
    )
    _, faults = load_config(_write_config(tmp_path, _issue_config(workflows)))
    assert faults, "step id 'input' validated but must be refused"
    assert faults[0].path == "workflows.implement.steps.input"
