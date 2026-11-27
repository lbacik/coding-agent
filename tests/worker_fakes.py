"""Fake Worker startup ports for the ``worker.ports`` seam (issue #454).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4. Each
fake implements exactly one production port from
:mod:`coding_agent.worker.ports` and records the order its phases ran in a
shared list, so tests assert phase order without patching module globals:

- :class:`FakeSkillBundlePort` passes verification through
  :func:`support.run_inputs.make_run_inputs`, or returns a configured
  refusal, or raises a configured error.
- :class:`FakeCodeHostPort` passes the §18 checks with a configured
  identity, hands out a configured :class:`FakeStepTracker`, and resolves
  the identity mode from the tracker's owner lookup — or refuses/raises
  where configured. It can also delegate the checks to the real
  :func:`run_startup_checks` where a test drives live HTTP through a mock.
- :class:`FakeModelPort` hands out a configured model, delegates gateway
  resolution to the real rules while recording the lookup, and passes the
  Capability Assertion — or refuses/raises where configured.

:func:`make_worker_ports` builds a :class:`WorkerPorts` of fakes plus the
shared order list in one call.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from langchain_core.messages import AIMessage

import requests

from coding_agent.github.client import GitHubClient
from coding_agent.identity.startup import (
    Identity,
    StartupCheckResult,
    run_startup_checks,
)
from coding_agent.inputs.authorisation import IdentityMode, detect_identity_mode
from coding_agent.inputs.github import (
    fetch_repository_file as _fetch_repository_file,
)
from coding_agent.inputs.port import CodeHostPort
from coding_agent.provider.capability import (
    CapabilityAssertionResult,
    ProviderCapabilityReport,
)
from coding_agent.provider.pinned_model import (
    InvokableToolModel,
    PinnedModel,
    resolve_gateway_url as real_resolve_gateway_url,
)
from coding_agent.provider.price_table import PriceTable
from coding_agent.runtime.git import target_repository_url as real_target_repository_url
from coding_agent.skillbundle.deployment import (
    RunVerificationRefusal,
    VerifiedRunInputs,
)
from coding_agent.worker.ports import (
    CodeHostStartupPort,
    ModelPort,
    SkillBundlePort,
    WorkerPorts,
)
from coding_agent.workflow.configuration import LoadedWorkflowConfiguration
from support.fake_step_tracker import FakeStepTracker
from support.run_inputs import make_run_inputs

FAKE_IDENTITY = Identity(account_id=999, login="coding-agent")
"""The identity the fake GitHub checks report unless told otherwise."""


@dataclass
class FakeSkillBundlePort:
    """The fake :class:`SkillBundlePort`: pass, refuse or raise on demand."""

    order: list[str] = field(default_factory=list)
    """The shared startup order, appended with ``"skill-bundle"`` per call."""
    refusal: RunVerificationRefusal | None = None
    """Where set, verification returns this refusal instead of passing."""
    error: Exception | None = None
    """Where set, verification raises this error instead of passing."""
    calls: list[dict[str, object]] = field(default_factory=list)
    """The recorded verification kwargs, one entry per call."""

    def verify_run_inputs(
        self,
        *,
        config: LoadedWorkflowConfiguration,
        policy: Path,
        bundle_dir: Path,
        home: Path,
    ) -> VerifiedRunInputs | RunVerificationRefusal:
        """Verify through the loaded configuration, or refuse/raise."""
        self.order.append("skill-bundle")
        self.calls.append(
            {
                "config": config,
                "policy": policy,
                "bundle_dir": bundle_dir,
                "home": home,
            }
        )
        if self.error is not None:
            raise self.error
        if self.refusal is not None:
            return self.refusal
        return make_run_inputs(config)


@dataclass
class FakeCodeHostPort:
    """The fake :class:`CodeHostStartupPort`: pass, refuse or raise on demand."""

    order: list[str] = field(default_factory=list)
    """The shared startup order, appended with ``"github"`` per check run."""
    identity: Identity = FAKE_IDENTITY
    """The identity the fake checks report."""
    push_allowed: bool = True
    """The ``permissions.push`` value the fake checks report."""
    checks_error: Exception | None = None
    """Where set, the checks raise this error instead of passing."""
    real_checks: bool = False
    """Where set, the checks delegate to the real startup checks."""
    owner_error: Exception | None = None
    """Where set, the owner/mode lookup raises this error instead."""
    tracker: FakeStepTracker | None = None
    """The tracker the factory hands out; built per identity by default."""
    repository_url: str | None = None
    """The remote URL override; the real canonical URL by default."""
    tracker_calls: list[dict[str, object]] = field(default_factory=list)
    """The recorded tracker-factory kwargs, one entry per call."""

    def run_checks(
        self,
        token: str,
        owner: str,
        repo: str,
    ) -> StartupCheckResult:
        """Report the configured identity, or refuse/raise, or run live."""
        self.order.append("github")
        if self.checks_error is not None:
            raise self.checks_error
        if self.real_checks:
            return run_startup_checks(
                GitHubClient(token), token, owner, repo
            )
        return StartupCheckResult(
            identity=self.identity,
            push_allowed=self.push_allowed,
            rate_limit_remaining=4999,
            rate_limit_limit=5000,
        )

    def make_tracker(
        self,
        *,
        owner: str,
        repo: str,
        token: str,
        identity: Identity,
    ) -> CodeHostPort:
        """Hand out the configured tracker, recording the startup facts."""
        self.tracker_calls.append(
            {"owner": owner, "repo": repo, "token": token, "identity": identity}
        )
        if self.tracker is not None:
            return self.tracker
        return FakeStepTracker(
            agent_login=identity.login, agent_id=identity.account_id
        )

    def fetch_repository_file(
        self,
        *,
        token: str,
        owner: str,
        repo: str,
        rev: str,
        path: str,
    ) -> requests.Response:
        """GET one repository file at a revision; statuses come back to read."""
        return _fetch_repository_file(
            token=token, owner=owner, repo=repo, rev=rev, path=path
        )

    def resolve_identity_mode(
        self,
        tracker: CodeHostPort,
        identity: Identity,
    ) -> IdentityMode:
        """Resolve the owner through the tracker, then detect the mode."""
        if self.owner_error is not None:
            raise self.owner_error
        owner_id = tracker.fetch_repository_owner_id()
        return detect_identity_mode(identity.account_id, owner_id)

    def target_repository_url(self, owner: str, repo: str) -> str:
        """The override URL, or the real canonical URL for owner/repo."""
        if self.repository_url is not None:
            return self.repository_url
        return real_target_repository_url(owner, repo)


@dataclass
class FakeModelPort:
    """The fake :class:`ModelPort`: pass, refuse or raise on demand."""

    order: list[str] = field(default_factory=list)
    """The shared startup order, appended with ``"provider"`` per assertion."""
    model: Any = None
    """The model the factory hands out; required where startup builds one."""
    capability_error: Exception | None = None
    """Where set, the Capability Assertion raises this error instead."""
    gateway_error: Exception | None = None
    """Where set, gateway resolution raises this error instead of the rules."""
    resolve_calls: list[str] = field(default_factory=list)
    """The variable names gateway resolution looked up, in order."""
    build_calls: list[PinnedModel] = field(default_factory=list)
    """The pins the model factory built for, in order."""

    def resolve_gateway_url(
        self,
        model_id: str,
        name: str,
        env: Mapping[str, str],
    ) -> str:
        """Record the lookup, then apply the real gateway rules."""
        self.resolve_calls.append(name)
        if self.gateway_error is not None:
            raise self.gateway_error
        return real_resolve_gateway_url(model_id, name, env)

    def build_chat_model(
        self,
        pin: PinnedModel,
        *,
        api_key: str | None = None,
    ) -> InvokableToolModel:
        """Hand out the configured model, recording the pin."""
        _ = api_key
        self.build_calls.append(pin)
        if self.model is None:
            raise AssertionError("FakeModelPort.model is not set")
        return cast(InvokableToolModel, self.model)

    def assert_provider_capability(
        self,
        pin: PinnedModel,
        model: InvokableToolModel,
        price_table: PriceTable,
        *,
        max_tokens: int | None,
    ) -> CapabilityAssertionResult:
        """Pass the Capability Assertion, or raise the configured error."""
        _ = (model, price_table, max_tokens)
        self.order.append("provider")
        if self.capability_error is not None:
            raise self.capability_error
        return CapabilityAssertionResult(
            pin=pin,
            response=AIMessage(content=""),
            report=ProviderCapabilityReport(
                pin_key=pin.key,
                ran_at="2026-11-18T00:00:00Z",
                checks=(),
            ),
        )


@dataclass
class WorkerFakes:
    """The three fakes, the shared order and the assembled ports."""

    github: FakeCodeHostPort
    """The fake code-host port."""
    model: FakeModelPort
    """The fake model port."""
    skills: FakeSkillBundlePort
    """The fake Skill Bundle port."""
    order: list[str]
    """The shared startup order across all three fakes."""
    ports: WorkerPorts
    """The ports value to pass to ``start`` or ``run_worker``."""


def make_worker_ports(
    *,
    model: Any = None,
    tracker: FakeStepTracker | None = None,
    skill_refusal: RunVerificationRefusal | None = None,
    skill_error: Exception | None = None,
    github_error: Exception | None = None,
    provider_error: Exception | None = None,
    push_allowed: bool = True,
    owner_error: Exception | None = None,
    repository_url: str | None = None,
    real_github_checks: bool = False,
    identity: Identity | None = None,
) -> WorkerFakes:
    """Build a :class:`WorkerPorts` of fakes sharing one order list.

    ``model`` is the chat model the fake factory hands out; ``tracker`` is
    the step tracker it hands out. The ``*_error`` knobs raise at the matching
    check; ``skill_refusal`` refuses verification; ``push_allowed=False``
    refuses at ``github-checks``. ``repository_url`` points the Worker at a
    local bare remote for offline end-to-end runs.
    """
    order: list[str] = []
    github = FakeCodeHostPort(
        order=order,
        identity=identity if identity is not None else FAKE_IDENTITY,
        push_allowed=push_allowed,
        checks_error=github_error,
        real_checks=real_github_checks,
        owner_error=owner_error,
        tracker=tracker,
        repository_url=repository_url,
    )
    fake_model = FakeModelPort(order=order, model=model, capability_error=provider_error)
    skills = FakeSkillBundlePort(
        order=order, refusal=skill_refusal, error=skill_error
    )
    return WorkerFakes(
        github=github,
        model=fake_model,
        skills=skills,
        order=order,
        ports=WorkerPorts(github=github, model=fake_model, skills=skills),
    )


__all__ = [
    "FAKE_IDENTITY",
    "FakeCodeHostPort",
    "FakeModelPort",
    "FakeSkillBundlePort",
    "WorkerFakes",
    "make_worker_ports",
]
