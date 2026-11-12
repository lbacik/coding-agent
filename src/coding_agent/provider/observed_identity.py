"""The observed answering model of one model response (direct pins).

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §13 (the Run
Ledger records the model that answered each response, taken from that
response's own evidence and never copied from the pin).

For a direct pin the observed identity is the response model the provider
reports in the response metadata: ``model``, falling back to ``model_name``
(the two adapters name the same field differently). A response whose
metadata carries neither records that the identity was not reported
(``None``), never the pin key.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

MODEL_RESPONSE_KIND = "model_response"
"""Run Ledger event kind: one model response's observed identity (§13).

One event per model response in a Step Execution and in every Sub-agent.
The payload carries the Attempt id, the Step Execution (step, sequence
number), the response index within that execution (a Sub-agent response
also carries its call id) and the observed ``response_model``: the
response model string, or ``None`` where the response metadata carried
none (the identity was not reported).
"""

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
