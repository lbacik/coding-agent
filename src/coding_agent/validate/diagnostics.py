from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from coding_agent.profile.schema import ProjectProfile
from coding_agent.profile.substitution import render_command
from coding_agent.validate.harness import CommandContext
from coding_agent.validate.junit import JUnitResult, MalformedJUnitReport, parse_junit_xml


TargetedClassification = Literal[
    "passed",
    "assertion_failure",
    "infrastructure_failure",
    "no_tests_executed",
    "missing_evidence",
    "invalid_evidence",
]


class DiagnosticArtifactStore(Protocol):
    """The small storage boundary needed by targeted diagnostics.

    The implementation is deliberately compatible with the existing result
    artifact stores without making the validation package depend on the
    implement package.
    """

    def store(self, artifact_id: str, content: str) -> None: ...


@dataclass(frozen=True)
class DiagnosticArtifact:
    """A redacted diagnostic retained outside the inline model response."""

    artifact_id: str
    byte_size: int
    available_range: tuple[int, int]

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "byte_size": self.byte_size,
            "available_range": {"start": self.available_range[0], "end": self.available_range[1]},
        }


@dataclass(frozen=True)
class TargetedDiagnostic:
    """The advisory result returned by one `test_targeted` invocation.

    This is intentionally separate from `RawCommandResult` and
    `ValidationEvidence`: the model may use it to decide what to edit, but it
    can never become evidence for the Publication Gate.
    """

    invocation_id: str
    command: str
    working_directory: str
    requested_path: str
    report_path: str
    exit_status: int
    classification: TargetedClassification
    junit: JUnitResult | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    artifacts: tuple[DiagnosticArtifact, ...] = ()
    signature: str | None = None
    suppressed: bool = False
    stop_loop: bool = False
    report_present: bool = False
    suppression_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        junit: dict[str, object] | None = None
        if self.junit is not None:
            junit = {
                "executed_test_count": self.junit.executed,
                "failure_identifiers": sorted(self.junit.failure_ids),
            }
        return {
            "invocation_id": self.invocation_id,
            "command": self.command,
            "working_directory": self.working_directory,
            "requested_path": self.requested_path,
            "report_path": self.report_path,
            "exit_status": self.exit_status,
            # Kept as a compact compatibility summary for human inspection;
            # all machine-readable fields above remain the contract.
            "summary": f"command={self.command!r} exit={self.exit_status} classification={self.classification}",
            "classification": self.classification,
            "junit": junit,
            "stdout": {"excerpt": self.stdout, "truncated": self.stdout_truncated},
            "stderr": {"excerpt": self.stderr, "truncated": self.stderr_truncated},
            "artifacts": [artifact.as_dict() for artifact in self.artifacts],
            "signature": self.signature,
            "suppressed": self.suppressed,
            "stop_loop": self.stop_loop,
            "report_present": self.report_present,
            "suppression_reason": self.suppression_reason,
        }

    def render(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True)


@dataclass(frozen=True)
class _WorkspaceState:
    files: dict[str, str]


_INFRASTRUCTURE_MARKERS = (
    "command not found",
    "not found",
    "no such file or directory",
    "cannot execute",
    "permission denied",
    "executable file not found",
    "module not found",
    "class not found",
    "connection refused",
    "could not resolve host",
    "failed to connect",
)
_CONFIG_PATH_MARKERS = (
    "project-profile",
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "composer.json",
    "composer.lock",
    "dockerfile",
    ".env",
)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ISO_TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+(?:Z| UTC)?\b")
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)
_TEMP_PATH = re.compile(r"(?<!\w)(?:/[A-Za-z0-9_.-]+){2,}(?:/[A-Za-z0-9_.-]+)?")
_HEX_ADDRESS = re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE)


def redact_diagnostic(text: str, redactions: Mapping[str, str] | None = None) -> str:
    """Redact registered secret values before any excerpt or artifact exists."""

    result = text
    for name, secret in (redactions or {}).items():
        if secret:
            result = result.replace(secret, f"«redacted:{name}»")
    return result


def _normalise(text: str) -> str:
    normalised = _ANSI_ESCAPE.sub("", text)
    normalised = _ISO_TIMESTAMP.sub("<timestamp>", normalised)
    normalised = _UUID.sub("<uuid>", normalised)
    normalised = _TEMP_PATH.sub("<path>", normalised)
    normalised = _HEX_ADDRESS.sub("<address>", normalised)
    return " ".join(normalised.split())


def _looks_like_infrastructure_failure(executed_exit_status: int, stdout: str, stderr: str) -> bool:
    if executed_exit_status == 0:
        return False
    diagnostic = f"{stdout}\n{stderr}".lower()
    return any(marker in diagnostic for marker in _INFRASTRUCTURE_MARKERS)


def _workspace_state(root: Path, *, exclude: Path | None = None) -> _WorkspaceState:
    files: dict[str, str] = {}
    if not root.exists():
        return _WorkspaceState(files)
    excluded = exclude.resolve() if exclude is not None else None
    if excluded == root.resolve():
        # A small unit-test/in-process caller may use one directory for both
        # workspace and evidence. Only the adapter-owned targeted subtree is
        # then excluded; excluding the whole root would hide real edits.
        excluded = excluded / "targeted"
    try:
        candidates = root.rglob("*")
    except OSError:
        return _WorkspaceState(files)
    for path in candidates:
        if not path.is_file() or ".git" in path.parts:
            continue
        if excluded is not None and (path == excluded or excluded in path.parents):
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            files[path.relative_to(root).as_posix()] = digest
        except (OSError, ValueError):
            continue
    return _WorkspaceState(files)


def _changed_paths(previous: _WorkspaceState, current: _WorkspaceState) -> set[str]:
    paths = set(previous.files) | set(current.files)
    return {path for path in paths if previous.files.get(path) != current.files.get(path)}


class TargetedTestAdapter:
    """Run targeted tests and turn raw process output into bounded diagnostics."""

    def __init__(
        self,
        profile: ProjectProfile,
        context: CommandContext,
        *,
        artifact_store: DiagnosticArtifactStore,
        inline_limit: int,
        attempt_id: str = "attempt",
        redactions: Mapping[str, str] | None = None,
    ) -> None:
        if inline_limit < 1:
            raise ValueError("inline_limit must be positive")
        self._profile = profile
        self._context = context
        self._artifact_store = artifact_store
        self._inline_limit = inline_limit
        self._attempt_id = attempt_id
        self._redactions = dict(redactions or {})
        self._invocation_number = 0
        self._workspace_root = context.workspace_root or context.working_directory
        self._previous_state = _workspace_state(
            self._workspace_root, exclude=context.evidence_dir
        )
        self._manual_changes: set[str] = set()
        self._infra_signature: str | None = None
        self._infra_retry_used = False
        self._assertion_signature: str | None = None
        self._last_request_key: tuple[str, str] | None = None
        self._last_classification: TargetedClassification | None = None
        self._last_exit_status = 1

    def record_relevant_state_change(self, kind: str) -> None:
        """Record a non-file state transition visible to the adapter."""

        self._manual_changes.add(kind)

    def record_bootstrap_success(self) -> None:
        self.record_relevant_state_change("bootstrap")

    def record_dependency_change(self) -> None:
        self.record_relevant_state_change("dependency")

    def record_test_configuration_change(self) -> None:
        self.record_relevant_state_change("configuration")

    def record_declared_service_repair(self) -> None:
        self.record_relevant_state_change("service")

    def _scope(self, invocation_number: int) -> tuple[str, Path]:
        invocation_id = f"{self._attempt_id}/targeted/{invocation_number}"
        return invocation_id, self._context.evidence_dir / "targeted" / str(invocation_number)

    @staticmethod
    def _report_path(context: CommandContext, evidence_dir: Path, profile: ProjectProfile) -> Path:
        rendered = Path(render_command(profile.evidence.test_targeted, evidence_dir=evidence_dir))
        return rendered if rendered.is_absolute() else context.working_directory / rendered

    def _artifact_excerpt(
        self, text: str, *, artifact_id: str, excerpt_limit: int
    ) -> tuple[str, bool, DiagnosticArtifact | None]:
        byte_size = len(text.encode("utf-8"))
        if byte_size <= excerpt_limit:
            return text, False, None

        self._artifact_store.store(artifact_id, text)
        # Size the excerpt by UTF-8 bytes, not Python code points. The
        # retained artifact metadata reports the same byte range.
        head_bytes = max(1, excerpt_limit // 2)
        tail_bytes = max(1, excerpt_limit - head_bytes)

        def take_prefix(value: str, byte_limit: int) -> str:
            encoded = value.encode("utf-8")
            return encoded[:byte_limit].decode("utf-8", errors="ignore")

        def take_suffix(value: str, byte_limit: int) -> str:
            encoded = value.encode("utf-8")
            return encoded[-byte_limit:].decode("utf-8", errors="ignore")

        head_text = take_prefix(text, head_bytes)
        tail_text = take_suffix(text, tail_bytes)
        # Never cut a redaction marker in half: doing so would hide the fact
        # that a secret was removed from the inline diagnostic even though the
        # stored artifact is safe.
        for marker in re.findall(r"«redacted:[^»]+»", text):
            marker_start = text.find(marker)
            marker_end = marker_start + len(marker)
            if marker_start <= len(head_text) < marker_end:
                head_text = text[:marker_end]
            tail_start = len(text) - len(tail_text)
            if tail_start < marker_end and marker_start < tail_start:
                tail_text = text[marker_start:]
        omitted = max(0, len(text) - len(head_text) - len(tail_text))
        excerpt = f"{head_text}\n...[truncated {omitted} characters]...\n{tail_text}"
        artifact = DiagnosticArtifact(artifact_id, byte_size, (0, byte_size))
        return excerpt, True, artifact

    def _relevant_file_change(self, changed: set[str], classification: TargetedClassification) -> bool:
        if not changed:
            return False
        if classification == "assertion_failure":
            return True
        return any(
            any(marker in path.lower() for marker in _CONFIG_PATH_MARKERS) for path in changed
        )

    def _signature(
        self, classification: TargetedClassification, command: str, stdout: str, stderr: str, junit: JUnitResult | None
    ) -> str | None:
        if classification not in {"infrastructure_failure", "assertion_failure"}:
            return None
        failure_ids = ",".join(sorted(junit.failure_ids)) if junit is not None else ""
        # The report path is intentionally invocation-scoped. Remove only
        # that generated segment from the signature so equivalent failures
        # across invocations can still be recognised as equivalent.
        signature_command = self._signature_command(command)
        material = "|".join(
            (
                classification,
                signature_command,
                str(self._context.working_directory),
                failure_ids,
                _normalise(f"{stdout}\n{stderr}"),
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _signature_command(command: str) -> str:
        return re.sub(r"targeted[/\\][0-9]+", "targeted/<invocation>", command)

    def _suppressed_payload(
        self,
        *,
        invocation_id: str,
        command: str,
        report_path: Path,
        requested_path: str,
        classification: TargetedClassification,
        signature: str,
        stop_loop: bool,
        reason: str,
    ) -> str:
        return TargetedDiagnostic(
            invocation_id=invocation_id,
            command=command,
            working_directory=str(self._context.working_directory),
            requested_path=requested_path,
            report_path=str(report_path),
            exit_status=self._last_exit_status,
            classification=classification,
            junit=None,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            signature=signature,
            suppressed=not stop_loop,
            stop_loop=stop_loop,
            report_present=False,
            suppression_reason=reason,
        ).render()

    def run(self, path: str, *, redactions: Mapping[str, str] | None = None) -> str:
        self._invocation_number += 1
        invocation_id, invocation_evidence_dir = self._scope(self._invocation_number)
        invocation_evidence_dir.mkdir(parents=True, exist_ok=True)
        command = render_command(
            self._profile.test_targeted,
            evidence_dir=invocation_evidence_dir,
            path=path,
        )
        report_path = self._report_path(self._context, invocation_evidence_dir, self._profile)
        try:
            report_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # The command will still run, and the resulting inability to
            # establish current evidence is reported as missing/invalid.
            pass

        current_state = _workspace_state(self._workspace_root, exclude=self._context.evidence_dir)
        changed = _changed_paths(self._previous_state, current_state)
        relevant = self._relevant_file_change(changed, self._last_classification or "missing_evidence") or bool(
            self._manual_changes
        )
        request_key = (self._signature_command(command), path)
        if (
            not relevant
            and self._last_request_key == request_key
            and self._last_classification == "infrastructure_failure"
            and self._infra_signature is not None
        ):
            self._manual_changes.clear()
            self._previous_state = current_state
            if self._infra_retry_used:
                return self._suppressed_payload(
                    invocation_id=invocation_id,
                    command=command,
                    report_path=report_path,
                    requested_path=path,
                    classification="infrastructure_failure",
                    signature=self._infra_signature,
                    stop_loop=True,
                    reason="the same infrastructure signature returned after its permitted retry",
                )
            return self._suppressed_payload(
                invocation_id=invocation_id,
                command=command,
                report_path=report_path,
                requested_path=path,
                classification="infrastructure_failure",
                signature=self._infra_signature,
                stop_loop=False,
                reason=(
                    "unchanged infrastructure signature suppressed; change bootstrap, dependencies "
                    "or lockfiles, test/profile configuration, or a declared service before retrying"
                ),
            )
        if (
            not relevant
            and self._last_request_key == request_key
            and self._last_classification == "assertion_failure"
            and self._assertion_signature is not None
        ):
            self._manual_changes.clear()
            self._previous_state = current_state
            return self._suppressed_payload(
                invocation_id=invocation_id,
                command=command,
                report_path=report_path,
                requested_path=path,
                classification="assertion_failure",
                signature=self._assertion_signature,
                stop_loop=False,
                reason="unchanged assertion failure suppressed; edit source or test before retrying",
            )

        executed = self._context.runner.run(command, cwd=self._context.working_directory)
        effective_redactions = {**self._redactions, **(redactions or {})}
        stdout = redact_diagnostic(executed.stdout, effective_redactions)
        stderr = redact_diagnostic(executed.stderr, effective_redactions)
        report_text: str | None = None
        report_present = report_path.is_file()
        if report_present:
            try:
                report_text = redact_diagnostic(
                    report_path.read_text(encoding="utf-8"), effective_redactions
                )
            except (OSError, UnicodeDecodeError):
                report_text = None

        junit: JUnitResult | None = None
        if not report_present:
            classification: TargetedClassification = (
                "infrastructure_failure"
                if _looks_like_infrastructure_failure(executed.exit_code, stdout, stderr)
                else "missing_evidence"
            )
        elif report_text is None:
            classification = "invalid_evidence"
        else:
            try:
                junit = parse_junit_xml(report_text)
            except MalformedJUnitReport:
                classification = "invalid_evidence"
            else:
                if junit.executed == 0:
                    classification = "no_tests_executed"
                elif junit.failure_ids:
                    classification = "assertion_failure"
                elif executed.exit_code != 0:
                    # A valid report proves that tests ran, but a failing
                    # command without named failures is not a pass (ADR 0004).
                    classification = "infrastructure_failure"
                else:
                    # A valid, executed report is the evidence state. Exit
                    # status alone cannot turn it into an infrastructure or
                    # no-test result.
                    classification = "passed"

        current_state = _workspace_state(self._workspace_root, exclude=self._context.evidence_dir)
        changed = _changed_paths(self._previous_state, current_state)
        relevant = self._relevant_file_change(changed, classification) or bool(self._manual_changes)
        self._manual_changes.clear()
        signature = self._signature(classification, command, stdout, stderr, junit)
        suppressed = False
        stop_loop = False
        suppression_reason: str | None = None
        if classification == "infrastructure_failure" and signature is not None:
            if signature == self._infra_signature:
                if relevant and not self._infra_retry_used:
                    self._infra_retry_used = True
                elif self._infra_retry_used:
                    stop_loop = True
                else:
                    suppressed = True
                    suppression_reason = (
                        "unchanged infrastructure signature suppressed; change bootstrap, "
                        "dependencies or lockfiles, test/profile configuration, or a declared "
                        "service before retrying"
                    )
            else:
                self._infra_signature = signature
                self._infra_retry_used = False
        elif classification == "assertion_failure" and signature is not None:
            if signature == self._assertion_signature and not relevant:
                suppressed = True
                suppression_reason = "unchanged assertion failure suppressed; edit source or test before retrying"
            self._assertion_signature = signature

        artifacts: list[DiagnosticArtifact] = []
        # Leave room for the required structured metadata in the model
        # response. The full redacted stream/report remains range-readable in
        # the artifact store, while the inline payload stays below the outer
        # tool-result cap in normal operation.
        excerpt_limit = max(16, self._inline_limit // 4)
        if suppressed:
            # The first equivalent result is the diagnostic; later calls only
            # carry the suppression reason and signature, not a second copy of
            # the secret-bearing streams or report.
            stdout_excerpt = stderr_excerpt = ""
            stdout_truncated = stderr_truncated = False
            junit = None
        else:
            stdout_excerpt, stdout_truncated, artifact = self._artifact_excerpt(
                stdout, artifact_id=f"{invocation_id}/stdout", excerpt_limit=excerpt_limit
            )
            if artifact is not None:
                artifacts.append(artifact)
            stderr_excerpt, stderr_truncated, artifact = self._artifact_excerpt(
                stderr, artifact_id=f"{invocation_id}/stderr", excerpt_limit=excerpt_limit
            )
            if artifact is not None:
                artifacts.append(artifact)
            if report_text is not None:
                _report_excerpt, _report_truncated, artifact = self._artifact_excerpt(
                    report_text, artifact_id=f"{invocation_id}/report", excerpt_limit=excerpt_limit
                )
                if artifact is not None:
                    artifacts.append(artifact)

        self._last_request_key = request_key
        self._last_classification = classification
        self._last_exit_status = executed.exit_code
        self._previous_state = current_state
        return TargetedDiagnostic(
            invocation_id=invocation_id,
            command=command,
            working_directory=str(self._context.working_directory),
            requested_path=path,
            report_path=str(report_path),
            exit_status=executed.exit_code,
            classification=classification,
            junit=junit,
            stdout=stdout_excerpt,
            stderr=stderr_excerpt,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            artifacts=tuple(artifacts),
            signature=signature,
            suppressed=suppressed,
            stop_loop=stop_loop,
            report_present=report_present,
            suppression_reason=suppression_reason,
        ).render()


def diagnostic_requests_loop_stop(content: str) -> bool:
    """Recognise the adapter's explicit no-progress signal in a tool result."""

    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(payload, dict) and payload.get("stop_loop") is True
