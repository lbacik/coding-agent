"""The Provider Capability Assertion: one small live check at startup.

Contract: ``docs/contract/mvp-runtime-contract.md`` §18 ("Preflight and
startup checks") and §2.1 (the Pinned Model and its prices come from the
Effective Configuration), and
[ADR 0009](../../../docs/adr/0009-provider-capability-is-established-by-attempting.md)
(capability is established by attempting, never by reading a declaration).

MVP row ids: L1-4, L1-4a. Step-workflow row id: L1-4b.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

from langchain_core.exceptions import ModelInvalidRequestError
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool, tool

from coding_agent.provider.observed_identity import (
    EffortCheck,
    check_direct_pin,
    check_gateway_error_exc,
    check_reported_effort,
)
from coding_agent.provider.pinned_model import InvokableToolModel, PinnedModel
from coding_agent.provider.price_table import PriceTable, price_for
from coding_agent.provider.retry import invoke_with_retry
from coding_agent.runtime.redaction import Redactable, registry_from

_ASK_PROBE_PROMPT = "Call the capability_probe tool with the city set to paris."
"""The asking prompt of the gating tool probe.

Every work node binds its tools unforced and asks the model to call them,
so the gating probe does the same: the probe tool is bound with no forced
``tool_choice`` (``auto``) and the prompt asks for a call. Only a call to
the probe tool whose arguments match its schema passes; a text answer, a
call to another tool, or arguments that do not match refuse the pin.
"""

_PROBE_PROMPT = "Reply with the single word OK. Do not call any tool."
"""The contrary prompt of the diagnostic forced probe.

Kept from the MVP assertion: it proves the opposite direction — faced with
a forced ``tool_choice`` naming the probe tool and a prompt asking for no
tool call, the model either calls the probe tool or it does not. The
outcome is recorded but never refuses startup by itself.
"""

_FORCED_TOOL_CHOICE = "capability_probe"
"""The probe tool, named as the forced ``tool_choice`` of the diagnostic probe.

Only the diagnostic forced probe passes a non-``None`` ``tool_choice``
anywhere in ``src/``. A text answer means the forced choice was silently
downgraded to ``auto``; a refusal (a Meta model behind a gateway refuses
any forced choice with a 400) is recorded with the provider's error text.
Neither refuses the pin: the gating tool leg is the unforced ``auto``
probe above.
"""

UNHONOURED_EFFORT = "not-an-effort-level"
"""The effort value the reach probe sends: no provider accepts it.

ADR 0009's cheapest probe: a deliberately invalid effort proves the
parameter reaches the provider at all, because a provider that receives it
refuses the request naming the parameter. An effort pin silently dropped on
the way to the wire is otherwise invisible — the run looks correct and is
not the run the Attempt recorded. It runs only where the probe response
reports no effort: where the response reports the effort it ran at, that
own evidence is the reach proof instead.
"""

AUTO_PROBE_MAX_TRIES = 3
"""Total tries for the gating auto probe where arguments do not match.

Model output is non-deterministic, so a capable pin can be refused by a
single bad sample: only a call to the probe tool whose arguments do not
match its schema is retried, up to this many tries in total. A constant
owned by the code, not a configuration key: an operator must not be able
to pass an incapable model by raising it.

Every other gating failure — a text answer, a call to another tool, a
provider or transport error, a response that is not an ``AIMessage`` —
still refuses immediately, as decided in #474.
"""

_ARGS_EVIDENCE_CAP = 60
"""Per-try cap on recorded probe-call arguments (JSON, truncated).

Three truncated tries plus framing stay well under
:data:`REPORT_EVIDENCE_CAP`, so the refused entry needs no further cap.
"""

REPORT_EVIDENCE_CAP = 300
"""The cap on one report entry's evidence text.

Mirrors the Contract Event text limit
(``coding_agent.operator.events.MAX_EVENT_TEXT_LEN``) without depending on
the operator package from the provider layer: evidence is redacted through
the existing secret registry and capped at write time, so a secret in a
provider error never reaches ``agentctl`` output.
"""


@tool
def capability_probe(city: str) -> str:
    """A harmless tool the Provider Capability Assertion asks the Pinned Model to call."""
    return f"probe received {city!r}"


class ProviderCapabilityRefused(Exception):
    """The Provider Capability Assertion failed.

    No Attempt is opened, and nothing GitHub-facing is touched (ADR 0009
    part 4) — the caller's job is to turn this into a nonzero process exit.
    The report built up to the refusal travels on ``report``, so the caller
    always has it: the Worker persists it before recording the Halt, and
    ``agentctl status`` serves it offline.
    """

    def __init__(
        self, message: str, *, report: ProviderCapabilityReport | None = None
    ) -> None:
        super().__init__(message)
        self.report = report


CHECK_PRICE = "price"
CHECK_TOOL_CALL_AUTO = "tool-call-auto"
CHECK_TOOL_CALL_FORCED = "tool-call-forced"
CHECK_USAGE = "usage"
CHECK_IDENTITY = "identity"
CHECK_EFFORT = "effort"

OUTCOME_PASSED = "passed"
OUTCOME_REFUSED = "refused"
OUTCOME_NOT_RUN = "not-run"
OUTCOME_TEXT_ANSWER = "text-answer"

VIA_REPORTED = "reported"
VIA_INVALID_EFFORT_REFUSED = "invalid-effort-refused"


@dataclass(frozen=True)
class CapabilityCheck:
    """One check's recorded result within a Provider Capability Report.

    ``check`` names the check in the order it ran (``price``,
    ``tool-call-auto``, ``tool-call-forced``, ``usage``, ``identity``,
    ``effort``); ``gating`` is ``False`` for ``tool-call-forced`` only;
    ``outcome`` is ``passed`` / ``refused`` / ``not-run``, plus
    ``text-answer`` for the forced probe. ``evidence`` is the short,
    redacted, capped string behind the outcome. The ``effort`` entry also
    says which kind of evidence was used (``via``): ``reported`` with the
    ``reported`` and ``pinned`` values, or ``invalid-effort-refused``.
    """

    check: str
    gating: bool
    outcome: str
    evidence: str
    via: str | None = None
    reported: str | None = None
    pinned: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The check as the ``status.provider_capability`` JSON carries it."""
        record: dict[str, Any] = {
            "check": self.check,
            "gating": self.gating,
            "outcome": self.outcome,
            "evidence": self.evidence,
        }
        if self.via is not None:
            record["via"] = self.via
        if self.reported is not None:
            record["reported"] = self.reported
        if self.pinned is not None:
            record["pinned"] = self.pinned
        return record

    @classmethod
    def from_dict(cls, record: Mapping[str, Any]) -> CapabilityCheck:
        """Rebuild a check from its stored JSON (unknown fields ignored)."""
        return cls(
            check=str(record.get("check", "")),
            gating=bool(record.get("gating", True)),
            outcome=str(record.get("outcome", OUTCOME_NOT_RUN)),
            evidence=str(record.get("evidence", "")),
            via=(
                str(record["via"])
                if isinstance(record.get("via"), str)
                else None
            ),
            reported=(
                str(record["reported"])
                if isinstance(record.get("reported"), str)
                else None
            ),
            pinned=(
                str(record["pinned"])
                if isinstance(record.get("pinned"), str)
                else None
            ),
        )


@dataclass(frozen=True)
class ProviderCapabilityReport:
    """The recorded result of each check of the last assertion run.

    One report per run of the assertion: the pin key, when the assertion
    ran, and one entry per check in the order they ran. The Worker persists
    the latest report in the operator store on success and on refusal, and
    ``agentctl status`` returns it as ``status.provider_capability`` —
    ``None`` where no assertion has run yet.
    """

    pin_key: str
    ran_at: str
    checks: tuple[CapabilityCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        """The report as the ``status.provider_capability`` JSON carries it."""
        return {
            "pin_key": self.pin_key,
            "ran_at": self.ran_at,
            "checks": [check.to_dict() for check in self.checks],
        }

    @classmethod
    def from_dict(cls, record: Mapping[str, Any]) -> ProviderCapabilityReport:
        """Rebuild a report from its stored JSON (unknown fields ignored)."""
        raw_checks = record.get("checks")
        checks: list[CapabilityCheck] = []
        if isinstance(raw_checks, list):
            for raw in raw_checks:
                if isinstance(raw, Mapping):
                    checks.append(CapabilityCheck.from_dict(raw))
        return cls(
            pin_key=str(record.get("pin_key", "")),
            ran_at=str(record.get("ran_at", "")),
            checks=tuple(checks),
        )


def redact_report(
    record: Mapping[str, Any], registry: Redactable = None
) -> dict[str, Any]:
    """Redact and cap one report's evidence texts, returning plain JSON.

    Every check's ``evidence`` is redacted through the secret registry and
    capped at :data:`REPORT_EVIDENCE_CAP` characters, like every Contract
    Event text. The operator store redacts at write time and the
    ``provider-check`` command redacts before printing, so a secret in a
    provider error never reaches ``agentctl`` output.
    """
    resolved = registry_from(registry)
    redacted_checks: list[dict[str, Any]] = []
    raw_checks = record.get("checks")
    if isinstance(raw_checks, list):
        for raw in raw_checks:
            if not isinstance(raw, Mapping):
                continue
            entry = dict(raw)
            evidence = entry.get("evidence", "")
            text = resolved.redact(evidence) if isinstance(evidence, str) else ""
            if len(text) > REPORT_EVIDENCE_CAP:
                text = text[:REPORT_EVIDENCE_CAP]
            entry["evidence"] = text
            redacted_checks.append(entry)
    return {
        "pin_key": str(record.get("pin_key", "")),
        "ran_at": str(record.get("ran_at", "")),
        "checks": redacted_checks,
    }


@dataclass(frozen=True)
class CapabilityAssertionResult:
    """The Provider Capability Assertion passed.

    ``response`` is the gating ``tool-call-auto`` probe's response object,
    exactly as the model returned it — never rebuilt (ADR 0010). ``report``
    is the Provider Capability Report: every check's recorded result, which
    the caller persists.
    """

    pin: PinnedModel
    response: AIMessage
    report: ProviderCapabilityReport


class _EffortOverride:
    """``bound``, invoked with a call-time ``reasoning_effort`` override.

    Both provider adapters accept ``reasoning_effort`` at call time and put
    it on the wire where the constructor's value would go (Anthropic's
    ``output_config.effort``, OpenAI Responses' ``reasoning.effort``).
    """

    def __init__(self, bound: InvokableToolModel, effort: str) -> None:
        self._bound = bound
        self._effort = effort

    def bind_tools(
        self, tools: Sequence[BaseTool], tool_choice: str | None = None
    ) -> InvokableToolModel:
        return _EffortOverride(
            self._bound.bind_tools(tools, tool_choice=tool_choice), self._effort
        )

    def invoke(self, input: LanguageModelInput, **kwargs: Any) -> BaseMessage:
        invoke = cast(Callable[..., BaseMessage], self._bound.invoke)
        return invoke(input, reasoning_effort=self._effort, **kwargs)


def assert_provider_capability(
    pin: PinnedModel,
    model: InvokableToolModel,
    price_table: PriceTable,
    *,
    max_tokens: int | None = None,
) -> CapabilityAssertionResult:
    """ADR 0009's Required Capability list, established by attempting rather
    than by reading a declaration (`L1-4`, `L1-4a`, `L1-4b`).

    - **a price** for the pin in ``price_table`` — on the configured path,
      the Effective Configuration's ``prices_usd_per_mtok``. No provider
      exposes a rate through any API, so this is checked first and costs no
      call;
    - **a call of the named probe tool under ``auto``**, proven against an
      asking prompt — live calls under the pinned effort bind the probe
      tool with no forced ``tool_choice`` and send a prompt asking for a
      call; only a call to the probe tool whose arguments match its schema
      passes, and that same response must report non-zero usage. A text
      answer or a call to another tool refuses the pin with no retry, while
      arguments that do not match get a bounded number of tries
      (:data:`AUTO_PROBE_MAX_TRIES` in total): the existing retry policy
      for retryable provider errors is unchanged;
    - **the diagnostic forced probe** — today's forced ``tool_choice``
      probe, kept as a recorded diagnostic that never refuses startup by
      itself: ``passed`` on a call to the probe tool, ``text-answer`` where
      the forced choice was silently downgraded, ``refused`` where the
      provider or gateway refused the request, with the error text as
      evidence. Both tool probes run once the price check passes and both
      results are recorded before startup is refused for a tool-calling
      reason;
    - **gateway identity on every probe, including the forced one** — retry
      or fallback evidence above 0, or a model group other than the pin,
      refuses startup, on a response and on an error alike. Evidence of
      fallback is a fact about the gateway, not about forced tool choice;
    - **the pinned effort reaches the provider** — where the gating probe
      response reports the effort it ran at, the reported effort must equal
      the pinned effort exactly, and the invalid-effort probe is skipped.
      Where the response reports none, the same request (unforced bind,
      asking prompt) sent with an invalid effort must be refused as an
      invalid request naming the effort. The live call above already proved
      the identical request otherwise succeeds, so the refusal is
      attributable to the effort value alone.

    Step-workflow contract §2.10, §11.4 (matrix row STEP-17): where
    ``max_tokens`` — the Pinned Model's code-owned output cap — is given,
    every probe request carries it, so the assertion proves the cap reaches
    the provider. ``None`` keeps the MVP call shape working.

    Returns the assertion result carrying the Provider Capability Report;
    raises :class:`ProviderCapabilityRefused` carrying the report built up
    to the refusal, so the caller always has it.
    """
    ran_at = _utc_now_z()
    finished: list[CapabilityCheck] = []

    def _not_run(name: str, *, gating: bool = True) -> CapabilityCheck:
        return CapabilityCheck(
            check=name, gating=gating, outcome=OUTCOME_NOT_RUN, evidence=""
        )

    def _refuse_after(names: list[str], message: str) -> ProviderCapabilityRefused:
        checks = list(finished)
        for name in names:
            checks.append(_not_run(name, gating=(name != CHECK_TOOL_CALL_FORCED)))
        return ProviderCapabilityRefused(
            message,
            report=ProviderCapabilityReport(
                pin_key=pin.key, ran_at=ran_at, checks=tuple(checks)
            ),
        )

    if price_for(price_table, pin) is None:
        report = ProviderCapabilityReport(
            pin_key=pin.key,
            ran_at=ran_at,
            checks=tuple(
                [
                    CapabilityCheck(
                        check=CHECK_PRICE,
                        gating=True,
                        outcome=OUTCOME_REFUSED,
                        evidence=f"no Price Table entry for pinned model {pin.key!r}",
                    ),
                    _not_run(CHECK_TOOL_CALL_AUTO),
                    _not_run(CHECK_TOOL_CALL_FORCED, gating=False),
                    _not_run(CHECK_USAGE),
                    _not_run(CHECK_IDENTITY),
                    _not_run(CHECK_EFFORT),
                ]
            ),
        )
        raise ProviderCapabilityRefused(
            f"no Price Table entry for pinned model {pin.key!r}", report=report
        )
    finished.append(
        CapabilityCheck(
            check=CHECK_PRICE,
            gating=True,
            outcome=OUTCOME_PASSED,
            evidence=f"price table entry for {pin.key!r}",
        )
    )

    # Both tool probes run once the price check passes; both results are
    # recorded before startup is refused for a tool-calling reason. The
    # gating auto probe gets a bounded number of tries, but only where the
    # probe tool is called with arguments that do not match its schema —
    # every other gating failure refuses immediately, as decided in #474.
    # Every try carries the same pinned effort and output cap, and gateway
    # identity is checked on every try, including a failed one.
    failed_tries: list[str] = []
    passing_response: AIMessage | None = None
    auto_error: BaseException | None = None
    fatal_failure: str | None = None
    fatal_response: BaseMessage | None = None
    failed_try_identity: str | None = None
    for try_number in range(1, AUTO_PROBE_MAX_TRIES + 1):
        auto_response, auto_error = _run_auto_probe(pin, model, max_tokens=max_tokens)
        if auto_error is not None:
            break
        assert auto_response is not None
        if not isinstance(auto_response, AIMessage):
            fatal_response = auto_response
            break
        failure = _auto_probe_failure(auto_response)
        if failure is None:
            passing_response = auto_response
            break
        if not _is_schema_mismatch(auto_response):
            fatal_failure = failure
            failed_try_identity = _auto_identity_failure(pin, auto_response)
            break
        failed_tries.append(_mismatched_probe_args_text(auto_response))
        failed_try_identity = _auto_identity_failure(pin, auto_response)
        if failed_try_identity is not None:
            break
    forced_response, forced_error = _run_forced_probe(pin, model, max_tokens=max_tokens)

    if auto_error is not None:
        error_evidence = _auto_error_evidence(failed_tries, auto_error)
        finished.append(
            CapabilityCheck(
                check=CHECK_TOOL_CALL_AUTO,
                gating=True,
                outcome=OUTCOME_REFUSED,
                evidence=error_evidence,
            )
        )
        finished.append(_forced_entry(pin, forced_response, forced_error))
        if pin.is_gateway and check_gateway_error_exc(pin, auto_error).violated:
            finished.append(_not_run(CHECK_USAGE))
            finished.append(
                CapabilityCheck(
                    check=CHECK_IDENTITY,
                    gating=True,
                    outcome=OUTCOME_REFUSED,
                    evidence=(
                        "the tool-call-auto probe request carried retry or "
                        "fallback evidence above 0 — another model answered "
                        "for the gateway pin, so startup refuses"
                    ),
                )
            )
            finished.append(_not_run(CHECK_EFFORT))
            raise ProviderCapabilityRefused(
                f"pinned model {pin.key!r}: the probe request carried retry "
                f"or fallback evidence above 0 — another model answered for "
                f"the gateway pin, so startup refuses",
                report=ProviderCapabilityReport(
                    pin_key=pin.key, ran_at=ran_at, checks=tuple(finished)
                ),
            ) from auto_error
        raise _refuse_after(
            [CHECK_USAGE, CHECK_IDENTITY, CHECK_EFFORT],
            f"pinned model {pin.key!r} refused the capability probe: {error_evidence}",
        ) from auto_error

    if fatal_response is not None:
        finished.append(
            CapabilityCheck(
                check=CHECK_TOOL_CALL_AUTO,
                gating=True,
                outcome=OUTCOME_REFUSED,
                evidence=f"returned {type(fatal_response).__name__}, not an AIMessage",
            )
        )
        finished.append(_forced_entry(pin, forced_response, forced_error))
        raise _refuse_after(
            [CHECK_USAGE, CHECK_IDENTITY, CHECK_EFFORT],
            f"pinned model {pin.key!r} returned {type(fatal_response).__name__}, "
            f"not an AIMessage",
        )

    if failed_try_identity is not None:
        if fatal_failure is not None:
            auto_identity_evidence = fatal_failure
            identity_message = failed_try_identity
        else:
            auto_identity_evidence = _mismatch_evidence(failed_tries)
            identity_message = (
                f"{failed_try_identity} ({_mismatch_evidence(failed_tries)})"
            )
        finished.append(
            CapabilityCheck(
                check=CHECK_TOOL_CALL_AUTO,
                gating=True,
                outcome=OUTCOME_REFUSED,
                evidence=auto_identity_evidence,
            )
        )
        finished.append(_forced_entry(pin, forced_response, forced_error))
        finished.append(_not_run(CHECK_USAGE))
        finished.append(
            CapabilityCheck(
                check=CHECK_IDENTITY,
                gating=True,
                outcome=OUTCOME_REFUSED,
                evidence=failed_try_identity,
            )
        )
        finished.append(_not_run(CHECK_EFFORT))
        raise ProviderCapabilityRefused(
            identity_message,
            report=ProviderCapabilityReport(
                pin_key=pin.key, ran_at=ran_at, checks=tuple(finished)
            ),
        )

    if fatal_failure is not None:
        finished.append(
            CapabilityCheck(
                check=CHECK_TOOL_CALL_AUTO,
                gating=True,
                outcome=OUTCOME_REFUSED,
                evidence=fatal_failure,
            )
        )
        finished.append(_forced_entry(pin, forced_response, forced_error))
        raise _refuse_after(
            [CHECK_USAGE, CHECK_IDENTITY, CHECK_EFFORT],
            f"pinned model {pin.key!r} did not call the {capability_probe.name!r} "
            f"tool under the pinned {pin.effort!r} effort ({fatal_failure})",
        )

    if passing_response is None:
        mismatch_evidence = _mismatch_evidence(failed_tries)
        finished.append(
            CapabilityCheck(
                check=CHECK_TOOL_CALL_AUTO,
                gating=True,
                outcome=OUTCOME_REFUSED,
                evidence=mismatch_evidence,
            )
        )
        finished.append(_forced_entry(pin, forced_response, forced_error))
        raise _refuse_after(
            [CHECK_USAGE, CHECK_IDENTITY, CHECK_EFFORT],
            f"pinned model {pin.key!r} did not call the {capability_probe.name!r} "
            f"tool under the pinned {pin.effort!r} effort ({mismatch_evidence})",
        )
    if failed_tries:
        finished.append(
            CapabilityCheck(
                check=CHECK_TOOL_CALL_AUTO,
                gating=True,
                outcome=OUTCOME_PASSED,
                evidence=_retry_pass_evidence(failed_tries),
            )
        )
    else:
        finished.append(
            CapabilityCheck(
                check=CHECK_TOOL_CALL_AUTO,
                gating=True,
                outcome=OUTCOME_PASSED,
                evidence=f"called {capability_probe.name!r} under auto",
            )
        )
    finished.append(_forced_entry(pin, forced_response, forced_error))

    usage = passing_response.usage_metadata
    if not usage or not usage.get("total_tokens"):
        finished.append(
            CapabilityCheck(
                check=CHECK_USAGE,
                gating=True,
                outcome=OUTCOME_REFUSED,
                evidence="the probe response reported zero usage",
            )
        )
        raise _refuse_after(
            [CHECK_IDENTITY, CHECK_EFFORT],
            f"pinned model {pin.key!r} reported zero usage on a successful response",
        )
    finished.append(
        CapabilityCheck(
            check=CHECK_USAGE,
            gating=True,
            outcome=OUTCOME_PASSED,
            evidence=_usage_evidence(usage),
        )
    )

    identity_failure = _identity_failure(
        pin, passing_response, forced_response, forced_error
    )
    if identity_failure is not None:
        finished.append(
            CapabilityCheck(
                check=CHECK_IDENTITY,
                gating=True,
                outcome=OUTCOME_REFUSED,
                evidence=identity_failure,
            )
        )
        raise _refuse_after(
            [CHECK_EFFORT],
            identity_failure,
        )
    finished.append(
        CapabilityCheck(
            check=CHECK_IDENTITY,
            gating=True,
            outcome=OUTCOME_PASSED,
            evidence=_identity_evidence(pin, passing_response),
        )
    )

    effort_entry, effort_refusal = _assert_effort_reaches_provider(
        pin, model, passing_response, max_tokens=max_tokens
    )
    finished.append(effort_entry)
    if effort_refusal is not None:
        raise _refuse_after([], effort_refusal)

    return CapabilityAssertionResult(
        pin=pin,
        response=passing_response,
        report=ProviderCapabilityReport(
            pin_key=pin.key, ran_at=ran_at, checks=tuple(finished)
        ),
    )


def _run_auto_probe(
    pin: PinnedModel, model: InvokableToolModel, *, max_tokens: int | None
) -> tuple[BaseMessage | None, BaseException | None]:
    """Run the gating probe: the probe tool bound unforced, asking prompt."""
    try:
        bound = model.bind_tools([capability_probe])
        return _invoke_probe(bound, _ASK_PROBE_PROMPT, max_tokens=max_tokens), None
    except Exception as exc:
        return None, exc


def _run_forced_probe(
    pin: PinnedModel, model: InvokableToolModel, *, max_tokens: int | None
) -> tuple[BaseMessage | None, BaseException | None]:
    """Run the diagnostic forced probe: forced choice, contrary prompt.

    Never refuses startup by itself: the outcome is recorded and the
    gateway identity evidence still gates through the identity check.
    """
    try:
        bound = model.bind_tools([capability_probe], tool_choice=_FORCED_TOOL_CHOICE)
        return _invoke_probe(bound, _PROBE_PROMPT, max_tokens=max_tokens), None
    except Exception as exc:
        return None, exc


def _forced_entry(
    pin: PinnedModel,
    response: BaseMessage | None,
    error: BaseException | None,
) -> CapabilityCheck:
    """The recorded outcome of the diagnostic forced probe (never gating)."""
    if error is not None:
        return CapabilityCheck(
            check=CHECK_TOOL_CALL_FORCED,
            gating=False,
            outcome=OUTCOME_REFUSED,
            evidence=_short_error(error),
        )
    assert response is not None
    if isinstance(response, AIMessage) and any(
        call.get("name") == capability_probe.name for call in response.tool_calls
    ):
        return CapabilityCheck(
            check=CHECK_TOOL_CALL_FORCED,
            gating=False,
            outcome=OUTCOME_PASSED,
            evidence=f"called {capability_probe.name!r} under the forced choice",
        )
    called = (
        ",".join(sorted({str(call.get("name")) for call in response.tool_calls}))
        if isinstance(response, AIMessage) and response.tool_calls
        else "text"
    )
    return CapabilityCheck(
        check=CHECK_TOOL_CALL_FORCED,
        gating=False,
        outcome=OUTCOME_TEXT_ANSWER,
        evidence=(
            "the forced choice was silently downgraded to auto: "
            f"answered with {called}"
        ),
    )


def _probe_tool_call(response: AIMessage) -> dict[str, Any] | None:
    """The response's call to the probe tool, if it made exactly the point.

    ``None`` where the response called no probe tool, called another tool
    instead, or the probe call's arguments do not match the tool's schema.
    """
    for call in response.tool_calls:
        if call.get("name") != capability_probe.name:
            continue
        args = call.get("args")
        if not isinstance(args, dict):
            return None
        try:
            schema = capability_probe.args_schema
            model_validate = getattr(schema, "model_validate", None)
            if callable(model_validate):
                model_validate(args)
            else:
                parse_obj = getattr(schema, "parse_obj", None)
                if not callable(parse_obj):
                    return None
                parse_obj(args)
        except Exception:
            return None
        return cast(dict[str, Any], call)
    return None


def _auto_probe_failure(response: AIMessage) -> str | None:
    """Why the gating probe response refuses the pin, or ``None``.

    Only a call to the probe tool whose arguments match its schema passes:
    a text answer, a call to another tool, or arguments that do not match
    refuse startup. Only the arguments-mismatch failure is retried, up to
    :data:`AUTO_PROBE_MAX_TRIES` tries in total; every other failure
    refuses with no retry.
    """
    if not response.tool_calls:
        return "answered with text instead of calling the probe tool"
    names = sorted({str(call.get("name")) for call in response.tool_calls})
    if all(name != capability_probe.name for name in names):
        return f"called {names[0]!r} instead of the probe tool"
    if _probe_tool_call(response) is None:
        return "the probe tool call's arguments did not match its schema"
    return None


def _is_schema_mismatch(response: AIMessage) -> bool:
    """Whether the gating failure is the retried kind: the probe tool was
    called, but its arguments did not match the tool schema.

    A text answer or a call to another tool is never a schema mismatch and
    refuses immediately, as decided in #474.
    """
    if not response.tool_calls:
        return False
    names = {str(call.get("name")) for call in response.tool_calls}
    if all(name != capability_probe.name for name in names):
        return False
    return _probe_tool_call(response) is None


def _format_probe_args(args: Any) -> str:
    """Received tool-call arguments as JSON, truncated for evidence.

    One try's arguments are capped at :data:`_ARGS_EVIDENCE_CAP`
    characters, so every try is recorded yet the entry stays well under
    :data:`REPORT_EVIDENCE_CAP`.
    """
    try:
        text = json.dumps(args, sort_keys=True)
    except (TypeError, ValueError):
        text = str(args)
    if len(text) > _ARGS_EVIDENCE_CAP:
        text = text[:_ARGS_EVIDENCE_CAP]
    return text


def _mismatched_probe_args_text(response: AIMessage) -> str:
    """The received arguments of a schema-mismatching probe call."""
    for call in response.tool_calls:
        if call.get("name") != capability_probe.name:
            continue
        return _format_probe_args(call.get("args"))
    return _format_probe_args(None)


def _prior_tries_text(failed_tries: Sequence[str]) -> str:
    """One ``try N: ...`` fragment per failed try, in try order."""
    return "; ".join(
        f"try {number}: arguments did not match its schema, got {received}"
        for number, received in enumerate(failed_tries, start=1)
    )


def _mismatch_evidence(failed_tries: Sequence[str]) -> str:
    """The refused ``tool-call-auto`` evidence after every try failed."""
    tried = "; ".join(failed_tries)
    return (
        "the probe tool call's arguments did not match its schema "
        f"on {len(failed_tries)} of {AUTO_PROBE_MAX_TRIES} tries (got {tried})"
    )


def _retry_pass_evidence(failed_tries: Sequence[str]) -> str:
    """The passed ``tool-call-auto`` evidence after a retry.

    A pass on the first try keeps today's evidence unchanged; only a pass
    after a retry names the try and the earlier tries' arguments.
    """
    return (
        f"called {capability_probe.name!r} under auto "
        f"on try {len(failed_tries) + 1} of {AUTO_PROBE_MAX_TRIES} "
        f"({_prior_tries_text(failed_tries)})"
    )


def _auto_error_evidence(failed_tries: Sequence[str], error: BaseException) -> str:
    """The refused ``tool-call-auto`` evidence for a probe error.

    Where earlier tries failed with mismatched arguments, their failures
    are listed before the error that refused startup.
    """
    if not failed_tries:
        return _short_error(error)
    evidence = f"{_prior_tries_text(failed_tries)}; probe request failed: {_short_error(error)}"
    if len(evidence) > REPORT_EVIDENCE_CAP:
        evidence = evidence[:REPORT_EVIDENCE_CAP]
    return evidence


def _usage_evidence(usage: Any) -> str:
    """The non-zero usage totals behind a passed usage check."""
    if isinstance(usage, Mapping):
        total = usage.get("total_tokens")
        arrived = usage.get("input_tokens")
        departed = usage.get("output_tokens")
        return f"usage input={arrived} output={departed} total={total}"
    return "usage reported"


def _auto_identity_failure(pin: PinnedModel, auto_response: AIMessage) -> str | None:
    """Why one gating probe response's own identity refuses the pin, or ``None``.

    Gateway identity is checked on every auto try, including a failed one:
    retry or fallback evidence above 0, or a model group other than the
    pin, refuses startup — as does a direct pin's response reporting
    another model id.
    """
    check = check_direct_pin(pin, auto_response)
    if check.violated:
        if pin.is_gateway:
            return (
                f"pinned model {pin.key!r}: the probe response's model group "
                f"was {check.observed!r} — a gateway pin's probe response "
                f"must carry {pin.model!r} with attempted fallbacks 0, else "
                f"another model answered for the pin, so startup refuses"
            )
        return (
            f"pinned model {pin.key!r} resolved to {check.observed!r}: a direct "
            f"pin's probe response must report the pinned model id "
            f"{pin.model!r} exactly — pin the model the vendor reports"
        )
    return None


def _identity_failure(
    pin: PinnedModel,
    auto_response: AIMessage,
    forced_response: BaseMessage | None,
    forced_error: BaseException | None,
) -> str | None:
    """Why the probe identity evidence refuses the pin, or ``None``.

    Gateway identity gates on every probe, including the diagnostic forced
    one: retry or fallback evidence above 0, or a model group other than
    the pin, refuses startup, on a response and on an error alike.
    """
    auto_failure = _auto_identity_failure(pin, auto_response)
    if auto_failure is not None:
        return auto_failure
    if pin.is_gateway:
        if forced_response is not None:
            forced_check = check_direct_pin(pin, forced_response)
            if forced_check.violated:
                return (
                    f"pinned model {pin.key!r}: the forced probe response's "
                    f"model group was {forced_check.observed!r} — a gateway "
                    f"pin's probe response must carry {pin.model!r} with "
                    f"attempted fallbacks 0, else another model answered "
                    f"for the pin, so startup refuses"
                )
        elif forced_error is not None and check_gateway_error_exc(
            pin, forced_error
        ).violated:
            return (
                f"pinned model {pin.key!r}: the forced probe request carried "
                f"retry or fallback evidence above 0 — another model "
                f"answered for the gateway pin, so startup refuses"
            )
    return None


def _identity_evidence(pin: PinnedModel, auto_response: AIMessage) -> str:
    """The identity evidence behind a passed identity check."""
    check = check_direct_pin(pin, auto_response)
    if pin.is_gateway and check.gateway is not None:
        identity = check.gateway
        return (
            f"model group {identity.model_group!r} with attempted fallbacks "
            f"{identity.attempted_fallbacks} and retries "
            f"{identity.attempted_retries}"
        )
    return f"response model {check.observed!r} equals the pinned model"


def _short_error(exc: BaseException) -> str:
    """One provider error as short evidence text (redacted at write time)."""
    text = str(exc)
    if len(text) > REPORT_EVIDENCE_CAP:
        text = text[:REPORT_EVIDENCE_CAP]
    return text


def _utc_now_z() -> str:
    """Now in UTC, formatted as RFC 3339 with a ``Z`` suffix."""
    moment = datetime.now(timezone.utc)
    base = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        base += f".{moment.microsecond:06d}"
    return base + "Z"


def _invoke_probe(
    bound: InvokableToolModel, prompt: str, *, max_tokens: int | None
) -> BaseMessage:
    """Run one capability probe, carrying the output cap where one is pinned.

    ``max_tokens`` travels as a call-time kwarg the fakes record and the
    real adapters forward, exactly like the effort override below. ``None``
    sends the probe exactly as the MVP did.
    """
    if max_tokens is None:
        return invoke_with_retry(bound, prompt)
    return invoke_with_retry(_OutputCapOverride(bound, max_tokens), prompt)


class _OutputCapOverride:
    """``bound``, invoked with a call-time ``max_tokens`` output cap."""

    def __init__(self, bound: InvokableToolModel, max_tokens: int) -> None:
        self._bound = bound
        self._max_tokens = max_tokens

    def bind_tools(
        self, tools: Sequence[BaseTool], tool_choice: str | None = None
    ) -> InvokableToolModel:
        return _OutputCapOverride(
            self._bound.bind_tools(tools, tool_choice=tool_choice), self._max_tokens
        )

    def invoke(self, input: LanguageModelInput, **kwargs: Any) -> BaseMessage:
        invoke = cast(Callable[..., BaseMessage], self._bound.invoke)
        return invoke(input, max_tokens=self._max_tokens, **kwargs)


def _assert_effort_reaches_provider(
    pin: PinnedModel,
    model: InvokableToolModel,
    auto_response: AIMessage,
    *,
    max_tokens: int | None = None,
) -> tuple[CapabilityCheck, str | None]:
    """The effort leg: the response's own evidence, else the invalid-effort probe.

    Returns the effort report entry and, where the leg refuses the pin, the
    refusal message (``None`` where the leg passes). Where the gating probe
    response reports the effort it ran at, the reported effort must exactly
    equal the pinned effort and the invalid-effort probe is skipped. Where
    the response reports none, the same request (unforced bind, asking
    prompt) sent with an invalid effort must be refused as an invalid
    request naming the effort.
    """
    reported = check_reported_effort(pin, auto_response)
    if reported.reported is not None:
        return _reported_effort_entry(pin, reported)
    return _invalid_effort_leg(pin, model, max_tokens=max_tokens)


def _reported_effort_entry(
    pin: PinnedModel, check: EffortCheck
) -> tuple[CapabilityCheck, str | None]:
    """The effort entry from the response's own reported effort."""
    assert check.reported is not None
    if not check.violated:
        return (
            CapabilityCheck(
                check=CHECK_EFFORT,
                gating=True,
                outcome=OUTCOME_PASSED,
                evidence=(
                    f"the probe response ran at the pinned effort "
                    f"{pin.effort!r} and reported it"
                ),
                via=VIA_REPORTED,
                reported=check.reported,
                pinned=pin.effort,
            ),
            None,
        )
    return (
        CapabilityCheck(
            check=CHECK_EFFORT,
            gating=True,
            outcome=OUTCOME_REFUSED,
            evidence=(
                f"the probe response reported effort {check.reported!r}, "
                f"not the pinned {pin.effort!r}"
            ),
            via=VIA_REPORTED,
            reported=check.reported,
            pinned=pin.effort,
        ),
        (
            f"pinned model {pin.key!r} reported effort {check.reported!r} — "
            f"not the pinned {pin.effort!r} effort, so the effort setting "
            f"does not reach the provider as pinned"
        ),
    )


def _invalid_effort_leg(
    pin: PinnedModel, model: InvokableToolModel, *, max_tokens: int | None
) -> tuple[CapabilityCheck, str | None]:
    """The invalid-effort refusal leg, for responses that report no effort."""
    try:
        effort_probe: InvokableToolModel = _EffortOverride(
            model.bind_tools([capability_probe]), UNHONOURED_EFFORT
        )
        if max_tokens is not None:
            effort_probe = _OutputCapOverride(effort_probe, max_tokens)
        invoke_with_retry(effort_probe, _ASK_PROBE_PROMPT)
    except ModelInvalidRequestError as exc:
        # A gateway pin fails closed on retry or fallback evidence even on an
        # error: the suppression fields were not honoured (§9.1).
        if pin.is_gateway and check_gateway_error_exc(pin, exc).violated:
            entry = CapabilityCheck(
                check=CHECK_EFFORT,
                gating=True,
                outcome=OUTCOME_REFUSED,
                evidence=(
                    "the invalid-effort probe carried retry or fallback "
                    "evidence above 0 — another model answered for the "
                    "gateway pin, so startup refuses"
                ),
                via=VIA_INVALID_EFFORT_REFUSED,
                pinned=pin.effort,
            )
            return entry, (
                f"pinned model {pin.key!r}: the invalid-effort probe carried retry "
                f"or fallback evidence above 0 — another model answered for "
                f"the gateway pin, so startup refuses"
            )
        # Both providers refuse naming the parameter or the value (ADR 0009);
        # an invalid request for any other reason establishes nothing.
        if "effort" not in str(exc).lower():
            entry = CapabilityCheck(
                check=CHECK_EFFORT,
                gating=True,
                outcome=OUTCOME_REFUSED,
                evidence=_short_error(exc),
                via=VIA_INVALID_EFFORT_REFUSED,
                pinned=pin.effort,
            )
            return entry, (
                f"pinned model {pin.key!r}: the invalid-effort probe was refused for a "
                f"reason that does not name the effort setting: {exc}"
            )
        return (
            CapabilityCheck(
                check=CHECK_EFFORT,
                gating=True,
                outcome=OUTCOME_PASSED,
                evidence=_short_error(exc),
                via=VIA_INVALID_EFFORT_REFUSED,
                pinned=pin.effort,
            ),
            None,
        )
    except Exception as exc:
        if pin.is_gateway and check_gateway_error_exc(pin, exc).violated:
            entry = CapabilityCheck(
                check=CHECK_EFFORT,
                gating=True,
                outcome=OUTCOME_REFUSED,
                evidence=(
                    "the invalid-effort probe carried retry or fallback "
                    "evidence above 0 — another model answered for the "
                    "gateway pin, so startup refuses"
                ),
                via=VIA_INVALID_EFFORT_REFUSED,
                pinned=pin.effort,
            )
            return entry, (
                f"pinned model {pin.key!r}: the invalid-effort probe carried retry "
                f"or fallback evidence above 0 — another model answered for "
                f"the gateway pin, so startup refuses"
            )
        entry = CapabilityCheck(
            check=CHECK_EFFORT,
            gating=True,
            outcome=OUTCOME_REFUSED,
            evidence=_short_error(exc),
            via=VIA_INVALID_EFFORT_REFUSED,
            pinned=pin.effort,
        )
        return entry, (
            f"pinned model {pin.key!r}: could not establish that the effort setting "
            f"reaches the provider: {exc}"
        )
    entry = CapabilityCheck(
        check=CHECK_EFFORT,
        gating=True,
        outcome=OUTCOME_REFUSED,
        evidence=(
            f"the provider accepted the invalid effort {UNHONOURED_EFFORT!r} — "
            f"the effort setting does not reach the provider"
        ),
        via=VIA_INVALID_EFFORT_REFUSED,
        pinned=pin.effort,
    )
    return entry, (
        f"pinned model {pin.key!r} accepted the invalid effort {UNHONOURED_EFFORT!r} — "
        f"the effort setting does not reach the provider, so the pinned "
        f"{pin.effort!r} effort is not honoured"
    )
