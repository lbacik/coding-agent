"""Shared gateway assertions: one suite for the fake and the real gateway.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §4.1 (the
probe) and §9.1 (every gateway request carries the per-request suppression;
the answering model is asserted from the gateway's own identity headers;
assistant turns travel inside the request, never by server-side reference).

The fake gateway (``tests/support/fake_gateway.py``) replays the vendored
recordings through the real adapter, and the L1 rows
(``tests/test_gateway_live.py``, matrix ``GW-1`` … ``GW-5``) run the same
checks against a real gateway. Both sides call the helpers here, so a fake
that drifts from a real gateway fails the default run: when the two
diverge, the recordings are re-recorded.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage

EXPECTED_INCLUDE = ["reasoning.encrypted_content"]
"""The encrypted-reasoning include, sent with every gateway request."""

PROBE_TOOL_CHOICE = {"type": "function", "name": "capability_probe"}
"""The forced ``tool_choice`` of the Provider Capability Assertion probe."""


def assert_suppression_on_wire(body: dict[str, Any]) -> None:
    """Every gateway request carries the per-request suppression (§9.1).

    The Worker's own provider retry is then the only retry: ``num_retries``
    ``0`` with ``disable_fallbacks`` true.
    """
    assert body.get("disable_fallbacks") is True, (
        f"gateway request must carry disable_fallbacks: true, got {body!r}"
    )
    assert body.get("num_retries") == 0, (
        f"gateway request must carry num_retries: 0, got {body!r}"
    )


def assert_self_contained_request(
    body: dict[str, Any],
    *,
    model: str,
    effort: str,
    output_cap: int,
) -> None:
    """A gateway request is self-contained: the gateway key aside, the body
    names the gateway's own model, the pinned effort, the pin's output cap,
    ``store: False`` and the encrypted-reasoning include (§9.1)."""
    assert body.get("model") == model, (
        f"gateway request must name the pinned gateway model {model!r}, "
        f"got {body.get('model')!r}"
    )
    assert body.get("store") is False, (
        f"gateway request must carry store: false, got {body!r}"
    )
    assert body.get("include") == EXPECTED_INCLUDE, (
        f"gateway request must carry include {EXPECTED_INCLUDE!r}, got {body!r}"
    )
    assert body.get("max_output_tokens") == output_cap, (
        f"gateway request must carry the pin's output cap {output_cap}, "
        f"got {body.get('max_output_tokens')!r}"
    )
    assert body.get("reasoning") == {"effort": effort}, (
        f"gateway request must carry the pinned effort {effort!r}, "
        f"got {body.get('reasoning')!r}"
    )
    assert_suppression_on_wire(body)


def assert_probe_request(
    body: dict[str, Any],
    *,
    model: str,
    effort: str,
    output_cap: int,
) -> None:
    """The capability probe is self-contained and forces the probe tool."""
    assert_self_contained_request(
        body, model=model, effort=effort, output_cap=output_cap
    )
    assert body.get("tool_choice") == PROBE_TOOL_CHOICE, (
        f"probe request must force the probe tool choice, got {body!r}"
    )


def gateway_headers_of(message: AIMessage) -> dict[str, str]:
    """The response's gateway identity headers, keyed lowercase."""
    metadata = message.response_metadata
    raw = metadata.get("headers", {})
    assert isinstance(raw, Mapping), (
        f"gateway response must expose headers on the message, got {metadata!r}"
    )
    return {str(key).lower(): str(value) for key, value in raw.items()}


def assert_gateway_identity_ok(
    headers: Mapping[str, str], *, pinned_model: str
) -> None:
    """The pinned name answered with no gateway retry and no fallback (§9.1).

    ``headers`` are matched case-insensitively. The group must equal the
    pinned name and attempted fallbacks must read ``0``; attempted retries
    may read ``0`` or be absent (absent means ``0``). Anything else —
    including a present-but-unparseable count — fails closed.
    """
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    assert lowered.get("x-litellm-model-group") == pinned_model, (
        f"x-litellm-model-group must equal the pinned name {pinned_model!r}, "
        f"got {lowered.get('x-litellm-model-group')!r}"
    )
    assert lowered.get("x-litellm-attempted-fallbacks") == "0", (
        "x-litellm-attempted-fallbacks must read '0' on a pinned answer, "
        f"got {lowered.get('x-litellm-attempted-fallbacks')!r}"
    )
    retries = lowered.get("x-litellm-attempted-retries")
    assert retries is None or retries == "0", (
        "x-litellm-attempted-retries must read '0' or be absent on a pinned "
        f"answer, got {retries!r}"
    )


def summarize_gateway_evidence(headers: Mapping[str, str]) -> str:
    """One fact line for the run record: what answered and which version.

    The upstream and the LiteLLM version are recorded as a fact about one
    run, never as a support claim: nothing here asserts their values.
    """
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    group = lowered.get("x-litellm-model-group")
    model_id = lowered.get("x-litellm-model-id")
    model_name = lowered.get("x-litellm-model-name")
    version = lowered.get("x-litellm-version")
    return (
        f"upstream model_group={group!r} model_id={model_id!r} "
        f"model_name={model_name!r} litellm-version={version!r}"
    )


def reasoning_blocks_of(message: AIMessage) -> list[dict[str, Any]]:
    """The ``reasoning`` content blocks of one assistant turn."""
    blocks: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, dict) and block.get("type") == "reasoning":
            blocks.append(block)
    return blocks


def assert_reasoning_carried_verbatim(
    first: AIMessage, second_body: dict[str, Any]
) -> None:
    """The second request carries the first turn back byte-equal (ADR 0010).

    Every ``reasoning`` block of ``first`` — including
    ``encrypted_content`` — appears unchanged in the second request's input,
    so the turn is self-contained and never a reference to server-side
    state. The tool call travels with it, by call id.
    """
    first_blocks = reasoning_blocks_of(first)
    assert first.tool_calls, "the first turn must carry its tool call"
    call_id = first.tool_calls[0]["id"]
    turn_input = second_body.get("input")
    assert isinstance(turn_input, list), (
        f"the second request must carry the turn in its input, got {second_body!r}"
    )
    carried = [
        block
        for block in turn_input
        if isinstance(block, dict) and block.get("type") == "reasoning"
    ]
    assert len(carried) == len(first_blocks), (
        f"the second request must carry all {len(first_blocks)} reasoning "
        f"blocks back, carried {len(carried)}"
    )
    for sent, back in zip(first_blocks, carried):
        assert back == sent, (
            "a reasoning block came back changed: verbatim means byte-equal, "
            f"sent {sent!r}, carried {back!r}"
        )
    assert any(
        isinstance(block, dict) and block.get("call_id") == call_id
        for block in turn_input
    ), f"the second request must carry the tool call {call_id!r} back"
