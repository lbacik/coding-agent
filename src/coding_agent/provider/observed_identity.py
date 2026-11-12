"""The observed answering model of one model response (direct pins).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §13 (the Run
Ledger records the model that answered each response, taken from that
response's own evidence and never copied from the pin) and §9.1 (a direct
pin's response model must equal the pinned model id exactly, asserted per
response).

For a direct pin the observed identity is the response model the provider
reports in the response metadata: ``model``, falling back to ``model_name``
(the two adapters name the same field differently). A response whose
metadata carries neither records that the identity was not reported
(``None``), never the pin key.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from coding_agent.provider.pinned_model import PinnedModel

MODEL_RESPONSE_KIND = "model_response"
"""Run Ledger event kind: one model response's observed identity (§13).

One event per model response in a Step Execution and in every Sub-agent.
The payload carries the Attempt id, the Step Execution (step, sequence
number), the response index within that execution (a Sub-agent response
also carries its call id) and the observed ``response_model``: the
response model string, or ``None`` where the response metadata carried
none (the identity was not reported).
"""

PIN_VIOLATION_KIND = "pin_violation"
"""Run Ledger event kind: one asserted pin violation (§9.1).

Recorded where the violation is observed — a Step Execution's tool loop
or a Sub-agent — beside the violating response's own ``model_response``
event. The payload carries the Attempt id, the Step Execution (step,
sequence number; a Sub-agent violation also carries its call id), the
response index, the expected pinned model id and the observed model.
"""

PIN_VIOLATION_REASON = "pin-violation"
"""The runtime-only Terminal Outcome reason for an asserted pin violation.

The Attempt ends ``interrupted`` with this reason (§12, §16.3); the set
of Terminal Outcomes stays closed, only this reason is new.
"""

PIN_VIOLATION_DETAIL = (
    "pin-violation: a model response reported a model other than the "
    "pinned model, so the Attempt ends here; the violating response was "
    "discarded and the Worker halts — pin the model the vendor reports "
    "and restart (see the pin_violation ledger event for the observed identity)"
)
"""The terminal comment detail for a pin-violated Attempt (§15.3)."""

_RESPONSE_MODEL_KEYS = ("model", "model_name")
"""Response-metadata keys holding the provider-reported response model.

Anthropic's streaming adapter reports ``model_name``; the OpenAI adapter
reports ``model_name`` in its output while the wire model travels as
``model``. Either key counts as the response's own evidence.
"""


def observed_response_model(response: BaseMessage) -> str | None:
    """The model one response reports about itself, or ``None``.

    Reads the response metadata's ``model`` (falling back to
    ``model_name``) where it is a non-blank string, else ``None``: the
    identity was not reported. Anything that is not a model response —
    a human or tool message, or metadata that is not a mapping — also
    yields ``None``. Never the pin key.
    """
    if not isinstance(response, AIMessage):
        return None
    metadata: Any = getattr(response, "response_metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    for key in _RESPONSE_MODEL_KEYS:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip() != "":
            return value
    return None


def models_of_responses(messages: Iterable[BaseMessage]) -> list[str | None]:
    """The observed model of every assistant turn, in order.

    One entry per :class:`AIMessage` (``None`` where that response did
    not report one); any other message contributes nothing.
    """
    return [
        observed_response_model(message)
        for message in messages
        if isinstance(message, AIMessage)
    ]


def last_reported_model(models: Iterable[str | None]) -> str | None:
    """The most recent reported model, or ``None`` where none was reported.

    A Sub-agent's ``answering_model``: with no fallback every response
    agrees, so the most recent reported one stands for the run; where no
    response reported a model the answer is ``None`` (not reported),
    never the pin key.
    """
    last: str | None = None
    for model in models:
        if model is not None:
            last = model
    return last


@dataclass(frozen=True)
class PinCheck:
    """One direct pin's verdict over one model response (§9.1).

    ``observed`` is the response's own evidence (``None`` where it
    reported none); ``expected`` is the pinned model id; ``violated`` is
    whether the response is proven to come from another model. Only a
    reported model that differs from the pin exactly is a violation: a
    response that reports no model carries no evidence either way.
    Gateway pins add their own observed fields to this same verdict later.
    """

    observed: str | None
    expected: str
    violated: bool


def check_direct_pin(pin: PinnedModel, response: BaseMessage) -> PinCheck:
    """Assert one model response against its direct pin (§9.1).

    Returns the response's observed identity and the verdict: a direct
    pin's response model must equal the pinned model id exactly. Both
    current pins echo the pinned id with no snapshot suffix, so any
    reported difference — including a vendor-resolved snapshot — is a
    violation, and the caller must refuse to use the response.
    """
    observed = observed_response_model(response)
    return PinCheck(
        observed=observed,
        expected=pin.model,
        violated=observed is not None and observed != pin.model,
    )


class PinViolation(Exception):
    """A model response proven to come from another model (§9.1, §12).

    Raised where the violation is observed — the Step Execution's tool
    loop or a Sub-agent — carrying the check and the response's place, so
    the Workflow graph can end the Attempt ``interrupted`` /
    ``pin-violation`` without routing it. Carries the violating response
    itself so the catcher can charge its usage without re-reading it.
    """

    def __init__(
        self,
        *,
        observed: str | None,
        expected: str,
        step_id: str,
        seq: int,
        call_id: str | None = None,
        response_index: int = 0,
        response: BaseMessage | None = None,
    ) -> None:
        self.observed = observed
        self.expected = expected
        self.step_id = step_id
        self.seq = seq
        self.call_id = call_id
        self.response_index = response_index
        self.response = response
        where = f"{step_id}#{seq}"
        if call_id is not None:
            where += f" sub-agent {call_id!r}"
        super().__init__(
            f"pin-violation at {where} response {response_index}: "
            f"reported {observed!r}, pinned {expected!r}"
        )
