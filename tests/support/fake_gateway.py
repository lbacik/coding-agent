"""The fake Model Gateway: an ``httpx`` transport replaying redacted recordings.

Contract: ``docs/contract/step-workflow-runtime-contract.md`` §2.1 (a
gateway pin names its gateway and URL; the adapter serves the Responses API
at the gateway's ``/v1``).

The transport replays the vendored recordings in
``tests/fixtures/gateway/`` (redacted shapes of
``research/litellm-test-gateway`` traffic) plus mutated variants the tests
build from them. Every request is recorded so a test can assert on the wire:
the path, the gateway key, the effort, the output cap, ``store: False`` and
``include: ["reasoning.encrypted_content"]`` — and that no gateway metadata
endpoint (``/model/info``, ``/v1/models``) was ever called.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "gateway"

METADATA_PATHS = ("/model/info", "/v1/models")
"""Gateway metadata endpoints the adapter must never call (ADR 0009)."""


def load_recording(name: str) -> tuple[int, dict[str, str], dict[str, Any]]:
    """A vendored recording: its status, headers and JSON body."""
    raw = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    status = int(raw["status"])
    headers = {str(k): str(v) for k, v in dict(raw.get("headers", {})).items()}
    body = dict(raw["body"])
    return status, headers, body


def recording_response(
    name: str, *, extra_headers: dict[str, str] | None = None
) -> httpx.Response:
    """A recording as an ``httpx`` response."""
    status, headers, body = load_recording(name)
    if extra_headers:
        headers = {**headers, **extra_headers}
    return httpx.Response(status, json=body, headers=headers)


def request_json(request: httpx.Request) -> dict[str, Any]:
    """The request's JSON body."""
    return dict(json.loads(request.content.decode("utf-8")))


Responder = Callable[[httpx.Request, dict[str, Any]], httpx.Response]
"""Answers one gateway request from its parsed JSON body."""


@dataclass
class RecordedRequest:
    """One request the fake gateway saw, with its parsed JSON body."""

    method: str
    path: str
    authorization: str | None
    body: dict[str, Any]


@dataclass
class FakeGatewayTransport(httpx.BaseTransport):
    """An ``httpx`` transport answering from ``responder``, recording all."""

    responder: Responder
    requests: list[RecordedRequest] = field(default_factory=list)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request_json(request)
        self.requests.append(
            RecordedRequest(
                method=request.method,
                path=request.url.path,
                authorization=request.headers.get("authorization"),
                body=body,
            )
        )
        return self.responder(request, body)

    def assert_only_responses_api(self) -> None:
        """No gateway metadata endpoint was ever called."""
        for recorded in self.requests:
            assert recorded.path == "/v1/responses", (
                f"unexpected gateway path {recorded.path!r}: "
                "the adapter speaks only the Responses API, never "
                f"a metadata endpoint ({', '.join(METADATA_PATHS)})"
            )


def queue_responder(responses: list[httpx.Response]) -> Responder:
    """Answer each request with the next queued response, in order."""

    def respond(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        assert responses, f"fake gateway got an unexpected request to {request.url.path}"
        return responses.pop(0)

    return respond
