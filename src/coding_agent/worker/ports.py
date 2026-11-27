"""Worker startup ports: the startup module's external collaborators.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4 (startup in
order). :func:`coding_agent.worker.startup.start` performs no external work
itself: every collaborator behind the three ports below is injected through
:class:`WorkerPorts`, with :func:`production_ports` supplying the production
adapters. Tests pass the fakes from ``tests/worker_fakes.py`` instead of
patching module globals.

- :class:`CodeHostStartupPort`: the §18 non-mutating checks, the tracker
  factory and the repository-owner / identity-mode lookup, plus the
  canonical remote URL derived from the same owner/repo coordinates. Git
  *operations* (the mirror, the workspace) stay out of scope. The port owns
  the code-host transport: callers pass the credential and the Target
  Repository coordinates, never a client.
- :class:`ModelPort`: resolving the gateway address, building the chat
  model, and the Provider Capability Assertion. These three always travel
  together.
- :class:`SkillBundlePort`: the Skill Bundle run verification, returning the
  complete verified-run-inputs value or a refusal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests

from coding_agent.identity.startup import (
    Identity,
    StartupCheckResult,
    run_startup_checks_for_token,
)
from coding_agent.inputs.authorisation import IdentityMode, detect_identity_mode
from coding_agent.inputs.github import (
    fetch_repository_file as _fetch_repository_file,
)
from coding_agent.inputs.github import (
    github_step_tracker as _github_step_tracker,
)
from coding_agent.inputs.port import StepTrackerPort
from coding_agent.provider.capability import (
    CapabilityAssertionResult,
    assert_provider_capability,
)
from coding_agent.provider.pinned_model import (
    InvokableToolModel,
    PinnedModel,
    build_chat_model,
    resolve_gateway_url,
)
from coding_agent.provider.price_table import PriceTable
from coding_agent.runtime.git import target_repository_url
from coding_agent.skillbundle.deployment import (
    RunVerificationRefusal,
    VerifiedRunInputs,
    verify_run_inputs,
)
from coding_agent.workflow.configuration import LoadedWorkflowConfiguration


class CodeHostStartupPort(Protocol):
    """The §18 non-mutating checks and everything derived from owner/repo."""

    def run_checks(
        self,
        token: str,
        owner: str,
        repo: str,
    ) -> StartupCheckResult:
        """Run the non-mutating code-host startup checks (§18)."""
        ...

    def make_tracker(
        self,
        *,
        owner: str,
        repo: str,
        token: str,
        identity: Identity,
    ) -> StepTrackerPort:
        """Build the Worker's step tracker for the Agent Identity."""
        ...

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
        ...

    def resolve_identity_mode(
        self,
        tracker: StepTrackerPort,
        identity: Identity,
    ) -> IdentityMode:
        """Resolve the repository owner and detect the identity mode (§18)."""
        ...

    def target_repository_url(self, owner: str, repo: str) -> str:
        """The canonical credential-free remote URL for owner/repo (§18)."""
        ...


class ModelPort(Protocol):
    """The gateway address, the chat model and the Capability Assertion."""

    def resolve_gateway_url(
        self,
        model_id: str,
        name: str,
        env: Mapping[str, str],
    ) -> str:
        """Resolve an env-sourced gateway address to its canonical URL."""
        ...

    def build_chat_model(
        self,
        pin: PinnedModel,
        *,
        api_key: str | None,
    ) -> InvokableToolModel:
        """Build the chat model for the Pinned Model (§2.1)."""
        ...

    def assert_provider_capability(
        self,
        pin: PinnedModel,
        model: InvokableToolModel,
        price_table: PriceTable,
        *,
        max_tokens: int | None,
    ) -> CapabilityAssertionResult:
        """Run the Provider Capability Assertion for the pin (§2.9).

        The result carries the Provider Capability Report; a refusal
        carries the report built up to it. The caller persists the
        report in the operator store on success and on refusal.
        """
        ...


class SkillBundlePort(Protocol):
    """The Skill Bundle run verification."""

    def verify_run_inputs(
        self,
        *,
        config: LoadedWorkflowConfiguration,
        policy: Path,
        bundle_dir: Path,
        home: Path,
    ) -> VerifiedRunInputs | RunVerificationRefusal:
        """Verify the prepared deployment and build the run inputs."""
        ...


@dataclass(frozen=True)
class WorkerPorts:
    """The startup module's external collaborators, as three ports."""

    github: CodeHostStartupPort
    """The §18 checks, the tracker factory and the owner/mode lookup."""
    model: ModelPort
    """The gateway address, the chat model and the Capability Assertion."""
    skills: SkillBundlePort
    """The Skill Bundle run verification."""


@dataclass(frozen=True)
class ProductionCodeHostPort:
    """The production :class:`CodeHostStartupPort`: live code host, no seam."""

    def run_checks(
        self,
        token: str,
        owner: str,
        repo: str,
    ) -> StartupCheckResult:
        """Run the non-mutating code-host startup checks (§18)."""
        return run_startup_checks_for_token(token, owner, repo)

    def make_tracker(
        self,
        *,
        owner: str,
        repo: str,
        token: str,
        identity: Identity,
    ) -> StepTrackerPort:
        """Build the production step tracker for the Agent Identity."""
        return _github_step_tracker(
            owner=owner,
            repo=repo,
            token=token,
            agent_login=identity.login,
            agent_id=identity.account_id,
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
        tracker: StepTrackerPort,
        identity: Identity,
    ) -> IdentityMode:
        """Resolve the repository owner and detect the identity mode (§18)."""
        owner_id = tracker.fetch_repository_owner_id()
        return detect_identity_mode(identity.account_id, owner_id)

    def target_repository_url(self, owner: str, repo: str) -> str:
        """The canonical credential-free remote URL for owner/repo (§18)."""
        return target_repository_url(owner, repo)


@dataclass(frozen=True)
class ProductionModelPort:
    """The production :class:`ModelPort`: real gateway rules and live calls."""

    def resolve_gateway_url(
        self,
        model_id: str,
        name: str,
        env: Mapping[str, str],
    ) -> str:
        """Resolve an env-sourced gateway address to its canonical URL."""
        return resolve_gateway_url(model_id, name, env)

    def build_chat_model(
        self,
        pin: PinnedModel,
        *,
        api_key: str | None,
    ) -> InvokableToolModel:
        """Build the chat model for the Pinned Model (§2.1)."""
        return build_chat_model(pin, api_key=api_key)

    def assert_provider_capability(
        self,
        pin: PinnedModel,
        model: InvokableToolModel,
        price_table: PriceTable,
        *,
        max_tokens: int | None,
    ) -> CapabilityAssertionResult:
        """Run the Provider Capability Assertion for the pin (§2.9)."""
        return assert_provider_capability(
            pin, model, price_table, max_tokens=max_tokens
        )


@dataclass(frozen=True)
class ProductionSkillBundlePort:
    """The production :class:`SkillBundlePort`: the real offline verification."""

    def verify_run_inputs(
        self,
        *,
        config: LoadedWorkflowConfiguration,
        policy: Path,
        bundle_dir: Path,
        home: Path,
    ) -> VerifiedRunInputs | RunVerificationRefusal:
        """Verify the prepared deployment and build the run inputs."""
        return verify_run_inputs(
            config=config, policy=policy, bundle_dir=bundle_dir, home=home
        )


def production_ports() -> WorkerPorts:
    """The production adapters: what ``agent run`` uses (§4)."""
    return WorkerPorts(
        github=ProductionCodeHostPort(),
        model=ProductionModelPort(),
        skills=ProductionSkillBundlePort(),
    )


__all__ = [
    "CodeHostStartupPort",
    "ModelPort",
    "ProductionCodeHostPort",
    "ProductionModelPort",
    "ProductionSkillBundlePort",
    "SkillBundlePort",
    "WorkerPorts",
    "production_ports",
]
