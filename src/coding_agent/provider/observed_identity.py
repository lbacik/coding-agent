"""The observed answering model of one model response (direct and gateway pins).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §13 (the Run
Ledger records the model that answered each response, taken from that
response's own evidence and never copied from the pin) and §9.1 (a direct
pin's response model must equal the pinned model id exactly, asserted per
response; a gateway pin's response is from the Pinned Model only when the
``x-litellm-model-group`` header equals the pinned name and
``x-litellm-attempted-fallbacks`` is ``0``).

For a direct pin the observed identity is the response model the provider
reports in the response metadata: ``model``, falling back to ``model_name``
(the two adapters name the same field differently). A response whose
metadata carries neither records that the identity was not reported
(``None``), never the pin key.

For a gateway pin the body ``model`` is never read for identity: the
observed identity is the gateway's own ``x-litellm-model-group`` header,
and ``x-litellm-model-id``, ``x-litellm-model-name``,
``x-litellm-attempted-retries``, the call id and the observed
``x-litellm-version`` are recorded, never asserted. The header names are
stated assumptions until issue #337 replaces them with recordings.
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

GATEWAY_HEADER_GROUP = "x-litellm-model-group"
"""The gateway header naming the model group that answered (§9.1).

A response is from the Pinned Model only when this equals the pinned name.
Any deployment the gateway spreads that name over counts as the Pinned
Model. Stated assumption until issue #337 replaces it with recordings.
"""

GATEWAY_HEADER_FALLBACKS = "x-litellm-attempted-fallbacks"
"""The gateway header counting fallback attempts behind the response (§9.1).

A response is from the Pinned Model only when this is ``0``. A successful
response missing it fails closed. Stated assumption until issue #337.
"""

GATEWAY_HEADER_RETRIES = "x-litellm-attempted-retries"
"""The gateway header counting gateway-level retries behind the response (§9.1).

A missing header means ``0``; anything above ``0`` is a pin violation.
Stated assumption until issue #337 replaces it with recordings.
"""

GATEWAY_HEADER_MODEL_ID = "x-litellm-model-id"
"""The gateway header naming the deployment that answered (§13).

Recorded, never asserted. Stated assumption until issue #337.
"""

GATEWAY_HEADER_MODEL_NAME = "x-litellm-model-name"
"""The gateway header naming the answering model within its group (§13).

Recorded, never asserted. Stated assumption until issue #337.
"""

GATEWAY_HEADER_VERSION = "x-litellm-version"
"""The gateway header reporting the gateway version (§13).

Recorded as observed, never asserted. Stated assumption until issue #337.
"""

GATEWAY_HEADER_CALL_ID = "x-litellm-call-id"
"""The gateway header carrying the gateway's own call id (§13).

Recorded, never asserted; where the header is absent the response id
stands in. Stated assumption until issue #337 replaces it with recordings.
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
class GatewayIdentity:
    """One gateway response's observed identity, from its own headers (§13).

    ``model_group`` is the answering group (``None`` where unreported);
    ``attempted_fallbacks`` is the fallback count (``None`` where missing
    or unparseable); ``attempted_retries`` is the retry count (``0`` where
    the header is missing, ``None`` where present but unparseable);
    ``model_id``, ``model_name``, ``version`` and ``call_id`` are recorded,
    never asserted. Where the gateway's own call id is absent the response
    id stands in, else ``None``.
    """

    model_group: str | None
    attempted_fallbacks: int | None
    attempted_retries: int | None
    model_id: str | None
    model_name: str | None
    version: str | None
    call_id: str | None


def _lowered_str_map(raw: Any) -> dict[str, str]:
    """A case-insensitive string map from response or error headers.

    Accepts a mapping or any object with ``items()`` (such as
    ``httpx.Headers``); anything else — and any non-string key or value —
    contributes nothing, so the caller fails closed on what is missing.
    """
    items: Any = None
    if isinstance(raw, Mapping):
        items = raw.items()
    elif hasattr(raw, "items"):
        try:
            items = raw.items()
        except Exception:
            return {}
    else:
        return {}
    lowered: dict[str, str] = {}
    for key, value in items:
        if isinstance(key, str) and isinstance(value, str):
            lowered.setdefault(key.lower(), value)
    return lowered


def _metadata_headers(response: BaseMessage) -> dict[str, str]:
    """A successful response's headers, case-insensitively (§9.1).

    The gateway adapter exposes the wire headers on each message's
    ``response_metadata["headers"]``; anything else contributes nothing.
    """
    metadata: Any = getattr(response, "response_metadata", None)
    if not isinstance(metadata, Mapping):
        return {}
    return _lowered_str_map(metadata.get("headers"))


def _nonblank(values: Mapping[str, str], name: str) -> str | None:
    """One header's stripped value, or ``None`` where missing or blank."""
    value = values.get(name)
    if value is None:
        return None
    text = value.strip()
    return text if text != "" else None


def _parse_count(values: Mapping[str, str], name: str) -> int | None:
    """One numeric header, or ``None`` where missing or unparseable."""
    text = _nonblank(values, name)
    if text is None:
        return None
    try:
        return int(text, 10)
    except ValueError:
        return None


def _gateway_identity_from_headers(
    headers: Mapping[str, str], *, call_id: str | None
) -> GatewayIdentity:
    """A gateway identity from already-lowered headers (§13)."""
    retries_text = _nonblank(headers, GATEWAY_HEADER_RETRIES)
    return GatewayIdentity(
        model_group=_nonblank(headers, GATEWAY_HEADER_GROUP),
        attempted_fallbacks=_parse_count(headers, GATEWAY_HEADER_FALLBACKS),
        attempted_retries=0 if retries_text is None else _parse_count(
            headers, GATEWAY_HEADER_RETRIES
        ),
        model_id=_nonblank(headers, GATEWAY_HEADER_MODEL_ID),
        model_name=_nonblank(headers, GATEWAY_HEADER_MODEL_NAME),
        version=_nonblank(headers, GATEWAY_HEADER_VERSION),
        call_id=_nonblank(headers, GATEWAY_HEADER_CALL_ID) or call_id,
    )


def _response_id(response: BaseMessage) -> str | None:
    """A successful response's own id, or ``None`` where it has none."""
    metadata: Any = getattr(response, "response_metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get("id")
    if not isinstance(value, str) or value.strip() == "":
        return None
    return value


def gateway_identity_of(response: BaseMessage) -> GatewayIdentity:
    """One gateway response's observed identity, from its own headers (§13).

    Never reads the body ``model``: any deployment the gateway spreads the
    pinned name over counts as the Pinned Model.
    """
    return _gateway_identity_from_headers(
        _metadata_headers(response), call_id=_response_id(response)
    )


def gateway_error_headers(exc: BaseException) -> dict[str, str]:
    """The ``x-litellm-*`` headers a failed gateway request carried, if any.

    The adapter surfaces the wire headers on the raised error's
    ``response.headers``; an error without them contributes nothing and is
    classified by its status as today, never as a violation for that alone.
    """
    response: Any = getattr(exc, "response", None)
    return _lowered_str_map(getattr(response, "headers", None))


@dataclass(frozen=True)
class PinCheck:
    """One pin's verdict over one model response (§9.1).

    ``observed`` is the response's own evidence (``None`` where it
    reported none); ``expected`` is the pinned model id; ``violated`` is
    whether the response is proven to come from another model. For a direct
    pin only a reported model that differs from the pin exactly is a
    violation: a response that reports no model carries no evidence either
    way. For a gateway pin ``observed`` is the model group and ``gateway``
    carries the full observed identity: a successful response missing
    either identity header, naming another group, or carrying retry or
    fallback evidence above ``0`` is a violation.
    """

    observed: str | None
    expected: str
    violated: bool
    gateway: GatewayIdentity | None = None


def check_gateway_response(pin: PinnedModel, response: BaseMessage) -> PinCheck:
    """Assert one successful gateway response against its pin (§9.1).

    The response is from the Pinned Model only when the model group equals
    the pinned name and attempted fallbacks is ``0``; a missing group or
    fallback header fails closed, as does any attempted retry above ``0``
    (a missing retry header means ``0``) or an unparseable count.
    """
    identity = gateway_identity_of(response)
    return PinCheck(
        observed=identity.model_group,
        expected=pin.model,
        violated=(
            identity.model_group is None
            or identity.model_group != pin.model
            or identity.attempted_fallbacks is None
            or identity.attempted_fallbacks != 0
            or identity.attempted_retries is None
            or identity.attempted_retries != 0
        ),
        gateway=identity,
    )


def check_gateway_error(
    pin: PinnedModel, headers: Mapping[str, str]
) -> PinCheck:
    """Assert a failed gateway request's headers against its pin (§9.1).

    Only the identity check covers successful responses: an error without
    identity headers is not a violation and is classified by its status as
    today. Retry or fallback evidence above ``0`` on an error is still a
    violation — a retry or fallback happened despite the per-request
    suppression — and a present-but-unparseable count fails closed.
    """
    lowered = _lowered_str_map(headers)
    identity = _gateway_identity_from_headers(lowered, call_id=None)
    fallback_text = _nonblank(lowered, GATEWAY_HEADER_FALLBACKS)
    retry_text = _nonblank(lowered, GATEWAY_HEADER_RETRIES)
    # A present-but-unparseable count compares ``None != 0`` and fails
    # closed; a missing header contributes no text and is never a violation.
    violated = (
        fallback_text is not None
        and identity.attempted_fallbacks != 0
    ) or (
        retry_text is not None
        and identity.attempted_retries != 0
    )
    return PinCheck(
        observed=identity.model_group,
        expected=pin.model,
        violated=violated,
        gateway=identity,
    )


def check_gateway_error_exc(pin: PinnedModel, exc: BaseException) -> PinCheck:
    """Assert a failed gateway request against its pin (§9.1).

    Reads the ``x-litellm-*`` headers the adapter surfaces on the raised
    error; an error carrying none is never a violation for that alone.
    """
    return check_gateway_error(pin, gateway_error_headers(exc))


def check_direct_pin(pin: PinnedModel, response: BaseMessage) -> PinCheck:
    """Assert one model response against its pin (§9.1).

    One function owns the identity rules: a gateway pin takes the gateway
    branch (model group plus fallback and retry evidence, never the body
    model), every other pin the direct branch. A direct pin's response
    model must equal the pinned model id exactly. Both current direct pins
    echo the pinned id with no snapshot suffix, so any reported difference
    — including a vendor-resolved snapshot — is a violation, and the
    caller must refuse to use the response.
    """
    if pin.is_gateway:
        return check_gateway_response(pin, response)
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
    itself so the catcher can charge its usage without re-reading it; a
    violation observed on a failed request carries no response (its hold
    was already charged at worst case) and the observed gateway identity
    instead, so the catcher can still record what answered.
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
        gateway: GatewayIdentity | None = None,
    ) -> None:
        self.observed = observed
        self.expected = expected
        self.step_id = step_id
        self.seq = seq
        self.call_id = call_id
        self.response_index = response_index
        self.response = response
        self.gateway = gateway
        where = f"{step_id}#{seq}"
        if call_id is not None:
            where += f" sub-agent {call_id!r}"
        super().__init__(
            f"pin-violation at {where} response {response_index}: "
            f"reported {observed!r}, pinned {expected!r}"
        )
