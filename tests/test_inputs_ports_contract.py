"""The port boundary contract: AgentIdentityPort / InputPort / IssueTrackerPort / CodeHostPort.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §5 (Workflow
Inputs and selection), §6 (the Claim), §7 (Gates and the base-branch tip),
§10.5 (the pull-request-to-issue link line), §10.4 (the workflows path
authorship refusal), §14 (Effect Intent receipts and the neutral failure
taxonomy) and §18 (per-port identity; the two structural abstentions: no
port applies a Selection Label and none edits an input's title or body).

Row ids: INP-1 … INP-20, OP-14 (with S7b), REC-22.

This suite pins the boundary the split established, not the flows the
step-tracker contract suite already covers over fake and recorded GitHub
fixtures: the exact operation set of each port, the ``pull_requests``
facet, ``delivery_reference`` and the ``CodeHostProfile`` facts, the
neutral-error translation at the adapter boundary, and the two
no-leak rules (no ``coding_agent.github`` import and no ``GitHubClient``
type outside the adapter modules; no GitHub host literal in core
runtime code). There is no combined port: the runtime threads the narrow
port each call site needs, and one production object serves them all.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from coding_agent.github.client import DEFAULT_BASE_URL, GitHubClient
from coding_agent.inputs import github as github_adapter
from coding_agent.inputs.errors import (
    CredentialFailure,
    InputError,
    InputNotFound,
    PermissionRefusal,
)
from coding_agent.inputs.claim import claim_marker_for
from coding_agent.inputs.github import GitHubStepTracker
from coding_agent.inputs.port import (
    AgentIdentityPort,
    CodeHostPort,
    InputPort,
    IssueTrackerPort,
)
from coding_agent.inputs.profile import (
    GITHUB_PROFILE,
    WORKFLOWS_PATH_PREFIX,
    WORKFLOWS_PATH_ROOT,
    CodeHostProfile,
)
from support.fake_step_tracker import FakeStepTracker

TEST_BASE_URL = "https://api.github.test"


AGENT_IDENTITY_PORT_OPS = frozenset(
    {
        "target_repo",
        "agent_login",
        "agent_id",
        "fetch_agent_identity",
        "fetch_repository_owner_id",
    }
)
"""The Agent Identity every port carries (§2.1, §14, §18)."""

INPUT_PORT_OPS = frozenset(
    {
        "target_repo",
        "agent_login",
        "agent_id",
        "fetch_agent_identity",
        "fetch_repository_owner_id",
        "fetch_input",
        "list_issue_candidates",
        "list_pull_request_candidates",
        "read_labeled_event",
        "has_open_blocker",
        "assign_input",
        "unassign_input",
        "format_marker",
        "post_claim_comment",
        "list_comments",
        "find_comment_by_marker",
        "delete_comment",
        "post_comment",
        "remove_label",
        "add_labels",
    }
)
"""Every operation ``InputPort`` expresses (INP-1 … INP-13, INP-19, INP-20)."""

ISSUE_TRACKER_PORT_OPS = frozenset(INPUT_PORT_OPS | {"delivery_reference"})
"""``InputPort`` plus the pull-request-to-issue link line (INP-14)."""

CODE_HOST_PORT_OPS = frozenset(
    ISSUE_TRACKER_PORT_OPS
    | {
        "profile",
        "pull_requests",
        "fetch_base_tip",
        "list_open_pull_requests",
        "publish_review",
        "list_reviews",
        "find_review_by_marker",
        "open_pull_request",
        "find_pull_request",
    }
)
"""``IssueTrackerPort`` plus the repository-host delivery operations (INP-15 … INP-18, OP-14, REC-22)."""

FORBIDDEN_PORT_OPS = frozenset(
    {
        "apply_selection_label",
        "add_selection_label",
        "remove_selection_label",
        "set_title",
        "update_title",
        "edit_title",
        "set_body",
        "update_body",
        "edit_body",
    }
)
"""Writes the §18 structural abstentions forbid: no port may express them."""


def _port_ops(port: type) -> frozenset[str]:
    """The public operation names a port Protocol declares."""
    return frozenset(name for name in dir(port) if not name.startswith("_"))


def _make_adapter() -> GitHubStepTracker:
    """The GitHub adapter over no network: the constructor performs no I/O."""
    return GitHubStepTracker(
        owner="octocat",
        repo="sandbox",
        client=GitHubClient("github_pat_testtoken", base_url=TEST_BASE_URL),
        agent_login="coding-agent",
        agent_id=1001,
    )


def test_agent_identity_port_exposes_only_the_agent_identity() -> None:
    """§2.1, §14, §18: ``AgentIdentityPort`` is exactly its 5 operations."""
    assert _port_ops(AgentIdentityPort) == AGENT_IDENTITY_PORT_OPS


def test_input_port_exposes_exactly_the_claimable_input_operations() -> None:
    """INP-1 … INP-13, INP-19, INP-20: ``InputPort`` is exactly its 20 operations."""
    assert _port_ops(InputPort) == INPUT_PORT_OPS
    assert AgentIdentityPort in InputPort.__mro__


def test_issue_tracker_port_adds_only_the_delivery_reference() -> None:
    """INP-14: ``IssueTrackerPort`` is ``InputPort`` plus ``delivery_reference``."""
    assert _port_ops(IssueTrackerPort) == ISSUE_TRACKER_PORT_OPS
    assert InputPort in IssueTrackerPort.__mro__


def test_code_host_port_adds_only_the_repository_operations() -> None:
    """INP-15 … INP-18, OP-14, REC-22: ``CodeHostPort`` is the tracker port plus delivery."""
    assert _port_ops(CodeHostPort) == CODE_HOST_PORT_OPS
    assert IssueTrackerPort in CodeHostPort.__mro__


def test_no_port_can_apply_a_selection_label_or_edit_title_or_body() -> None:
    """§18: the abstentions hold structurally, on every port."""
    for port in (AgentIdentityPort, InputPort, IssueTrackerPort, CodeHostPort):
        assert _port_ops(port).isdisjoint(FORBIDDEN_PORT_OPS)


PORT_PROPERTIES = frozenset(
    {"target_repo", "agent_login", "agent_id", "profile", "pull_requests"}
)
"""Port members exposed as properties rather than methods."""


def _assert_serves_all_ports(servant: object) -> None:
    """Every port operation is present: methods callable, properties readable."""
    for operation in CODE_HOST_PORT_OPS:
        assert hasattr(servant, operation), operation
        if operation not in PORT_PROPERTIES:
            assert callable(getattr(servant, operation, None)), operation


def test_fake_serves_every_port() -> None:
    """The fake implements every operation of every port (INP-1 … INP-20, OP-14)."""
    fake = FakeStepTracker()
    _assert_serves_all_ports(fake)
    assert _port_ops(type(fake)).isdisjoint(FORBIDDEN_PORT_OPS)
    assert isinstance(fake.profile, CodeHostProfile)


def test_adapter_serves_every_port() -> None:
    """The GitHub adapter implements every operation of every port."""
    adapter = _make_adapter()
    _assert_serves_all_ports(adapter)
    for operation in FORBIDDEN_PORT_OPS:
        assert not hasattr(adapter, operation), operation
    assert isinstance(adapter.profile, CodeHostProfile)


def test_pull_requests_facet_is_the_input_port_for_pull_request_workflows() -> None:
    """``CodeHostPort.pull_requests`` reuses ``InputPort`` (§5, pull-request Workflows)."""
    fake = FakeStepTracker()
    assert fake.pull_requests is fake
    adapter = _make_adapter()
    assert adapter.pull_requests is adapter
    for facet in (fake.pull_requests, adapter.pull_requests):
        for operation in INPUT_PORT_OPS:
            assert hasattr(facet, operation), operation
            if operation not in PORT_PROPERTIES:
                assert callable(getattr(facet, operation, None)), operation


def test_github_profile_carries_todays_host_facts() -> None:
    """The profile value holds every fact runtime code used to hard-code."""
    assert GITHUB_PROFILE.host_label == "GitHub"
    assert GITHUB_PROFILE.base_url == "https://github.com"
    assert GITHUB_PROFILE.max_body_chars == 65_536
    assert GITHUB_PROFILE.workflows_path_prefix == ".github/workflows/"
    assert GITHUB_PROFILE.workflows_path_root == ".github/workflows"
    assert GITHUB_PROFILE.closing_keyword == "Closes"
    assert GITHUB_PROFILE.non_closing_keyword == "Part of"
    assert GITHUB_PROFILE.marker_fence_open == "<!--"
    assert GITHUB_PROFILE.marker_fence_close == "-->"


def test_delivery_reference_renders_through_the_profile() -> None:
    """INP-14: core passes the semantic reference; the profile formats the line."""
    assert GITHUB_PROFILE.delivery_reference("30") == "Closes #30"
    assert GITHUB_PROFILE.delivery_reference("30", draft=True) == "Part of #30"


def test_fake_and_adapter_agree_on_the_delivery_reference() -> None:
    """INP-14: both sides compose the link line through the same profile."""
    fake = FakeStepTracker()
    adapter = _make_adapter()
    assert fake.profile is GITHUB_PROFILE
    assert adapter.profile is GITHUB_PROFILE
    assert fake.delivery_reference("30") == "Closes #30"
    assert adapter.delivery_reference("30") == "Closes #30"
    assert fake.delivery_reference("30", draft=True) == "Part of #30"
    assert adapter.delivery_reference("30", draft=True) == "Part of #30"


def test_second_vendor_is_a_second_profile_value() -> None:
    """A second vendor is a second value: the keyword lives in the profile."""
    other = CodeHostProfile(
        host_label="Example",
        base_url="https://example.test",
        max_body_chars=1024,
        workflows_path_prefix=".workflows/",
        workflows_path_root=".workflows",
        closing_keyword="Fixes",
        non_closing_keyword="Refs",
    )
    assert other.delivery_reference("30") == "Fixes #30"
    assert other.delivery_reference("30", draft=True) == "Refs #30"
    fake = FakeStepTracker(profile=other)
    assert fake.delivery_reference("30") == "Fixes #30"


def test_profile_repository_url_and_workflows_predicate() -> None:
    """The clone URL and the §10.4 authorship scan read the profile, not literals."""
    assert GITHUB_PROFILE.repository_url("octocat", "sandbox") == (
        "https://github.com/octocat/sandbox.git"
    )
    assert GITHUB_PROFILE.is_workflows_path(".github/workflows")
    assert GITHUB_PROFILE.is_workflows_path(".github/workflows/ci.yaml")
    assert not GITHUB_PROFILE.is_workflows_path("src/coding_agent/runtime/git.py")


def test_production_factory_hides_the_concrete_client_type() -> None:
    """Core startup passes the token; only the adapter names ``GitHubClient``."""
    tracker = github_adapter.make_step_tracker(
        owner="octocat",
        repo="sandbox",
        token="github_pat_testtoken",
        agent_login="coding-agent",
        agent_id=1001,
        base_url=TEST_BASE_URL,
    )
    assert tracker.target_repo == "octocat/sandbox"
    assert tracker.agent_login == "coding-agent"
    assert tracker.profile is GITHUB_PROFILE


def test_adapter_failures_are_neutral_types_by_inheritance() -> None:
    """§14.4: adapter errors subclass the inputs taxonomy, so core catches neutrally."""
    from coding_agent.github.errors import (
        CredentialFailure as GitHubCredentialFailure,
    )
    from coding_agent.github.errors import (
        GitHubFailure,
    )
    from coding_agent.github.errors import (
        PermissionRefusal as GitHubPermissionRefusal,
    )
    from coding_agent.github.errors import (
        RateLimitExhausted as GitHubRateLimitExhausted,
    )
    from coding_agent.inputs.errors import (
        RateLimitExhausted as NeutralRateLimitExhausted,
    )
    from coding_agent.inputs.github import StepTrackerError

    assert issubclass(GitHubFailure, InputError)
    assert issubclass(GitHubCredentialFailure, CredentialFailure)
    assert issubclass(GitHubPermissionRefusal, PermissionRefusal)
    assert issubclass(GitHubRateLimitExhausted, NeutralRateLimitExhausted)
    assert issubclass(StepTrackerError, InputError)


def test_adapter_permission_refusal_is_caught_as_the_neutral_type(
    requests_mock: Any,
) -> None:
    """A 403 the rate headers do not explain raises neutral ``PermissionRefusal``."""
    adapter = _make_adapter()
    requests_mock.get(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30",
        status_code=403,
        headers={"x-ratelimit-remaining": "10"},
        json={"message": "refused"},
    )
    with pytest.raises(PermissionRefusal):
        adapter.fetch_input(30, "issue")
    with pytest.raises(InputError):
        adapter.fetch_input(30, "issue")


def test_adapter_transient_write_failure_is_caught_as_neutral_input_error(
    requests_mock: Any,
) -> None:
    """§14.4: a 5xx comment write raises a transient neutral ``InputError``."""
    adapter = _make_adapter()
    requests_mock.post(
        f"{TEST_BASE_URL}/repos/octocat/sandbox/issues/30/comments",
        status_code=500,
        json={"message": "boom"},
    )
    with pytest.raises(InputError) as exc_info:
        adapter.post_claim_comment(
            30, "issue", "a body", claim_marker_for("implement#30/1")
        )
    assert exc_info.value.transient is True
    assert exc_info.value.failure_class == "github"


def test_adapter_absent_profile_read_is_neutral_not_found(
    requests_mock: Any,
) -> None:
    """The deferred profile read reports absence as neutral ``InputNotFound``."""
    requests_mock.get(
        f"{DEFAULT_BASE_URL}/repos/octocat/sandbox/contents/profile.md?ref=main",
        status_code=404,
        json={"message": "Not Found"},
    )
    with pytest.raises(InputNotFound):
        github_adapter.read_text_at_revision(
            token="github_pat_testtoken",
            owner="octocat",
            repo="sandbox",
            path="profile.md",
            ref="main",
        )


_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "coding_agent"
"""The packaged source this boundary test walks."""

_ADAPTER_FILES = frozenset(
    {
        _SRC_ROOT / "github" / "__init__.py",
        _SRC_ROOT / "github" / "client.py",
        _SRC_ROOT / "github" / "errors.py",
        _SRC_ROOT / "github" / "issues.py",
        _SRC_ROOT / "inputs" / "github.py",
    }
)
"""Every module allowed to name the GitHub client, errors or API modules."""

_GITHUB_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+coding_agent\.github\b|import\s+coding_agent\.github\b)"
)
_CLIENT_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+\S+\s+import\s+[^\n]*|import\s+[^\n]*)\bGitHubClient\b"
)


def _python_files() -> list[Path]:
    return sorted(_SRC_ROOT.rglob("*.py"))


def test_no_module_outside_the_adapters_imports_the_github_modules() -> None:
    """The port boundary: core never imports ``coding_agent.github``."""
    offenders = [
        str(path.relative_to(_SRC_ROOT.parent.parent))
        for path in _python_files()
        if path not in _ADAPTER_FILES
        and any(
            _GITHUB_IMPORT_RE.match(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    ]
    assert offenders == []


def test_core_startup_never_names_the_concrete_client_type() -> None:
    """Adapter-owned startup: only the adapters name ``GitHubClient``."""
    offenders = [
        str(path.relative_to(_SRC_ROOT.parent.parent))
        for path in _python_files()
        if path not in _ADAPTER_FILES
        and any(
            _CLIENT_IMPORT_RE.match(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    ]
    assert offenders == []


def test_closing_keyword_literal_lives_only_in_the_profile() -> None:
    """Core never emits vendor link text: ``Closes #`` is a profile literal."""
    offenders = [
        str(path.relative_to(_SRC_ROOT.parent.parent))
        for path in _python_files()
        if path != _SRC_ROOT / "inputs" / "profile.py"
        and "Closes #" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_marker_construction_lives_only_in_the_adapter() -> None:
    """Core never renders marker text: the HTML comment is adapter-owned.

    The bare ``coding-agent attempt=`` fragment in ``inputs/comments.py``
    is the Claim-time read-back parse, which stays core-owned by design.
    """
    offenders = [
        str(path.relative_to(_SRC_ROOT.parent.parent))
        for path in _python_files()
        if path != _SRC_ROOT / "inputs" / "github.py"
        and "<!-- coding-agent attempt=" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_workflows_paths_have_a_single_definition() -> None:
    """The duplicated ``.github/workflows/`` constants collapsed to the profile."""
    definitions = [
        str(path.relative_to(_SRC_ROOT.parent.parent))
        for path in _python_files()
        if "WORKFLOWS_PATH_PREFIX = " in path.read_text(encoding="utf-8")
    ]
    assert definitions == ["src/coding_agent/inputs/profile.py"]
    from coding_agent.runtime import git as runtime_git
    from coding_agent.runtime import step_operations

    assert getattr(runtime_git, "WORKFLOWS_PATH_PREFIX") == WORKFLOWS_PATH_PREFIX
    assert getattr(runtime_git, "WORKFLOWS_PATH_ROOT") == WORKFLOWS_PATH_ROOT
    assert getattr(step_operations, "WORKFLOWS_PATH_PREFIX") == WORKFLOWS_PATH_PREFIX
    assert getattr(step_operations, "WORKFLOWS_PATH_ROOT") == WORKFLOWS_PATH_ROOT
    assert WORKFLOWS_PATH_PREFIX == GITHUB_PROFILE.workflows_path_prefix
    assert WORKFLOWS_PATH_ROOT == GITHUB_PROFILE.workflows_path_root


def test_body_limit_literal_lives_only_in_the_profile() -> None:
    """No second body-size literal: the alias reads the profile value."""
    from coding_agent.runtime.step_publication import GITHUB_BODY_CHAR_LIMIT

    assert GITHUB_BODY_CHAR_LIMIT == GITHUB_PROFILE.max_body_chars
    underscored = [
        str(path.relative_to(_SRC_ROOT.parent.parent))
        for path in _python_files()
        if path != _SRC_ROOT / "inputs" / "profile.py"
        and "65_536" in path.read_text(encoding="utf-8")
    ]
    assert underscored == []
    bare = [
        str(path.relative_to(_SRC_ROOT.parent.parent))
        for path in _python_files()
        if path
        not in {
            _SRC_ROOT / "inputs" / "profile.py",
            _SRC_ROOT / "operator" / "archive.py",
        }
        and "65536" in path.read_text(encoding="utf-8")
    ]
    assert bare == []
